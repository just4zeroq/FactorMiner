"""Regression tests for the layer-0 "tell the truth" fixes.

The full module review (``user_workspace/reports/full_code_review_20261115.md``)
found that several entry points reported success even when nothing had been
done or when the underlying command had failed. These tests pin the corrected
behaviour:

* ``core/cli.py`` must turn command return values into real shell exit codes
  and must not hard-code deleted factor ids.
* ``core/commands/download.py`` must surface ``{'success': False}`` payloads.
* ``core/data_feed/gap_filler.py`` must never report simulated gap repairs.
* ``core/data_feed/health_checker.py`` must actually count invalid OHLC bars.
* ``core/data_feed/processor.py`` must not mix index labels with ``.iloc``.
* ``setup.py`` / ``configRL.json`` must use the documented dependency/key names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from core import cli
from core.commands import download as download_cmd
from core.data_feed.gap_filler import DataGapFiller
from core.data_feed.health_checker import DataHealthChecker
from core.data_feed.processor import data_processor

ROOT = Path(__file__).resolve().parents[1]


class TestCliExitCodes(unittest.TestCase):
    def test_exit_code_normalisation(self):
        self.assertEqual(cli._exit_code(None), 0)
        self.assertEqual(cli._exit_code(0), 0)
        self.assertEqual(cli._exit_code(2), 2)
        self.assertEqual(cli._exit_code(True), 0)
        self.assertEqual(cli._exit_code(False), 1)
        self.assertEqual(cli._exit_code({"success": False}), 1)
        self.assertEqual(cli._exit_code({"ok": False}), 1)
        self.assertEqual(cli._exit_code({"error": "boom"}), 1)
        self.assertEqual(cli._exit_code({"state": {"status": "failed"}}), 1)
        self.assertEqual(cli._exit_code({"run_id": "abc", "state": {"status": "completed"}}), 0)

    def test_backtest_failure_propagates_to_exit_code(self):
        with patch("core.commands.backtest.run_backtest_cmd", return_value=1):
            self.assertEqual(cli.main(["backtest"]), 1)
        with patch("core.commands.backtest.run_backtest_cmd", return_value=0):
            self.assertEqual(cli.main(["backtest"]), 0)

    def test_download_failure_propagates_to_exit_code(self):
        async def failing(_args):
            return 1

        with patch.object(download_cmd, "run_downloader", failing):
            self.assertEqual(cli.main(["download", "--symbols", "BTC/USDT",
                                       "--start", "2024-01-01", "--end", "2024-01-02"]), 1)

    def test_missing_command_is_not_success(self):
        self.assertEqual(cli.main([]), 2)

    def test_backtest_factors_default_is_discovery(self):
        args = cli.setup_parser().parse_args(["backtest"])
        self.assertEqual(args.factors, "")
        args = cli.setup_parser().parse_args(["backtest", "--factors", "fac_a,fac_b"])
        self.assertEqual(args.factors, "fac_a,fac_b")


class TestDownloadExitCode(unittest.TestCase):
    @staticmethod
    def _args():
        return argparse.Namespace(
            exchange="binance", symbols="BTC/USDT,ETH/USDT", timeframes="1d",
            type="spot", start="2024-01-01", end="2024-01-02", mode="full",
        )

    def test_failure_payload_returns_one(self):
        with patch("core.commands.download._make_downloader") as cls:
            cls.return_value.download_ohlcv_batch.return_value = {
                "success": False, "error": "no candles",
            }
            self.assertEqual(asyncio.run(download_cmd.run_downloader(self._args())), 1)

    def test_partial_failure_returns_one(self):
        with patch("core.commands.download._make_downloader") as cls:
            cls.return_value.download_ohlcv_batch.side_effect = [
                {"success": True}, {"success": False, "error": "timeout"},
            ]
            self.assertEqual(asyncio.run(download_cmd.run_downloader(self._args())), 1)

    def test_exception_returns_one(self):
        with patch("core.commands.download._make_downloader") as cls:
            cls.return_value.download_ohlcv_batch.side_effect = RuntimeError("network down")
            self.assertEqual(asyncio.run(download_cmd.run_downloader(self._args())), 1)

    def test_all_success_returns_zero(self):
        with patch("core.commands.download._make_downloader") as cls:
            cls.return_value.download_ohlcv_batch.return_value = {"success": True}
            self.assertEqual(asyncio.run(download_cmd.run_downloader(self._args())), 0)


class TestGapFillerHonesty(unittest.TestCase):
    def setUp(self):
        self.filler = DataGapFiller()
        self.tasks = [
            {"symbol": "BTC", "timeframe": "1h"},
            {"symbol": "ETH", "timeframe": "1h"},
        ]

    def test_no_tasks_is_a_vacuous_success(self):
        result = self.filler.execute_fill_tasks([])
        self.assertTrue(result["success"])
        self.assertEqual(result["executed_count"], 0)

    def test_without_downloader_it_refuses_to_fake_success(self):
        result = self.filler.execute_fill_tasks(self.tasks)
        self.assertFalse(result["success"])
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failed_count"], len(self.tasks))
        self.assertEqual(result["executed_count"], 0)

    def test_real_downloader_outcomes_are_counted(self):
        outcomes = [True, {"success": False, "error": "boom"}]

        def downloader(task):
            return outcomes.pop(0)

        result = self.filler.execute_fill_tasks(self.tasks, downloader=downloader)
        self.assertFalse(result["success"])
        self.assertEqual(result["executed_count"], 2)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("boom", " ".join(result["errors"]))

    def test_all_real_successes_report_success(self):
        result = self.filler.execute_fill_tasks(
            self.tasks, downloader=lambda task: {"success": True}
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failed_count"], 0)

    def test_auto_fill_propagates_execution_failure(self):
        with patch.object(self.filler, "scan_for_gaps", return_value=[{"gap": 1}]), \
             patch.object(self.filler, "create_fill_tasks", return_value=self.tasks):
            result = self.filler.auto_fill_gaps("BTC", "1h", data_dir="does-not-exist")
        self.assertFalse(result["success"])
        self.assertEqual(result["gaps_found"], 1)
        self.assertEqual(result["tasks_created"], 2)


class TestHealthChecker(unittest.TestCase):
    def test_invalid_ohlc_is_actually_counted(self):
        n = 20
        df = pd.DataFrame({
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.0] * n,
            "volume": [1.0] * n,
        })
        df.loc[5, "open"] = 20.0  # open > high -> logically impossible bar
        report = DataHealthChecker()._check_data_quality(df)
        text = " ".join(report["issues"] + report["warnings"])
        self.assertIn("OHLC", text)
        self.assertIn("1 条", text)

    def test_valid_ohlc_has_no_warning(self):
        df = pd.DataFrame({
            "open": [10.0] * 5,
            "high": [11.0] * 5,
            "low": [9.0] * 5,
            "close": [10.0] * 5,
            "volume": [1.0] * 5,
        })
        report = DataHealthChecker()._check_data_quality(df)
        self.assertNotIn("OHLC", " ".join(report["issues"] + report["warnings"]))


class TestGapDetectionIndex(unittest.TestCase):
    def test_non_zero_index_labels_do_not_corrupt_boundaries(self):
        dates = pd.date_range("2024-01-01", periods=5, freq="h").append(
            pd.date_range("2024-01-01 10:00", periods=2, freq="h")
        )
        df = pd.DataFrame({"date": dates, "close": np.arange(len(dates), dtype=float)})
        df.index = np.arange(100, 100 + len(df))
        gaps = data_processor.detect_gaps(df, "1h")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(pd.Timestamp(gaps[0]["gap_start"]), pd.Timestamp("2024-01-01 04:00:00"))
        self.assertEqual(pd.Timestamp(gaps[0]["gap_end"]), pd.Timestamp("2024-01-01 10:00:00"))


class TestConfigAndPackaging(unittest.TestCase):
    def test_rl_config_uses_the_key_the_miner_reads(self):
        cfg = json.loads(
            (ROOT / "user_workspace" / "configs" / "configRL.json").read_text(encoding="utf-8-sig")
        )
        self.assertIn("rl", cfg)
        self.assertNotIn("rl_config", cfg)
        self.assertEqual(cfg["rl"]["learning_rate"], 0.5)

    def test_setup_uses_requirements_as_single_source(self):
        text = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn("requirements.txt", text)
        self.assertNotIn("ml4t-backtest>=0.1.0", text)


if __name__ == "__main__":
    unittest.main()
