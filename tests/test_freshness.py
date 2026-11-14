"""Data freshness and pre-registered window progress.

Two failure modes this guards against:

* A handful of symbols silently freezing two months back while the rest of the
  library looks current — the old per-symbol coverage endpoint could not see it.
* Starting the P0.5 forward test on a window that is not actually complete, or
  worse, peeking at one that has not fully accumulated.
"""

import datetime
import os
import shutil
import tempfile
import unittest

import pandas as pd

from core.data_feed.freshness import (
    annotate_freshness,
    display_symbol,
    experiment_progress,
    freshness_report,
    scan_data_root,
)


def _write_feather(root, exchange, trade_type, filename, dates):
    directory = os.path.join(root, exchange, trade_type)
    os.makedirs(directory, exist_ok=True)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "close": [100.0 + i for i in range(len(dates))],
        }
    )
    frame.to_feather(os.path.join(directory, filename))


class DisplaySymbolTests(unittest.TestCase):
    def test_futures_keeps_settlement_suffix(self):
        self.assertEqual(display_symbol("BTC_USDT_USDT", "futures"), "BTC/USDT:USDT")

    def test_spot_is_a_plain_pair(self):
        self.assertEqual(display_symbol("BTC_USDT", "spot"), "BTC/USDT")

    def test_multi_token_base_survives(self):
        self.assertEqual(display_symbol("1000_PEPE_USDT", "spot"), "1000_PEPE/USDT")

    def test_single_token_is_returned_as_is(self):
        self.assertEqual(display_symbol("BTC", "spot"), "BTC")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _write_feather(
            self.root,
            "binance",
            "spot",
            "BTC_USDT-1d-spot.feather",
            pd.date_range("2024-01-01", periods=3, freq="D"),
        )
        _write_feather(
            self.root,
            "binance",
            "futures",
            "ETH_USDT_USDT-1d-futures.feather",
            pd.date_range("2024-01-01", periods=5, freq="D"),
        )
        _write_feather(
            self.root,
            "binance",
            "futures",
            "ETH_USDT_USDT-4h-futures.feather",
            pd.date_range("2024-01-01", periods=7, freq="4h"),
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_every_canonical_file_is_listed(self):
        items = scan_data_root(self.root)
        self.assertEqual(len(items), 3)
        symbols = sorted(item["symbol"] for item in items)
        self.assertEqual(symbols, ["BTC/USDT", "ETH/USDT:USDT", "ETH/USDT:USDT"])

    def test_rows_and_range_come_from_the_file(self):
        items = {item["timeframe"]: item for item in scan_data_root(self.root) if item["trade_type"] == "futures"}
        self.assertEqual(items["1d"]["rows"], 5)
        self.assertEqual(items["1d"]["start"], "2024-01-01")
        self.assertEqual(items["1d"]["last"], "2024-01-05")

    def test_non_canonical_files_are_ignored(self):
        _write_feather(
            self.root, "binance", "spot", "legacy-close.feather", pd.date_range("2024-01-01", periods=2)
        )
        open(os.path.join(self.root, "binance", "spot", "notes.txt"), "w").close()
        self.assertEqual(len(scan_data_root(self.root)), 3)

    def test_missing_root_is_empty_not_an_error(self):
        self.assertEqual(scan_data_root(os.path.join(self.root, "nope")), [])


class StalenessTests(unittest.TestCase):
    def _item(self, timeframe, last):
        return {"symbol": "BTC/USDT", "timeframe": timeframe, "last": last}

    def test_daily_file_with_two_days_of_lag_is_still_current(self):
        items = annotate_freshness(
            [self._item("1d", "2026-11-12")], today=datetime.date(2026, 11, 14)
        )
        self.assertFalse(items[0]["stale"])
        self.assertEqual(items[0]["lag_days"], 2)

    def test_daily_file_three_days_back_is_stale(self):
        items = annotate_freshness(
            [self._item("1d", "2026-11-11")], today=datetime.date(2026, 11, 14)
        )
        self.assertTrue(items[0]["stale"])
        self.assertEqual(items[0]["stale_after_days"], 2)

    def test_intraday_is_held_to_a_tighter_limit(self):
        items = annotate_freshness(
            [self._item("1h", "2026-11-12")], today=datetime.date(2026, 11, 14)
        )
        self.assertTrue(items[0]["stale"])

    def test_threshold_can_be_overridden(self):
        items = annotate_freshness(
            [self._item("1d", "2026-11-12")],
            today=datetime.date(2026, 11, 14),
            stale_after_days={"1d": 10},
        )
        self.assertFalse(items[0]["stale"])

    def test_private_timestamp_field_is_not_leaked(self):
        items = annotate_freshness(
            [{**self._item("1d", "2026-11-12"), "_last_ts": pd.Timestamp("2026-11-12")}],
            today=datetime.date(2026, 11, 14),
        )
        self.assertNotIn("_last_ts", items[0])


class ExperimentProgressTests(unittest.TestCase):
    spec = {"label": "P0.5 forward", "start": "2026-12-01", "min_bars": 90, "timeframe": "1d"}

    def _items(self, *lasts):
        return [
            {"symbol": f"A{i}", "timeframe": "1d", "last": last} for i, last in enumerate(lasts)
        ]

    def test_no_spec_means_no_progress_block(self):
        self.assertIsNone(experiment_progress(None, self._items("2026-11-13")))

    def test_window_has_not_opened(self):
        progress = experiment_progress(
            self.spec, self._items("2026-11-13"), today=datetime.date(2026, 11, 14)
        )
        self.assertEqual(progress["accumulated"], 0)
        self.assertFalse(progress["ready"])
        self.assertEqual(progress["coverage"], 0)
        self.assertEqual(progress["eta"], "2027-02-28")

    def test_partial_window_counts_from_start(self):
        progress = experiment_progress(
            self.spec, self._items("2026-12-31"), today=datetime.date(2027, 1, 1)
        )
        self.assertEqual(progress["accumulated"], 31)
        self.assertEqual(progress["remaining"], 59)
        self.assertFalse(progress["ready"])
        self.assertAlmostEqual(progress["coverage"], 0.3444, places=4)

    def test_window_is_ready_exactly_at_min_bars(self):
        progress = experiment_progress(
            self.spec, self._items("2027-02-28"), today=datetime.date(2027, 3, 1)
        )
        self.assertEqual(progress["accumulated"], 90)
        self.assertTrue(progress["ready"])
        self.assertEqual(progress["remaining"], 0)
        self.assertEqual(progress["coverage"], 1.0)

    def test_the_shortest_asset_decides(self):
        progress = experiment_progress(
            self.spec,
            self._items("2027-03-15", "2026-12-10"),
            today=datetime.date(2027, 3, 16),
        )
        self.assertEqual(progress["accumulated"], 10)
        self.assertFalse(progress["ready"])

    def test_other_timeframes_are_ignored(self):
        items = [{"symbol": "A", "timeframe": "4h", "last": "2027-05-01"}]
        progress = experiment_progress(self.spec, items, today=datetime.date(2027, 5, 2))
        self.assertEqual(progress["accumulated"], 0)
        self.assertIn("no data", progress["note"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _write_feather(
            self.root,
            "binance",
            "spot",
            "BTC_USDT-1d-spot.feather",
            pd.date_range("2024-01-01", periods=3, freq="D"),
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_report_shape_and_summary(self):
        report = freshness_report(
            self.root,
            experiment_spec={"label": "x", "start": "2024-01-01", "min_bars": 2, "timeframe": "1d"},
        )
        self.assertEqual(report["summary"]["files"], 1)
        self.assertEqual(report["summary"]["assets_1d"], 1)
        self.assertEqual(report["summary"]["latest"], "2024-01-03")
        self.assertEqual(report["summary"]["stale"], 1)
        self.assertEqual(report["summary"]["stale_symbols"], ["BTC/USDT"])
        self.assertEqual(report["summary"]["timeframes"], {"1d": 1})
        self.assertEqual(report["summary"]["stale_by_timeframe"], {"1d": 1})
        self.assertTrue(report["experiment"]["ready"])

    def test_stale_symbols_are_deduplicated_across_timeframes(self):
        _write_feather(
            self.root,
            "binance",
            "spot",
            "BTC_USDT-4h-spot.feather",
            pd.date_range("2024-01-01", periods=4, freq="4h"),
        )
        report = freshness_report(self.root)
        self.assertEqual(report["summary"]["stale"], 2)
        self.assertEqual(report["summary"]["stale_symbols"], ["BTC/USDT"])
        self.assertEqual(report["summary"]["timeframes"], {"1d": 1, "4h": 1})
        self.assertEqual(report["summary"]["stale_by_timeframe"], {"1d": 1, "4h": 1})

    def test_fresh_library_reports_no_stale(self):
        future = datetime.date.today() + datetime.timedelta(days=1)
        _write_feather(
            self.root,
            "binance",
            "spot",
            "SOL_USDT-1d-spot.feather",
            pd.date_range(future - datetime.timedelta(days=3), periods=3, freq="D"),
        )
        report = freshness_report(self.root)
        newer = [item for item in report["items"] if item["symbol"] == "SOL/USDT"]
        self.assertFalse(newer[0]["stale"])

    def test_empty_library_reports_zeroes(self):
        report = freshness_report(os.path.join(self.root, "missing"))
        self.assertEqual(report["summary"]["files"], 0)
        self.assertIsNone(report["summary"]["latest"])
        self.assertIsNone(report["experiment"])


if __name__ == "__main__":
    unittest.main()
