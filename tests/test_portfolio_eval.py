import unittest

import numpy as np
import pandas as pd

from core.evaluation.portfolio_eval import (
    DEFAULT_LS_CFG,
    long_short_returns,
    ls_t_stat,
)


def _panel(T, N, seed):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=T, freq="D")
    cols = [f"A{i}" for i in range(N)]
    return (
        pd.DataFrame(rng.standard_normal((T, N)), index=idx, columns=cols),
        idx,
        cols,
    )


class LongShortReturnTest(unittest.TestCase):
    def test_perfect_signal_is_profitable_before_and_after_cost(self):
        T, N = 120, 20
        f, idx, cols = _panel(T, N, 1)
        # returns are an exact monotone function of the factor
        r = pd.DataFrame(0.01 * f.to_numpy(), index=idx, columns=cols)
        res = long_short_returns(f, r, orientation=1.0, cost_rate=0.0)
        self.assertGreater(res["mean_return"], 0.0)
        self.assertGreater(res["hit_rate"], 0.95)
        self.assertGreater(res["sharpe"], 5.0)
        # zero cost => net equals gross
        self.assertAlmostEqual(res["cost_adj_return"], res["mean_return"], places=12)

    def test_cost_turns_a_thin_edge_negative(self):
        rng = np.random.default_rng(2)
        T, N = 200, 20
        f, idx, cols = _panel(T, N, 2)
        r = pd.DataFrame(
            0.002 * f.to_numpy() + rng.standard_normal((T, N)),
            index=idx,
            columns=cols,
        )
        free = long_short_returns(f, r, orientation=1.0, cost_rate=0.0)
        charged = long_short_returns(f, r, orientation=1.0, cost_rate=0.02)
        self.assertGreater(free["cost_adj_return"], charged["cost_adj_return"])
        self.assertGreater(charged["cost"], free["cost"])

    def test_negative_ic_factor_is_profitable_once_oriented(self):
        # Regression: the book must go long the TOP of the oriented signal.
        # An inverted implementation silently longs the wrong tail and reports
        # a negative gross return for a perfectly good negative-IC factor.
        T, N = 200, 20
        f, idx, cols = _panel(T, N, 7)
        r = pd.DataFrame(-0.01 * f.to_numpy(), index=idx, columns=cols)

        raw = long_short_returns(f, r, orientation=1.0, cost_rate=0.0)
        flipped = long_short_returns(f, r, orientation=-1.0, cost_rate=0.0)

        self.assertLess(raw["mean_return"], 0.0)
        self.assertGreater(flipped["mean_return"], 0.0)
        # Rank quantisation on a 20-asset cross-section makes the two books
        # almost, not exactly, mirror images of each other.
        self.assertLess(
            abs(flipped["mean_return"] + raw["mean_return"]),
            0.01 * abs(raw["mean_return"]),
        )

    def test_holding_period_reduces_turnover(self):
        T, N = 250, 20
        f, idx, cols = _panel(T, N, 3)
        r = pd.DataFrame(np.random.default_rng(9).standard_normal((T, N)),
                         index=idx, columns=cols)
        daily = long_short_returns(f, r, rebalance_bars=1, cost_rate=0.0018)
        weekly = long_short_returns(f, r, rebalance_bars=5, cost_rate=0.0018)
        self.assertGreater(daily["turnover"], weekly["turnover"] * 3.0)
        # holding the book 5 bars must not pay 5x the cost
        self.assertLess(weekly["cost"], daily["cost"])

    def test_gross_exposure_is_one(self):
        T, N = 60, 20
        f, idx, cols = _panel(T, N, 4)
        r = pd.DataFrame(np.zeros((T, N)), index=idx, columns=cols)
        res = long_short_returns(f, r, orientation=1.0)
        self.assertAlmostEqual(res["gross"], 1.0)
        self.assertAlmostEqual(res["mean_return"], 0.0, places=12)

    def test_insufficient_cross_section_returns_zeros(self):
        idx = pd.date_range("2022-01-01", periods=5, freq="D")
        f = pd.DataFrame(np.ones((5, 2)), index=idx, columns=["A", "B"])
        r = pd.DataFrame(np.ones((5, 2)), index=idx, columns=["A", "B"])
        res = long_short_returns(f, r)
        # 2 assets cannot form a 20% long and 20% short leg twice over
        self.assertEqual(res["n_obs"], 0)
        self.assertEqual(res["cost_adj_return"], 0.0)

    def test_series_input_is_neutral(self):
        s = pd.Series(np.arange(10.0))
        res = long_short_returns(s, s)
        self.assertEqual(res["n_obs"], 0)

    def test_defaults_are_documented(self):
        self.assertEqual(DEFAULT_LS_CFG["quantile"], 0.2)
        self.assertGreater(DEFAULT_LS_CFG["cost_rate"], 0.0)


class LsTStatTest(unittest.TestCase):
    def test_positive_drift_is_significant(self):
        rng = np.random.default_rng(11)
        x = 0.05 + 0.01 * rng.standard_normal(500)
        self.assertGreater(ls_t_stat(x), 5.0)

    def test_zero_mean_is_not(self):
        rng = np.random.default_rng(12)
        x = rng.standard_normal(500)
        self.assertLess(abs(ls_t_stat(x)), 2.5)

    def test_short_series(self):
        self.assertEqual(ls_t_stat(np.array([0.1, 0.2])), 0.0)


if __name__ == "__main__":
    unittest.main()
