"""P7-3: rolling pre-registered windows, anchored on the ledger, counted in bars."""

from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.data_feed.freshness import experiment_progress
from core.data_feed.naming import data_path
from core.experiment import windows


def _write_tape(root: Path, symbol: str, dates) -> None:
    p = data_path(root, "binance", symbol, "1d", "futures")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime(list(dates)), "close": 1.0}).to_feather(p)


def _items(root: Path, n: int = 3):
    return [
        {"symbol": f"A{i}/USDT:USDT", "exchange": "binance",
         "trade_type": "futures", "timeframe": "1d"}
        for i in range(n)
    ]


class TestAnchor(unittest.TestCase):
    def test_auto_anchor_is_day_after_last_reveal(self):
        revealed = [
            {"start": "2025-07-01", "end": "2026-11-13"},
            {"start": "2024-01-01", "end": "2024-06-30"},
        ]
        anchor = windows.resolve_anchor({"start": "auto"}, revealed)
        self.assertEqual(anchor["start"], "2026-11-14")
        self.assertEqual(anchor["source"], "ledger_end_plus_one")
        self.assertEqual(anchor["ledger_end"], "2026-11-13")

    def test_explicit_start_after_anchor_wastes_fresh_bars(self):
        anchor = windows.resolve_anchor(
            {"start": "2026-12-01"},
            [{"start": "2025-07-01", "end": "2026-11-13"}],
        )
        self.assertEqual(anchor["source"], "explicit")
        self.assertEqual(anchor["auto_anchor"], "2026-11-14")
        self.assertTrue(anchor["starts_after_anchor"])
        self.assertEqual(anchor["wasted_days"], 17)

    def test_auto_anchor_is_not_moved_by_rolling_reveals(self):
        anchor = windows.resolve_anchor({"start": "auto"}, [
            {"start": "2025-07-01", "end": "2026-11-13"},
            {"start": "2026-11-14", "end": "2026-12-13",
             "period_key": "rolling_2026-11-14_2026-12-13"},
        ])
        self.assertEqual(anchor["start"], "2026-11-14")
        self.assertEqual(anchor["ledger_end"], "2026-11-13")

    def test_no_ledger_falls_back_to_today(self):
        anchor = windows.resolve_anchor({"start": "auto"}, [])
        self.assertEqual(anchor["source"], "today_no_ledger")


class TestBarTimeline(unittest.TestCase):
    def test_intersection_excludes_a_missing_bar(self):
        dates = pd.date_range("2026-11-14", periods=40, freq="D")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tape(root, "A0/USDT:USDT", dates)
            _write_tape(root, "A1/USDT:USDT", dates)
            _write_tape(root, "A2/USDT:USDT", dates.drop(dates[7]))
            timeline = windows.common_timeline(
                _items(root), data_root=str(root), timeframe="1d"
            )
        self.assertEqual(timeline["n_files"], 3)
        self.assertEqual(timeline["n_common"], 39)
        self.assertEqual(timeline["last"], "2026-12-23")

    def test_other_timeframes_and_streams_are_ignored(self):
        dates = pd.date_range("2026-11-14", periods=10, freq="D")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tape(root, "A0/USDT:USDT", dates)
            items = _items(root) + [
                {"symbol": "S", "exchange": "binance", "trade_type": "futures",
                 "timeframe": "4h"},
                {"symbol": "S", "exchange": "binance", "trade_type": "futures",
                 "timeframe": "1d", "stream": "funding_rate"},
            ]
            timeline = windows.common_timeline(
                items, data_root=str(root), timeframe="1d"
            )
        self.assertEqual(timeline["n_files"], 1)


