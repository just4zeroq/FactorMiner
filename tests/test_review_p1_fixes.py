"""Regression tests for the quant-review P1 fixes (review 2026-11-14).

* M6  ``_finalize_linear`` must keep the whole hall of fame, not just the
        top-K sub-factors used to fit the ridge combination (the rest were
        silently dropped before reaching the gate / factor_db).
* M9  ``long_short_returns`` / ``timing_returns`` expose the net-return
        skewness and Pearson kurtosis, and DSR actually uses them (negative
        skew / fat tails must lower the deflated Sharpe).
"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from core.evaluation.portfolio_eval import long_short_returns
from core.evaluation.significance import deflated_sharpe_ratio
from core.evaluation.targets import ForwardReturnTarget
from core.experiment.pipeline import _oos_warm_panel
from core.miner.paradigms.base import BaseFactorMiner
from core.miner.pool import AlphaPool


class TestOosWarmup(unittest.TestCase):
    def test_prepends_history_and_preserves_test_window(self):
        mine = pd.DataFrame(
            {"close": np.arange(100, dtype=float)},
            index=pd.date_range("2024-01-01", periods=100, freq="D"),
        )
        test = pd.DataFrame(
            {"close": np.arange(100, 150, dtype=float)},
            index=pd.date_range("2024-04-10", periods=50, freq="D"),
        )
        warm = _oos_warm_panel(mine, test, 30)
        self.assertEqual(len(warm), 30 + 50)
        self.assertTrue(warm.index.is_monotonic_increasing)
        # The real test window is preserved verbatim at the tail.
        self.assertTrue(warm.index[-50:].equals(test.index))
        # Warmup rows are the tail of the in-sample panel (no leakage forward).
        self.assertTrue(warm.index[:30].equals(mine.index[-30:]))

    def test_zero_warmup_is_passthrough(self):
        test = pd.DataFrame(
            {"x": [1.0]}, index=pd.date_range("2024-01-01", periods=1)
        )
        self.assertIs(_oos_warm_panel(None, test, 0), test)

    def test_cross_asset_dict_panels_are_warmed(self):
        # cross_asset mode hands the OOS stage a dict of feature panels, not a
        # single DataFrame — warmup must cover every key.
        mine_idx = pd.date_range("2024-01-01", periods=100, freq="D")
        test_idx = pd.date_range("2024-04-10", periods=50, freq="D")
        mine = {
            "close": pd.DataFrame(
                {"A": np.arange(100, dtype=float)}, index=mine_idx
            ),
            "open": pd.DataFrame(
                {"A": np.arange(100, dtype=float)}, index=mine_idx
            ),
        }
        test = {
            "close": pd.DataFrame(
                {"A": np.arange(100, 150, dtype=float)}, index=test_idx
            ),
            "open": pd.DataFrame(
                {"A": np.arange(100, 150, dtype=float)}, index=test_idx
            ),
        }
        warm = _oos_warm_panel(mine, test, 30)
        self.assertIsInstance(warm, dict)
        self.assertEqual(set(warm), {"close", "open"})
        self.assertEqual(len(warm["close"]), 80)
        self.assertTrue(warm["close"].index[-50:].equals(test_idx))


class TestLabelExecutionAlignment(unittest.TestCase):
    def test_next_close_matches_next_bar_close_execution(self):
        n = 8
        close = pd.Series(100.0 + np.arange(n, dtype=float))
        frame = pd.DataFrame(
            {"open": close - 0.5, "close": close},
            index=pd.date_range("2024-01-01", periods=n, freq="min"),
        )
        target = ForwardReturnTarget.from_config(
            {"entry_price": "next_close", "horizon_bars": 1}
        )
        actual = target.build(frame)
        # Backtest fills at the NEXT bar's close, then holds `horizon` bars:
        # return from close[t+1] to close[t+2].
        c = frame["close"]
        expected = (c.shift(-2) / c.shift(-1) - 1.0).rename("returns")
        pd.testing.assert_series_equal(actual, expected)
        self.assertEqual(target.definition(), "close[t+2] / close[t+1] - 1")

    def test_alphapool_default_shift_matches_native_horizon(self):
        # P1-M8: default must predict the native 1-bar label, not 21 bars ahead.
        self.assertEqual(AlphaPool().weight_fit_shift, 0)


class _ConcreteMiner(BaseFactorMiner):
    """Minimal concrete subclass so the ABC can be instantiated in the test."""

    def generate_candidates(self, *args, **kwargs):
        return []

    def evaluate_candidates(self, *args, **kwargs):
        return None

    def initialize_search_space(self, *args, **kwargs):
        return None

    def update_model(self, *args, **kwargs):
        return None


class TestHallOfFamePreserved(unittest.TestCase):
    def test_finalize_linear_keeps_all_candidates(self):
        m = object.__new__(_ConcreteMiner)
        m.config = {
            "paradigm": "GP",
            "linear_combine": True,
            "linear_combine_top_k": 2,
        }
        m.data = MagicMock()
        rng = np.random.default_rng(0)
        m.data.get_data.return_value = pd.DataFrame(rng.normal(size=(40, 5)))
        m.data.get_returns.return_value = pd.DataFrame(rng.normal(size=(40, 5)))
        cands = [MagicMock(name=f"c{i}") for i in range(6)]
        # Only 2 are selected to fit the combination; the other 4 must survive.
        m._select_diverse_combo_candidates = MagicMock(return_value=cands[:2])
        combined = MagicMock(name="combined")
        combined.weights = [0.5, 0.5]
        fb = MagicMock()
        fb.for_candidate.return_value = MagicMock(
            succeeded=True, metrics={"RankIC": 0.05}
        )
        m.evaluator = MagicMock()
        m.evaluator.evaluate.return_value = fb
        with patch(
            "core.miner.paradigms.base.fit_linear_combination",
            return_value=combined,
        ):
            out = m._finalize_linear(cands)
        self.assertIn(combined, out)
        for c in cands:
            self.assertIn(c, out)
        self.assertEqual(len(out), 1 + len(cands))


class TestNetMomentsForDSR(unittest.TestCase):
    def test_net_moments_present(self):
        rng = np.random.default_rng(0)
        f = pd.DataFrame(rng.normal(size=(200, 10)))
        r = pd.DataFrame(rng.normal(size=(200, 10)) * 0.01)
        out = long_short_returns(f, r, quantile=0.2, cost_rate=0.0018)
        self.assertIn("net_skew", out)
        self.assertIn("net_kurtosis", out)
        self.assertTrue(np.isfinite(out["net_skew"]))
        # Pearson kurtosis is >= 1 for any finite sample.
        self.assertGreaterEqual(out["net_kurtosis"], 1.0)

    def test_dsr_penalises_negative_skew_and_fat_tails(self):
        # Same Sharpe / sample size; non-normal moments must *lower* the DSR.
        normal = deflated_sharpe_ratio(sharpe=0.3, n_obs=250, n_trials=1)
        skewed = deflated_sharpe_ratio(
            sharpe=0.3, n_obs=250, n_trials=1, skewness=-1.5, kurtosis=8.0
        )
        self.assertLess(skewed, normal)


if __name__ == "__main__":
    unittest.main()
