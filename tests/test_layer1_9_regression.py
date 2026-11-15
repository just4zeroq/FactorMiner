"""Regression tests for review items 9 (E-05 / E-08 / E-01).

* E-05: the gate's ``ls_significant`` must test the NET return, not gross.
* E-08: shrinkage uses T (time observations), fitness book uses the gate's
        rebalance cadence and the same orientation statistic as the verdict.
* E-01: DSR must not "pass" on a non-positive variance term, and its
        multiple-testing benchmark must use the cross-trial sigma(SR).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from core.evaluation import evaluator as evaluator_mod
from core.evaluation.evaluator import ParallelEvaluator
from core.evaluation.portfolio_eval import long_short_returns, ls_t_stat
from core.evaluation.significance import (
    apply_promotion_gates,
    deflated_sharpe_ratio,
    multiple_testing_bar,
)


def _panels(seed: int = 0, t: int = 240, n: int = 6, turnover: float = 1.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=t, freq="D")
    cols = [f"A{i}" for i in range(n)]
    # A signal whose ranking changes every bar -> high turnover.
    signal = rng.standard_normal((t, n)) * turnover
    returns = pd.DataFrame(0.02 * signal + 0.01 * rng.standard_normal((t, n)),
                           index=idx, columns=cols)
    return pd.DataFrame(signal, index=idx, columns=cols), returns


class GrossNetAlignmentTests(unittest.TestCase):
    def test_ls_t_is_the_net_return_t(self):
        factor, returns = _panels()
        book = long_short_returns(factor, returns, cost_rate=0.01, rebalance_bars=1)
        self.assertIn("ls_t_gross", book)
        self.assertIn("net_mean_return", book)
        # Cost can only reduce the mean, so the net t must not exceed gross t.
        self.assertLessEqual(book["ls_t"], book["ls_t_gross"] + 1e-9)
        self.assertAlmostEqual(book["net_mean_return"], book["cost_adj_return"], places=10)

    def test_net_t_matches_explicit_hac_on_net_series(self):
        factor, returns = _panels(seed=3)
        book = long_short_returns(factor, returns, cost_rate=0.02)
        self.assertAlmostEqual(book["net_mean_return"], book["cost_adj_return"], places=10)
        # A higher cost rate must strictly lower the t-stat (gross t is fixed).
        expensive = long_short_returns(factor, returns, cost_rate=0.05)
        self.assertLess(expensive["ls_t"], book["ls_t"])
        self.assertAlmostEqual(expensive["ls_t_gross"], book["ls_t_gross"], places=10)

    def test_gross_fields_still_describe_gross(self):
        factor, returns = _panels(seed=5)
        book = long_short_returns(factor, returns, cost_rate=0.01)
        self.assertAlmostEqual(book["cost_adj_return"],
                               book["mean_return"] - book["cost"], places=10)
        self.assertLessEqual(book["net_hit_rate"], book["hit_rate"] + 1e-12)


class ShrinkageAndBookAlignmentTests(unittest.TestCase):
    def test_effective_obs_is_time_not_cells(self):
        factor, returns = _panels(t=100, n=6)
        self.assertEqual(ParallelEvaluator._effective_obs(factor, returns), 100)
        self.assertEqual(
            ParallelEvaluator._valid_pair_count(factor, returns), 100 * 6
        )
        self.assertEqual(
            ParallelEvaluator._effective_obs({"close": factor}, returns), 100
        )

    def test_book_uses_gate_rebalance_cadence(self):
        factor, returns = _panels(seed=7, t=300)
        one = ParallelEvaluator._calculate_built_in_metrics(factor, returns, 1)
        five = ParallelEvaluator._calculate_built_in_metrics(factor, returns, 5)
        self.assertIn("ls_cost_adj_return", one)
        self.assertNotAlmostEqual(
            one["ls_cost_adj_return"], five["ls_cost_adj_return"], places=6
        )

    def test_orientation_uses_ic_time_series_mean(self):
        factor, returns = _panels(seed=9)
        captured = {}

        def _fake_book(f, r, **kwargs):
            captured.update(kwargs)
            return {"cost_adj_return": 0.0, "ls_t": 0.0, "mean_return": 0.0}

        # Force a positive pooled RankIC but a negative IC-time-series mean.
        with patch("core.evaluation.evaluator.ic_series", return_value=np.full(10, -0.3)), \
             patch("core.evaluation.portfolio_eval.long_short_returns", side_effect=_fake_book):
            ParallelEvaluator._calculate_built_in_metrics(factor, returns, 1)
        self.assertEqual(captured.get("orientation"), -1.0)
        self.assertEqual(captured.get("rebalance_bars"), 1)


class DsrDeflationTests(unittest.TestCase):
    def test_non_positive_variance_term_fails_closed(self):
        # 1 - skew*SR + (kurt-1)/4*SR^2 < 0. The old clamp inside sqrt made
        # denom ~3.16e-5, z -> +inf and DSR -> 1.0 (a bogus pass).
        dsr = deflated_sharpe_ratio(
            sharpe=1.0, n_obs=200, n_trials=100, skewness=10.0, kurtosis=3.0
        )
        self.assertEqual(dsr, 0.0)

    def test_cross_trial_sigma_raises_the_bar(self):
        low = deflated_sharpe_ratio(sharpe=0.1, n_obs=100, n_trials=100,
                                    sigma_sr=0.01)
        high = deflated_sharpe_ratio(sharpe=0.1, n_obs=100, n_trials=100,
                                     sigma_sr=0.5)
        self.assertLess(high, low)
        self.assertGreater(
            multiple_testing_bar(100, 100, sigma_sr=0.5)["bar_per_period_sharpe"],
            multiple_testing_bar(100, 100, sigma_sr=0.01)["bar_per_period_sharpe"],
        )

    def test_more_trials_lower_dsr(self):
        few = deflated_sharpe_ratio(sharpe=0.2, n_obs=200, n_trials=2,
                                    sigma_sr=0.2)
        many = deflated_sharpe_ratio(sharpe=0.2, n_obs=200, n_trials=5000,
                                     sigma_sr=0.2)
        self.assertLess(many, few)

    def test_gate_estimates_sigma_from_trial_population(self):
        metrics = [
            {"p_value": 0.01, "hac_t": 3.0, "sr": sr, "sr_n_obs": 120,
             "cost_adj_return": 0.01, "oos_consistent": True,
             "oos_enabled": True, "RankIC_oos": 0.05, "ls_t": 2.0}
            for sr in (0.1, 0.2, 0.3, 0.4, 0.5)
        ]
        out = apply_promotion_gates(metrics, n_trials=50)
        sigma = float(np.std([m["sr"] for m in metrics], ddof=1))
        expected = deflated_sharpe_ratio(
            sharpe=0.1, n_obs=120, n_trials=50, sigma_sr=sigma
        )
        self.assertAlmostEqual(out[0]["dsr"], expected, places=10)
        legacy = deflated_sharpe_ratio(sharpe=0.1, n_obs=120, n_trials=50)
        self.assertNotAlmostEqual(out[0]["dsr"], legacy, places=6)


if __name__ == "__main__":
    unittest.main()
