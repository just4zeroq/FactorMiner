"""Data-driven annualisation (P0-a).

Two things are pinned here:

1. The 7x24 crypto calendar: 1d -> 365, 4h -> 2190, 1h -> 8760, 5m -> 105120,
   with an explicit override and a DatetimeIndex fallback.
2. The **no-behaviour-change invariant**: annualisation only rescales reported
   annualised Sharpes. HAC t, BH-FDR and the DSR verdict are computed on the
   per-period Sharpe and must be byte-for-byte identical before/after the fix.
   If a future change makes a gate read ``annualisation``, these tests fail.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from core.evaluation.annualisation import (
    DEFAULT_ANNUALISATION,
    MINUTES_PER_YEAR,
    index_minutes,
    infer_annualisation,
    resolve_annualisation,
    timeframe_minutes,
)
from core.evaluation.evaluator import DEFAULT_FITNESS_CFG, ParallelEvaluator
from core.evaluation.portfolio_eval import long_short_returns
from core.evaluation.significance import (
    apply_promotion_gates,
    deflated_sharpe_ratio,
    multiple_testing_bar,
)


class TimeframeMappingTests(unittest.TestCase):
    def test_crypto_calendar_mappings(self):
        self.assertEqual(infer_annualisation("1d"), 365.0)
        self.assertEqual(infer_annualisation("4h"), 2190.0)
        self.assertEqual(infer_annualisation("1h"), 8760.0)
        self.assertEqual(infer_annualisation("5m"), 105120.0)
        self.assertEqual(infer_annualisation("15m"), 35040.0)

    def test_case_and_whitespace_tolerant(self):
        self.assertEqual(infer_annualisation(" 4H "), 2190.0)
        self.assertEqual(timeframe_minutes("1day"), 1440.0)
        self.assertIsNone(timeframe_minutes("nonsense"))
        self.assertIsNone(timeframe_minutes("0h"))

    def test_fallback_is_365_not_252(self):
        self.assertEqual(DEFAULT_ANNUALISATION, 365.0)
        self.assertEqual(infer_annualisation(), 365.0)
        self.assertEqual(infer_annualisation("bogus"), 365.0)
        self.assertEqual(MINUTES_PER_YEAR / 1440.0, 365.0)

    def test_datetime_index_infers_bar_frequency(self):
        hourly = pd.date_range("2025-01-01", periods=100, freq="h")
        self.assertAlmostEqual(index_minutes(hourly), 60.0)
        self.assertEqual(infer_annualisation(index=hourly), 8760.0)
        daily = pd.date_range("2025-01-01", periods=100, freq="D")
        self.assertEqual(infer_annualisation(index=daily), 365.0)

    def test_timeframe_wins_over_index(self):
        hourly = pd.date_range("2025-01-01", periods=100, freq="h")
        self.assertEqual(infer_annualisation("1d", hourly), 365.0)

    def test_index_is_ignored_when_not_datetime(self):
        self.assertIsNone(index_minutes(pd.RangeIndex(100)))
        self.assertEqual(infer_annualisation(index=pd.RangeIndex(100)), 365.0)


class ResolutionTests(unittest.TestCase):
    def test_data_feeds_timeframe_drives_resolution(self):
        cfg = {"data_feeds": {"timeframe": "1h"}}
        self.assertEqual(resolve_annualisation(cfg), 8760.0)

    def test_explicit_fitness_override_wins(self):
        cfg = {
            "fitness": {"annualisation": 252},
            "data_feeds": {"timeframe": "1h"},
        }
        self.assertEqual(resolve_annualisation(cfg), 252.0)

    def test_explicit_gates_override_wins(self):
        cfg = {
            "evaluation": {"gates": {"annualisation": 252}},
            "data_feeds": {"timeframe": "4h"},
        }
        self.assertEqual(resolve_annualisation(cfg), 252.0)

    def test_explicit_timeframe_argument_overrides_config_timeframe(self):
        cfg = {"data_feeds": {"timeframe": "1d"}}
        self.assertEqual(resolve_annualisation(cfg, timeframe="1h"), 8760.0)

    def test_invalid_override_is_ignored(self):
        cfg = {"fitness": {"annualisation": "abc"}, "data_feeds": {"timeframe": "1d"}}
        self.assertEqual(resolve_annualisation(cfg), 365.0)

    def test_empty_config_and_runtime_hints(self):
        self.assertEqual(resolve_annualisation(), 365.0)
        hourly = pd.date_range("2025-01-01", periods=50, freq="h")
        self.assertEqual(resolve_annualisation({}, index=hourly), 8760.0)


class NoBehaviourChangeTests(unittest.TestCase):
    """Annualisation must NOT move any promotion decision."""

    def _panel(self):
        rng = np.random.default_rng(11)
        n, m = 240, 6
        f = pd.DataFrame(
            rng.standard_normal((n, m)),
            index=pd.date_range("2025-01-01", periods=n, freq="D"),
            columns=[f"A{i}" for i in range(m)],
        )
        # A real (if noisy) signal so the verdict is non-degenerate.
        r = 0.08 * f + 0.3 * pd.DataFrame(
            rng.standard_normal((n, m)), index=f.index, columns=f.columns
        )
        return f, r

    def test_long_short_only_sharpe_changes(self):
        f, r = self._panel()
        kwargs = dict(quantile=0.2, orientation=1.0, cost_rate=0.0018)
        a = long_short_returns(f, r, annualisation=252.0, **kwargs)
        b = long_short_returns(f, r, annualisation=365.0, **kwargs)
        for key in (
            "mean_return", "std_return", "hit_rate", "turnover", "cost",
            "cost_adj_return", "n_obs", "ls_t",
        ):
            self.assertAlmostEqual(a[key], b[key], places=12, msg=key)
        self.assertAlmostEqual(
            b["sharpe"] / a["sharpe"], math.sqrt(365.0 / 252.0), places=10
        )

    def test_dsr_is_unchanged_by_annualisation(self):
        f, r = self._panel()
        kwargs = dict(quantile=0.2, orientation=1.0, cost_rate=0.0018)
        outputs = []
        for ann in (252.0, 365.0, 8760.0):
            ls = long_short_returns(f, r, annualisation=ann, **kwargs)
            sr = ls["cost_adj_return"] / ls["std_return"]
            outputs.append(
                (
                    deflated_sharpe_ratio(sr, ls["n_obs"], n_trials=787),
                    sr,
                    ls["n_obs"],
                )
            )
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])

    def test_evaluator_verdict_is_identical_except_displayed_sharpe(self):
        f, r = self._panel()

        def verdict_for(cfg):
            ev = ParallelEvaluator.__new__(ParallelEvaluator)
            ev.config = cfg
            ev.gate_rebalance_bars = 1
            return ev._build_verdict(
                f, r, {**DEFAULT_FITNESS_CFG, **cfg.get("fitness", {})}
            )

        stock = verdict_for({"fitness": {"annualisation": 252}})
        crypto = verdict_for({"data_feeds": {"timeframe": "1d"}})
        for key in ("hac_t", "p_value", "dsr", "ir", "n_obs",
                    "cost_adj_return", "sr", "ls_t", "ls_turnover"):
            self.assertAlmostEqual(stock[key], crypto[key], places=12, msg=key)
        self.assertAlmostEqual(
            crypto["ls_sharpe"] / stock["ls_sharpe"],
            math.sqrt(365.0 / 252.0),
            places=10,
        )

    def test_promotion_gates_do_not_read_annualisation(self):
        metrics = [{
            "hac_t": 3.1, "p_value": 0.002, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.001,
            "RankIC_oos": 0.05,
        }]
        baseline = apply_promotion_gates(metrics, n_trials=100)
        annotated = apply_promotion_gates(
            [{**metrics[0], "annualisation": 8760}], n_trials=100
        )
        self.assertEqual(baseline[0]["gate"]["pass"], annotated[0]["gate"]["pass"])
        self.assertEqual(baseline[0]["p_adj"], annotated[0]["p_adj"])

    def test_reporting_bar_scales_but_per_period_bar_does_not(self):
        a = multiple_testing_bar(610, 787, annualisation=252)
        b = multiple_testing_bar(610, 787, annualisation=365)
        self.assertEqual(a["bar_per_period_sharpe"], b["bar_per_period_sharpe"])
        self.assertAlmostEqual(
            b["bar_annualised_sharpe"] / a["bar_annualised_sharpe"],
            math.sqrt(365.0 / 252.0),
            places=10,
        )
        self.assertAlmostEqual(a["bar_annualised_sharpe"], 2.35, delta=0.05)
        self.assertAlmostEqual(b["bar_annualised_sharpe"], 2.83, delta=0.05)


if __name__ == "__main__":
    unittest.main()
