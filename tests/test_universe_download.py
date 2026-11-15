"""Universe onboarding contracts (bulk discovery + config materialisation).

Two things must hold or a full-exchange mining run silently degrades:

* the forming daily bar never enters the library, including when a file was
  written by an older/manual run that left one behind — "already current" is
  not a reason to keep a partial candle;
* the mining config only lists symbols that actually have enough history in
  the mining window, because a market listed last week is not a
  cross-sectional bet and its NaN column makes every ``cs_*`` operator noisier.

Network is never touched here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _load(name: str, filename: str):
    module_name = f"_test_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_PROJECT_ROOT, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


bulk = _load("download_exchange_universe", "download_exchange_universe.py")
builder = _load("build_universe_config", "build_universe_config.py")


def _candles(dates):
    return [
        [int(pd.Timestamp(date).timestamp() * 1000), 1.0, 1.1, 0.9, 1.05, 10.0]
        for date in dates
    ]


class NormaliseTests(unittest.TestCase):
    def test_forming_bar_is_dropped(self):
        now = pd.Timestamp("2026-11-15 12:00:00")
        frame = bulk._normalise(
            _candles(["2026-11-13", "2026-11-14", "2026-11-15"]), "1d", now
        )
        self.assertEqual(list(frame["date"]), [pd.Timestamp("2026-11-13"), pd.Timestamp("2026-11-14")])

    def test_closed_tail_is_kept_and_columns_are_canonical(self):
        now = pd.Timestamp("2026-11-15 12:00:00")
        frame = bulk._normalise(_candles(["2026-11-13", "2026-11-14"]), "1d", now)
        self.assertEqual(
            list(frame.columns), ["date", "open", "high", "low", "close", "volume"]
        )
        self.assertEqual(frame["date"].iloc[-1], pd.Timestamp("2026-11-14"))

    def test_duplicate_timestamps_collapse(self):
        now = pd.Timestamp("2026-11-15 12:00:00")
        rows = _candles(["2026-11-13", "2026-11-13"])
        frame = bulk._normalise(rows, "1d", now)
        self.assertEqual(len(frame), 1)


class TrimTests(unittest.TestCase):
    def test_partial_bar_in_existing_file_is_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x-1d-futures.feather"
            pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-11-13", "2026-11-14", "2026-11-15"]),
                    "open": [1.0, 1.0, 1.0],
                    "high": [1.1, 1.1, 1.1],
                    "low": [0.9, 0.9, 0.9],
                    "close": [1.0, 1.0, 1.0],
                    "volume": [1.0, 1.0, 1.0],
                }
            ).to_feather(path)

            removed = bulk._trim_after(path, pd.Timestamp("2026-11-14").date())

            self.assertEqual(removed, 1)
            self.assertEqual(
                pd.to_datetime(pd.read_feather(path)["date"]).max(),
                pd.Timestamp("2026-11-14"),
            )


class BuildConfigTests(unittest.TestCase):
    def _write(self, root: str, symbol: str, trade_type: str, dates):
        from core.data_feed.naming import data_path

        path = data_path(root, "binance", symbol, "1d", trade_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "date": pd.to_datetime(dates),
                "open": [1.0] * len(dates),
                "high": [1.1] * len(dates),
                "low": [0.9] * len(dates),
                "close": [1.0] * len(dates),
                "volume": [1.0] * len(dates),
            }
        ).to_feather(path)

    def test_short_history_symbol_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = os.path.join(tmp, "data")
            self._write(
                data_root, "BTC/USDT:USDT", "futures",
                pd.date_range("2023-01-01", "2026-09-13", freq="D"),
            )
            self._write(data_root, "NEW/USDT:USDT", "futures", pd.date_range("2026-06-01", periods=20))

            universe = Path(tmp) / "u.json"
            universe.write_text(
                json.dumps(
                    {
                        "exchange": "binance",
                        "trade_type": "futures",
                        "timeframe": "1d",
                        "symbols": ["BTC/USDT:USDT", "NEW/USDT:USDT"],
                    }
                ),
                encoding="utf-8",
            )
            template = Path(tmp) / "t.json"
            template.write_text(json.dumps({"data_feeds": {"pairs": []}}), encoding="utf-8")
            out = Path(tmp) / "c.json"

            code = builder.main(
                [
                    "--universe", str(universe),
                    "--template", str(template),
                    "--out", str(out),
                    "--data-root", data_root,
                    "--mine-start", "2023-01-01",
                    "--mine-end", "2026-06-30",
                    "--test-start", "2026-07-01",
                    "--test-end", "2026-09-13",
                    "--min-bars", "250",
                ]
            )

            self.assertEqual(code, 0)
            config = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(config["data_feeds"]["pairs"], ["BTC/USDT:USDT"])
            self.assertEqual(config["data_feeds"]["instrument_type"], "futures")
            self.assertEqual(config["data_feeds"]["mine_period"], [["2023-01-01", "2026-06-30"]])
            # The audit is a report, not a config: it now lands in
            # <out.parent.parent>/reports/ so the configs directory contains
            # only runnable configs.
            audit = json.loads(
                (Path(tmp) / "reports" / "c.universe.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit["listed"], 2)
            self.assertEqual(audit["usable"], 1)


if __name__ == "__main__":
    unittest.main()
