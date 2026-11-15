"""Regression tests for layer-1 measurement alignment (E-07 / M-01).

* ``1M`` is a MONTH in CCXT/pandas, not a minute: annualising monthly bars
  with the minute convention inflated Sharpe by ~209x.
* ``_ts_rank`` must agree with ``rank(pct=True)``: largest value -> 1.0,
  ties -> average rank. The old implementation returned 0.0 for a rising
  series (fully inverted).
"""

from __future__ import annotations

import glob
import json
import os
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.evaluation.annualisation import (
    MINUTES_PER_YEAR,
    infer_annualisation,
    timeframe_minutes,
)
from core.miner.operator_runtime import _min_periods, _ts_rank


class TestMonthTimeframe(unittest.TestCase):
    def test_uppercase_m_is_a_month(self):
        self.assertEqual(timeframe_minutes("1M"), MINUTES_PER_YEAR / 12.0)
        self.assertAlmostEqual(infer_annualisation("1M"), 12.0)

    def test_lowercase_m_is_still_a_minute(self):
        self.assertEqual(timeframe_minutes("1m"), 1.0)
        self.assertEqual(infer_annualisation("1m"), float(MINUTES_PER_YEAR))
        self.assertEqual(infer_annualisation("5m"), 105120.0)

    def test_multi_month_and_year(self):
        self.assertAlmostEqual(infer_annualisation("3M"), 4.0)
        self.assertAlmostEqual(infer_annualisation("1Y"), 1.0)

    def test_unknown_units_still_fall_back(self):
        self.assertIsNone(timeframe_minutes("nonsense"))
        self.assertEqual(infer_annualisation("nonsense"), 365.0)


class TestTsRankDirection(unittest.TestCase):
    @staticmethod
    def _expected(series: pd.Series, window: int) -> pd.Series:
        return series.rolling(window, min_periods=_min_periods(window)).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=True
        )

    def test_rising_series_latest_value_is_top_percentile(self):
        s = pd.Series(np.arange(1, 21, dtype=float))
        got = _ts_rank(s, 5)
        self.assertAlmostEqual(float(got.iloc[-1]), 1.0)
        pd.testing.assert_series_equal(got, self._expected(s, 5))

    def test_falling_series_latest_value_is_bottom_percentile(self):
        s = pd.Series(np.arange(20, 0, -1, dtype=float))
        got = _ts_rank(s, 5)
        self.assertAlmostEqual(float(got.iloc[-1]), 1.0 / 5.0)
        pd.testing.assert_series_equal(got, self._expected(s, 5))

    def test_ties_use_average_rank(self):
        s = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0])
        got = _ts_rank(s, 5)
        # All five values tie; average rank is 3 -> 3/5.
        self.assertAlmostEqual(float(got.iloc[-1]), 3.0 / 5.0)
        pd.testing.assert_series_equal(got, self._expected(s, 5))

    def test_mixed_series_matches_pandas_for_every_window(self):
        rng = np.random.default_rng(7)
        s = pd.Series(np.round(rng.normal(size=60), 1))
        for w in (3, 5, 10):
            with self.subTest(window=w):
                pd.testing.assert_series_equal(_ts_rank(s, w), self._expected(s, w))


class TestEntryPriceAlignment(unittest.TestCase):
    """T-03: labels must be entered at the next bar's close.

    Backtests fill at NEXT_BAR/CLOSE; a `current_close` label is one bar early
    and inflates IC. Every shipped config must declare the aligned value
    explicitly rather than relying on the legacy default.
    """

    def test_every_shipped_config_uses_next_close(self):
        root = Path(__file__).resolve().parents[1]
        paths = sorted(glob.glob(str(root / "user_workspace" / "configs" / "*.json")))
        self.assertGreaterEqual(len(paths), 13)
        for path in paths:
            with self.subTest(config=os.path.basename(path)):
                with open(path, encoding="utf-8-sig") as fh:
                    cfg = json.load(fh)
                self.assertEqual(
                    (cfg.get("target") or {}).get("entry_price"),
                    "next_close",
                    f"{os.path.basename(path)} 缺少显式 next_close 入场口径",
                )


if __name__ == "__main__":
    unittest.main()
