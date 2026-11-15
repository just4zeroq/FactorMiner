"""Downloader write-path contracts (review P0-b / P0-c).

Two failures used to be invisible from the outside:

* ``download_ohlcv_batch`` returned ``success: True`` even when
  ``_save_data_with_merge`` failed, and the merge path "rescued" itself by
  writing ``<name>-new-<ts>.feather`` — a file nothing reads. The unattended
  updater advances its watermark on success, so a run could look clean while
  every bar was stale.
* ``get_exchange_instance()`` mutated ``self.exchange_id`` to ``binance`` when
  called without arguments (gap filling does exactly that), so a non-binance
  download merged its bars into ``data/binance/...``.

These tests pin both contracts without touching the network.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from core.data_feed.batch_downloader import SmartBatchDownloader
from core.data_feed.data_downloader import DataDownloader


def _frame(dates):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": [1.0] * len(dates),
            "high": [1.1] * len(dates),
            "low": [0.9] * len(dates),
            "close": [1.05] * len(dates),
            "volume": [10.0] * len(dates),
        }
    )


class SavePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.dl = SmartBatchDownloader(data_root=self.root)
        self.dl.exchange_id = "binance"
        self.dl.trade_type = "futures"

    def tearDown(self):
        self._tmp.cleanup()

    def test_corrupt_existing_file_fails_closed_and_writes_nothing(self):
        path = os.path.join(self.root, "binance", "futures")
        os.makedirs(path, exist_ok=True)
        target = os.path.join(path, "BTC_USDT_USDT-1d-futures.feather")
        with open(target, "wb") as handle:
            handle.write(b"not a feather file")
        before = os.path.getsize(target)

        result = self.dl._save_data_with_merge(
            _frame(["2024-01-01", "2024-01-02"]), "BTC/USDT:USDT", "1d",
            "2024-01-01", "2024-01-02",
        )

        self.assertFalse(result["success"])
        self.assertIn("现有文件", result["error"])
        # The unreadable file is left for the operator, and no orphan appears.
        self.assertEqual(os.path.getsize(target), before)
        self.assertEqual(
            [f for f in os.listdir(path) if "-new-" in f], [],
            "must not create <name>-new-<ts>.feather orphans",
        )

    def test_missing_file_is_written_to_the_canonical_path(self):
        result = self.dl._save_data_with_merge(
            _frame(["2024-01-01", "2024-01-02"]), "BTC/USDT:USDT", "1d",
            "2024-01-01", "2024-01-02",
        )
        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.exists(result["file_path"]))
        self.assertIn(os.path.join("binance", "futures"), result["file_path"])


class SuccessPropagationTests(unittest.TestCase):
    """download_ohlcv_batch must report the save result, not assume success."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dl = SmartBatchDownloader(data_root=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, save_result):
        page = {
            "candles": [[1704067200000, 1.0, 1.1, 0.9, 1.05, 10.0]],
            "batches": 1,
            "complete": True,
            "stopped": None,
            "errors": [],
        }
        frame = _frame(["2024-01-01"])

        class _Exchange:
            @staticmethod
            def parse_timeframe(_timeframe):
                return 86400

        with mock.patch.object(self.dl, "get_exchange_instance", return_value=_Exchange()), \
                mock.patch("core.data_feed.batch_downloader.paginate_ohlcv", return_value=page), \
                mock.patch.object(self.dl, "_merge_with_existing_data", return_value=frame), \
                mock.patch.object(self.dl, "_fill_data_gaps", return_value=frame), \
                mock.patch.object(self.dl, "_final_validation", return_value=True), \
                mock.patch.object(self.dl, "_save_data_with_merge", return_value=save_result):
            return self.dl.download_ohlcv_batch(
                exchange_id="binance",
                symbol="BTC/USDT:USDT",
                timeframe="1d",
                start_date="2024-01-01",
                end_date="2024-01-01",
                trade_type="futures",
            )

    def test_save_failure_becomes_download_failure(self):
        out = self._run({"success": False, "error": "磁盘已满"})
        self.assertFalse(out["success"])
        self.assertIn("磁盘已满", out["error"])

    def test_save_success_is_reported_with_file_path(self):
        out = self._run({"success": True, "message": "ok", "file_path": "/tmp/x.feather"})
        self.assertTrue(out["success"])
        self.assertEqual(out["file_path"], "/tmp/x.feather")


class ExchangeIdTests(unittest.TestCase):
    """A client handle must never silently retarget the write path."""

    def test_gap_fill_client_does_not_rewrite_exchange_id(self):
        dl = DataDownloader(data_root=tempfile.gettempdir())
        dl.exchange_id = "okx"
        with mock.patch.object(dl, "get_exchange_instance", wraps=dl.get_exchange_instance):
            exchange = dl.get_exchange_instance()
        self.assertIsNotNone(exchange)
        self.assertEqual(
            dl.exchange_id, "okx",
            "get_exchange_instance() must not redirect the save path to binance",
        )

    def test_explicit_exchange_is_honoured_without_touching_state(self):
        dl = DataDownloader(data_root=tempfile.gettempdir())
        dl.exchange_id = "okx"
        exchange = dl.get_exchange_instance(exchange_id="binance")
        self.assertIsNotNone(exchange)
        # The returned client is binance, but the instance still points at its
        # own library root; callers that want to switch must set it explicitly.
        self.assertEqual(dl.exchange_id, "okx")


if __name__ == "__main__":
    unittest.main()