class TestPlanWindows(unittest.TestCase):
    spec = {
        "label": "rolling", "rolling": True, "start": "auto",
        "timeframe": "1d", "window_bars": 30, "embargo_bars": 5,
    }

    def _plan(self, dates, revealed=None, **spec_overrides):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for i in range(3):
            _write_tape(root, f"A{i}/USDT:USDT", dates)
        spec = {**self.spec, **spec_overrides}
        plan = windows.plan_windows(
            spec, data_root=str(root), items=_items(root),
            revealed=revealed if revealed is not None else [
                {"start": "2025-07-01", "end": "2026-11-13"}],
            today=datetime.date(2027, 2, 1),
        )
        tmp.cleanup()
        return plan

    def test_two_full_windows_plus_a_partial_one(self):
        dates = pd.date_range("2026-11-14", periods=80, freq="D")
        plan = self._plan(dates)
        self.assertEqual(plan["anchor"]["start"], "2026-11-14")
        self.assertEqual(plan["n_common_bars"], 80)
        self.assertEqual(len(plan["windows"]), 3)
        self.assertEqual(plan["n_ready"], 2)
        w1, w2, w3 = plan["windows"]
        self.assertEqual(w1["start"], "2026-11-14")
        self.assertEqual(w1["end"], "2026-12-13")
        self.assertEqual(w1["n_bars"], 30)
        # 5-bar embargo between the end of w1 and the start of w2.
        self.assertEqual(
            (pd.Timestamp(w2["start"]) - pd.Timestamp(w1["end"])).days, 6
        )
        self.assertFalse(w3["ready"])
        self.assertEqual(w3["n_bars"], 10)
        self.assertEqual(plan["current"]["accumulated"], 10)
        self.assertEqual(plan["current"]["remaining"], 20)
        self.assertEqual(plan["current"]["rolling"], True)

    def test_ready_unrevealed_window_is_surfaced(self):
        dates = pd.date_range("2026-11-14", periods=80, freq="D")
        revealed = [
            {"start": "2025-07-01", "end": "2026-11-13"},
            {"start": "2026-11-14", "end": "2026-12-13",
             "period_key": "rolling_2026-11-14_2026-12-13"},
        ]
        plan = self._plan(dates, revealed=revealed)
        self.assertEqual(plan["anchor"]["start"], "2026-11-14")
        self.assertEqual(plan["n_revealed"], 1)
        self.assertIsNotNone(plan["next_ready_unrevealed"])
        self.assertEqual(plan["next_ready_unrevealed"]["start"], "2026-12-19")

    def test_gap_reduces_progress_instead_of_calendar_days(self):
        dates = pd.date_range("2026-11-14", periods=80, freq="D")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tape(root, "A0/USDT:USDT", dates)
            _write_tape(root, "A1/USDT:USDT", dates)
            _write_tape(root, "A2/USDT:USDT", dates.drop(dates[3]))
            plan = windows.plan_windows(
                self.spec, data_root=str(root), items=_items(root),
                revealed=[{"start": "2025-07-01", "end": "2026-11-13"}],
                today=datetime.date(2027, 2, 1),
            )
        # 79 real bars, not 80 calendar days; the first window ends one day
        # later because that bar is absent from one asset.
        self.assertEqual(plan["n_common_bars"], 79)
        self.assertNotEqual(plan["windows"][0]["end"], "2026-12-13")

    def test_no_new_bars_yields_zero_progress(self):
        dates = pd.date_range("2026-01-01", periods=30, freq="D")
        plan = self._plan(dates)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["current"]["accumulated"], 0)
        self.assertFalse(plan["current"]["ready"])


class TestIntegration(unittest.TestCase):
    def test_freshness_delegates_to_rolling_planner(self):
        dates = pd.date_range("2026-11-14", periods=35, freq="D")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(2):
                _write_tape(root, f"A{i}/USDT:USDT", dates)
            with patch.object(windows, "revealed_windows", return_value=[
                {"start": "2025-07-01", "end": "2026-11-13"}]):
                progress = experiment_progress(
                    {"rolling": True, "start": "auto", "window_bars": 30,
                     "embargo_bars": 5, "timeframe": "1d"},
                    _items(root, 2), data_root=str(root),
                    today=datetime.date(2026, 12, 20),
                )
        self.assertTrue(progress["rolling"])
        self.assertEqual(progress["anchor"]["start"], "2026-11-14")
        # 35 bars = one 30-bar window + a 5-bar embargo; the next window is
        # accumulating at 0 rather than reporting the ready one as "current".
        self.assertEqual(len(progress["rolling_windows"]), 2)
        self.assertTrue(progress["rolling_windows"][0]["ready"])
        self.assertFalse(progress["rolling_windows"][1]["ready"])
        self.assertEqual(progress["accumulated"], 0)

    def test_legacy_spec_is_unchanged(self):
        progress = experiment_progress(
            {"label": "x", "start": "2026-12-01", "min_bars": 90,
             "timeframe": "1d"},
            [{"symbol": "A", "timeframe": "1d", "last": "2026-12-31"}],
            today=datetime.date(2027, 1, 1),
        )
        self.assertEqual(progress["accumulated"], 31)
        self.assertNotIn("rolling", progress)

    def test_committed_spec_is_rolling_and_auto_anchored(self):
        spec = json.loads(windows.spec_path().read_text(encoding="utf-8"))
        self.assertTrue(spec["rolling"])
        self.assertEqual(spec["start"], "auto")
        self.assertEqual(spec["window_bars"], 30)
        self.assertEqual(spec["embargo_bars"], 5)

    def test_status_returns_a_plan(self):
        with patch.object(windows, "revealed_windows", return_value=[
            {"start": "2025-07-01", "end": "2026-11-13"}]):
            out = windows.status()
        self.assertIn("current", out)
        self.assertEqual(out["anchor"]["start"], "2026-11-14")

    def test_holdout_api_exposes_rolling(self):
        import asyncio

        from api.main import get_holdout_status

        with patch("core.experiment.windows.revealed_windows", return_value=[]):
            out = asyncio.run(get_holdout_status())
        self.assertIn("rolling", out)
        self.assertIn("current", out["rolling"])


if __name__ == "__main__":
    unittest.main()
