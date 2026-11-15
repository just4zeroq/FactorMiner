"""Regression tests for the Phase-2 data-causality fixes.

* **D-05** — the data layer must drop a still-forming (unclosed) trailing bar so
  mining never sees a half-built candle.
* **data_quality.strict** — every shipped config must declare it (13/13) so a
  broken panel fails loud instead of warning into a RankIC number.
"""

import glob
import json
import unittest

import pandas as pd

from core.data_feed.real_client import RealDataClient


def _client(timeframe="1d"):
    """Build a client without touching disk (skip __init__'s data load)."""
    client = RealDataClient.__new__(RealDataClient)
    client.exchange = "binance"
    client.timeframe = timeframe
    return client


class DropUnclosedBarTests(unittest.TestCase):
    def test_historical_frame_is_untouched(self):
        # All bars are long closed -> nothing dropped.
        idx = pd.date_range("2020-01-01", periods=10, freq="D")
        df = pd.DataFrame({"close": range(10)}, index=idx)
        out = _client()._drop_unclosed_last_bar(df)
        self.assertEqual(len(out), len(df))

    def test_forming_last_bar_is_dropped(self):
        # Build a daily series whose last bar starts "now" (closes in the
        # future), which is exactly the still-forming candle.
        now = pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()
        idx = pd.DatetimeIndex([now - pd.Timedelta(days=2),
                                now - pd.Timedelta(days=1),
                                now])  # last bar covers [now, now+1d) -> open
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
        out = _client("1d")._drop_unclosed_last_bar(df)
        self.assertEqual(len(out), 2)
        self.assertNotIn(now, out.index)

    def test_empty_and_nondatetime_are_safe(self):
        self.assertTrue(_client()._drop_unclosed_last_bar(pd.DataFrame()).empty)
        df = pd.DataFrame({"close": [1, 2]}, index=[0, 1])
        self.assertEqual(len(_client()._drop_unclosed_last_bar(df)), 2)


class ConfigStrictDeclaredTests(unittest.TestCase):
    def test_every_config_declares_data_quality_strict(self):
        files = sorted(glob.glob("user_workspace/configs/*.json"))
        self.assertTrue(files, "no config files found")
        missing = []
        for f in files:
            cfg = json.load(open(f, encoding="utf-8"))
            dq = cfg.get("data_quality")
            if not (isinstance(dq, dict) and dq.get("strict") is True):
                missing.append(f)
        self.assertEqual(missing, [], f"configs missing data_quality.strict=true: {missing}")


if __name__ == "__main__":
    unittest.main()
