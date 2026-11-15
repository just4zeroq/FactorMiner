"""One definition of "this bar has closed" for panel, planner and freshness.

A bar stamped ``t`` covers ``[t, t + timeframe)``. Until ``t + timeframe <= now``
its OHLCV is partial and its forward return is undefined. Three places used to
disagree about that:

* the mining panel trimmed it (D-05),
* the holdout window planner did not, so a window could be ``ready`` on a
  half-formed candle and burn a holdout window on it (P0-d),
* the freshness report counted it as an accumulated bar, making a
  pre-registered window look complete one bar early.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.data_feed.bar_clock import (
    bar_delta,
    bar_is_closed,
    closed_only,
    drop_unclosed_last_bar,
    point_is_known,
)
from core.data_feed.naming import data_path
from core.experiment import windows


class BarClockUnitTests(unittest.TestCase):
    def test_daily_bar_closes_at_the_next_midnight(self):
        start = pd.Timestamp("2026-11-15")
        self.assertFalse(bar_is_closed(start, "1d", now=pd.Timestamp("2026-11-15 10:12")))
        self.assertTrue(bar_is_closed(start, "1d", now=pd.Timestamp("2026-11-16 00:00")))
        self.assertTrue(bar_is_closed(start, "1d", now=pd.Timestamp("2026-11-16 00:01")))

    def test_hourly_and_four_hour_bars(self):
        start = pd.Timestamp("2026-11-15 08:00")
        self.assertFalse(bar_is_closed(start, "1h", now=pd.Timestamp("2026-11-15 08:59")))
        self.assertTrue(bar_is_closed(start, "1h", now=pd.Timestamp("2026-11-15 09:00")))
        start4 = pd.Timestamp("2026-11-15 08:00")
        self.assertFalse(bar_is_closed(start4, "4h", now=pd.Timestamp("2026-11-15 11:59")))
        self.assertTrue(bar_is_closed(start4, "4h", now=pd.Timestamp("2026-11-15 12:00")))

    def test_point_stamps_only_reject_the_future(self):
        stamp = pd.Timestamp("2026-11-15 00:00")
        self.assertTrue(point_is_known(stamp, now=pd.Timestamp("2026-11-15 00:00")))
        self.assertFalse(point_is_known(stamp, now=pd.Timestamp("2026-11-14 23:59")))

    def test_timezone_aware_now_is_normalised(self):
        start = pd.Timestamp("2026-11-15")
        now = pd.Timestamp("2026-11-16 09:00", tz="Asia/Shanghai")  # 01:00 UTC
        self.assertTrue(bar_is_closed(start, "1d", now=now))

    def test_closed_only_drops_the_tail_bar(self):
        dates = pd.date_range("2026-11-10", periods=5, freq="D")
        kept = closed_only(dates, "1d", now=pd.Timestamp("2026-11-14 12:00"))
        self.assertEqual(
            [d.strftime("%Y-%m-%d") for d in kept],
            ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"],
        )

    def test_drop_unclosed_last_bar_is_a_noop_on_closed_data(self):
        idx = pd.date_range("2026-11-10", periods=3, freq="D")
        frame = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=idx)
        out = drop_unclosed_last_bar(frame, "1d", now=pd.Timestamp("2026-11-20"))
        self.assertEqual(len(out), 3)

    def test_bar_delta_falls_back_to_one_day(self):
        self.assertEqual(bar_delta("weird"), pd.Timedelta(days=1))


def _write_tape(root: Path, symbol: str, dates) -> None:
    p = data_path(root, "binance", symbol, "1d", "futures")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime(list(dates)), "close": 1.0}).to_feather(p)


class WindowPlannerTests(unittest.TestCase):
    """A window must never be 'ready' while its last bar is still forming."""

    spec = {
        "label": "rolling", "rolling": True, "start": "auto",
        "timeframe": "1d", "window_bars": 30, "embargo_bars": 5,
    }

    def _plan(self, dates, now):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        items = []
        for i in range(2):
            symbol = f"A{i}/USDT:USDT"
            _write_tape(root, symbol, dates)
            items.append(
                {"symbol": symbol, "exchange": "binance",
                 "trade_type": "futures", "timeframe": "1d"}
            )
        plan = windows.plan_windows(
            self.spec, data_root=str(root), items=items,
            revealed=[{"start": "2025-07-01", "end": "2026-11-01"}],
            now=now,
        )
        tmp.cleanup()
        return plan

    def test_forming_bar_prevents_a_window_from_being_ready(self):
        dates = pd.date_range("2026-11-02", periods=30, freq="D")
        # now is inside the last bar's window -> 29 closed bars, window not ready
        plan = self._plan(dates, now=pd.Timestamp("2026-12-01 12:00"))
        self.assertEqual(plan["n_common_bars"], 29)
        self.assertEqual(plan["n_ready"], 0)
        self.assertFalse(plan["windows"][0]["ready"])

        # one day later the same tape yields a complete 30-bar window
        plan2 = self._plan(dates, now=pd.Timestamp("2026-12-02 00:00"))
        self.assertEqual(plan2["n_common_bars"], 30)
        self.assertEqual(plan2["n_ready"], 1)


class FreshnessTests(unittest.TestCase):
    def test_freshness_does_not_count_the_forming_bar(self):
        from core.data_feed.freshness import scan_library

        dates = pd.date_range("2026-11-01", periods=5, freq="D")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tape(root, "A0/USDT:USDT", dates)
            trimmed, _ = scan_library(str(root), now=pd.Timestamp("2026-11-05 09:00"))
            full, _ = scan_library(str(root), now=pd.Timestamp("2026-11-06 00:00"))
        self.assertEqual(trimmed[0]["last"], "2026-11-04")
        self.assertEqual(trimmed[0]["rows"], 4)
        self.assertEqual(full[0]["last"], "2026-11-05")
        self.assertEqual(full[0]["rows"], 5)


if __name__ == "__main__":
    unittest.main()
