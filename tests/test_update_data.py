"""Incremental update planning and tail trimming.

An unattended refresh job is only safe if two things hold: it knows exactly how
much is missing, and it never leaves a half-formed bar in the panel. The network
call itself is not exercised here — planning and trimming are the parts that
decide whether tomorrow's mining run sees clean data.
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_PROJECT_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts"))

import update_data  # noqa: E402


def _write_feather(root, exchange, trade_type, filename, dates):
    directory = os.path.join(root, exchange, trade_type)
    os.makedirs(directory, exist_ok=True)
    frame = pd.DataFrame({"date": pd.to_datetime(dates), "close": [1.0] * len(dates)})
    frame.to_feather(os.path.join(directory, filename))


def _universe(pairs=("BTC/USDT:USDT",), timeframe="1d", trade_type="futures"):
    return {
        "exchange": "binance",
        "trade_type": trade_type,
        "timeframe": timeframe,
        "pairs": list(pairs),
        "earliest_period": "2023-01-01",
    }


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.config = os.path.join(self.root, "config.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "data_feeds": {
                        "exchange": "okx",
                        "instrument_type": "spot",
                        "timeframe": "4h",
                        "pairs": ["BTC/USDT", "ETH/USDT"],
                        "mine_period": [["2023-04-01", "2023-12-31"]],
                        "test_period": [["2023-01-01", "2023-03-31"]],
                    }
                },
                handle,
            )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_universe_is_read_from_the_config(self):
        universe = update_data.load_universe(self.config)
        self.assertEqual(universe["exchange"], "okx")
        self.assertEqual(universe["trade_type"], "spot")
        self.assertEqual(universe["timeframe"], "4h")
        self.assertEqual(universe["pairs"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(universe["earliest_period"], "2023-01-01")

    def test_missing_config_is_a_clean_exit(self):
        with self.assertRaises(SystemExit):
            update_data.load_universe(os.path.join(self.root, "nope.json"))

    def test_config_without_pairs_is_a_clean_exit(self):
        empty = os.path.join(self.root, "empty.json")
        with open(empty, "w", encoding="utf-8") as handle:
            json.dump({"data_feeds": {"pairs": []}}, handle)
        with self.assertRaises(SystemExit):
            update_data.load_universe(empty)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _write_feather(
            self.root,
            "binance",
            "futures",
            "BTC_USDT_USDT-1d-futures.feather",
            pd.date_range("2024-01-01", periods=5, freq="D"),
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_current_symbol_is_skipped(self):
        plan = update_data.plan_updates(
            _universe(), datetime.date(2024, 1, 5), self.root
        )
        self.assertEqual(plan[0]["status"], "current")
        self.assertEqual(plan[0]["missing_days"], 0)

    def test_pending_symbol_starts_at_the_last_stored_bar(self):
        plan = update_data.plan_updates(
            _universe(), datetime.date(2024, 1, 9), self.root
        )
        record = plan[0]
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["missing_days"], 4)
        self.assertEqual(record["start"], "2024-01-05")
        # end is exclusive in the downloader loop, so it must overshoot by a day.
        self.assertEqual(record["end"], "2024-01-10")

    def test_unknown_symbol_uses_the_config_floor(self):
        plan = update_data.plan_updates(
            _universe(pairs=("ETH/USDT:USDT",)), datetime.date(2024, 1, 9), self.root
        )
        record = plan[0]
        self.assertEqual(record["status"], "new")
        self.assertEqual(record["start"], "2023-01-01")

    def test_other_timeframes_do_not_mask_a_missing_file(self):
        _write_feather(
            self.root,
            "binance",
            "futures",
            "BTC_USDT_USDT-4h-futures.feather",
            pd.date_range("2024-01-01", periods=5, freq="4h"),
        )
        plan = update_data.plan_updates(
            _universe(), datetime.date(2024, 1, 9), self.root
        )
        self.assertEqual(plan[0]["status"], "pending")
        self.assertEqual(plan[0]["last"], "2024-01-05")


class TrimTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, "bars.feather")
        pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=4, freq="D"),
                "close": [1.0, 2.0, 3.0, 4.0],
            }
        ).to_feather(self.path)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_partial_tail_is_removed(self):
        removed = update_data.trim_after(self.path, datetime.date(2024, 1, 2))
        self.assertEqual(removed, 2)
        frame = pd.read_feather(self.path)
        self.assertEqual(len(frame), 2)
        self.assertEqual(pd.to_datetime(frame["date"]).max(), pd.Timestamp("2024-01-02"))

    def test_nothing_to_trim_keeps_the_file_untouched(self):
        before = os.path.getmtime(self.path)
        self.assertEqual(update_data.trim_after(self.path, datetime.date(2024, 1, 4)), 0)
        self.assertEqual(os.path.getmtime(self.path), before)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(
            update_data.trim_after(os.path.join(self.root, "nope.feather"), datetime.date(2024, 1, 1)),
            0,
        )


class RunPlanTests(unittest.TestCase):
    """Regression: a symbol with no local file must actually be fetched.

    The first version skipped every status except ``pending``, so a brand-new
    symbol was reported as done without a single request being made.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.universe = _universe(pairs=("ETH/USDT:USDT",))
        self.until = datetime.date(2023, 1, 3)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _patch(self, fake):
        original = update_data._download_once
        update_data._download_once = fake
        self.addCleanup(setattr, update_data, "_download_once", original)

    def test_new_symbol_is_downloaded(self):
        calls = []

        def fake(record, exchange, data_root="data"):
            calls.append(record["symbol"])
            _write_feather(
                data_root,
                exchange,
                record["trade_type"],
                "ETH_USDT_USDT-1d-futures.feather",
                pd.date_range(record["start"], periods=3, freq="D"),
            )
            return {"success": True}

        self._patch(fake)
        plan = update_data.plan_updates(self.universe, self.until, self.root)
        results = update_data.run_plan(plan, self.universe, self.until, self.root, retries=1)
        self.assertEqual(calls, ["ETH/USDT:USDT"])
        self.assertEqual(results[0]["status"], "updated")
        self.assertEqual(results[0]["new_last"], "2023-01-03")

    def test_failure_is_retried_then_succeeds(self):
        attempts = {"count": 0}

        def flaky(record, exchange, data_root="data"):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise TimeoutError("exchange timed out")
            _write_feather(
                data_root,
                exchange,
                record["trade_type"],
                "ETH_USDT_USDT-1d-futures.feather",
                pd.date_range(record["start"], periods=3, freq="D"),
            )
            return {"success": True}

        self._patch(flaky)
        plan = update_data.plan_updates(self.universe, self.until, self.root)
        results = update_data.run_plan(
            plan, self.universe, self.until, self.root, retries=3, backoff=0
        )
        self.assertEqual(results[0]["status"], "updated")
        self.assertEqual(results[0]["attempts"], 3)

    def test_persistent_failure_is_reported_not_raised(self):
        def broken(record, exchange, data_root="data"):
            raise RuntimeError("network down")

        self._patch(broken)
        plan = update_data.plan_updates(self.universe, self.until, self.root)
        results = update_data.run_plan(
            plan, self.universe, self.until, self.root, retries=2, backoff=0
        )
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("network down", results[0]["error"])

    def test_short_download_is_flagged_partial(self):
        def short(record, exchange, data_root="data"):
            _write_feather(
                data_root,
                exchange,
                record["trade_type"],
                "ETH_USDT_USDT-1d-futures.feather",
                pd.date_range(record["start"], periods=1, freq="D"),
            )
            return {"success": True}

        self._patch(short)
        plan = update_data.plan_updates(self.universe, self.until, self.root)
        results = update_data.run_plan(plan, self.universe, self.until, self.root, retries=1)
        self.assertEqual(results[0]["status"], "partial")
        self.assertIn("2023-01-03", results[0]["error"])

    def test_symbols_already_current_are_not_downloaded(self):
        _write_feather(
            self.root,
            "binance",
            "futures",
            "ETH_USDT_USDT-1d-futures.feather",
            pd.date_range("2023-01-01", periods=3, freq="D"),
        )

        def never(record, exchange, data_root="data"):
            raise AssertionError("should not have been called")

        self._patch(never)
        plan = update_data.plan_updates(self.universe, self.until, self.root)
        results = update_data.run_plan(plan, self.universe, self.until, self.root, retries=1)
        self.assertEqual(results[0]["status"], "current")


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _write_feather(
            self.root,
            "binance",
            "futures",
            "BTC_USDT_USDT-1d-futures.feather",
            pd.date_range("2024-01-01", periods=3, freq="D"),
        )
        self.config = os.path.join(self.root, "config.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "data_feeds": {
                        "exchange": "binance",
                        "instrument_type": "futures",
                        "timeframe": "1d",
                        "pairs": ["BTC/USDT:USDT"],
                        "mine_period": [["2024-01-01", "2024-01-03"]],
                    }
                },
                handle,
            )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dry_run_exits_zero_and_writes_no_report(self):
        report_dir = os.path.join(self.root, "reports")
        code = update_data.main(
            [
                "--config",
                self.config,
                "--until",
                "2024-01-31",
                "--data-root",
                self.root,
                "--report-dir",
                report_dir,
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(report_dir))

    def test_unknown_config_exits_two(self):
        code = update_data.main(["--config", os.path.join(self.root, "nope.json"), "--dry-run"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
