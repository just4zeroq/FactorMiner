"""Regression tests for cursor-based OHLCV pagination (D-01 / D-02).

Old behaviour: advance by a 30-day wall-clock window while asking for at most
1000 candles -> 1m data lost ~29 days per batch; empty responses did not
advance the cursor at all (infinite loop); a failed batch still produced a
"100/100 complete" file.
"""

from __future__ import annotations

import unittest

from core.data_feed.pagination import paginate_ohlcv

MINUTE_MS = 60_000


def _candle(ts: int):
    return [ts, 100.0, 101.0, 99.0, 100.5, 10.0]


class FakeExchange:
    """Minimal CCXT stub: returns up to ``limit`` consecutive bars from since."""

    def __init__(self, count: int, *, timeframe_ms: int = MINUTE_MS,
                 mode: str = "normal", fail_times: int = 0):
        self.candles = [_candle(i * timeframe_ms) for i in range(count)]
        self.timeframe_ms = timeframe_ms
        self.mode = mode
        self.fail_times = fail_times
        self.calls: list[tuple[int, int]] = []

    @property
    def rateLimit(self):
        return 0

    def parse_timeframe(self, timeframe):
        return self.timeframe_ms / 1000.0

    def fetch_ohlcv(self, symbol, timeframe, since, limit=None):
        self.calls.append((since, limit))
        if limit is not None:
            assert limit >= 1, "limit must never be 0 (old dynamic_limit bug)"
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated network failure")
        if self.mode == "empty":
            return []
        if self.mode == "error":
            raise RuntimeError("permanent failure")
        if self.mode == "stall":
            # Only candles *before* the cursor -> no forward progress.
            return [[since - self.timeframe_ms, 1, 1, 1, 1, 1]]
        return [c for c in self.candles if c[0] >= since][:limit]


class TestPagination(unittest.TestCase):
    def setUp(self):
        self.until_ms = 4320 * MINUTE_MS  # 3 days of 1m bars

    def test_advances_on_last_returned_timestamp_without_gaps(self):
        ex = FakeExchange(4320)
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, self.until_ms,
                                timeframe_ms=MINUTE_MS, limit=1000)
        self.assertTrue(result["complete"], result["errors"])
        self.assertIsNone(result["stopped"])
        self.assertEqual(len(result["candles"]), 4320)
        self.assertEqual(result["batches"], 5)
        # Each new cursor is exactly one bar after the previous batch's end.
        self.assertEqual(ex.calls[0][0], 0)
        self.assertEqual(ex.calls[1][0], 1000 * MINUTE_MS)
        self.assertEqual(ex.calls[2][0], 2000 * MINUTE_MS)

    def test_old_wall_clock_scheme_would_have_lost_data(self):
        # Pin the failure mode: with limit=1000 a 30-day window covers only
        # 16.7 hours of 1m bars. The cursor helper covers the full span.
        n_bars = 30 * 24 * 60
        ex = FakeExchange(n_bars)
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, n_bars * MINUTE_MS,
                                timeframe_ms=MINUTE_MS, limit=1000)
        self.assertEqual(len(result["candles"]), n_bars)
        self.assertTrue(result["complete"], result["errors"])
        self.assertGreater(result["batches"], 40)

    def test_empty_responses_are_bounded_not_infinite(self):
        ex = FakeExchange(0, mode="empty")
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, self.until_ms,
                                timeframe_ms=MINUTE_MS, limit=1000)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped"], "no_data")
        self.assertLessEqual(result["batches"], 3)
        # Cursor must still have moved.
        self.assertGreater(result["cursor"], 0)

    def test_failing_batch_is_reported_not_saved_as_success(self):
        ex = FakeExchange(4320, mode="error")
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, self.until_ms,
                                timeframe_ms=MINUTE_MS, limit=1000)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped"], "error")
        self.assertTrue(result["errors"])

    def test_retry_can_recover(self):
        ex = FakeExchange(3, fail_times=1)
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, 3 * MINUTE_MS,
                                timeframe_ms=MINUTE_MS, limit=1000,
                                retry_attempts=2)
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(len(result["candles"]), 3)

    def test_stalled_cursor_is_detected(self):
        ex = FakeExchange(10, mode="stall")
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, 10 * MINUTE_MS,
                                timeframe_ms=MINUTE_MS, limit=1000)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped"], "stalled")

    def test_batch_cap_prevents_a_runaway_loop(self):
        ex = FakeExchange(4320)
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, self.until_ms,
                                timeframe_ms=MINUTE_MS, limit=10, max_batches=2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped"], "batch_limit")
        self.assertEqual(result["batches"], 2)

    def test_zero_limit_is_clamped(self):
        ex = FakeExchange(3)
        result = paginate_ohlcv(ex, "BTC/USDT", "1m", 0, 3 * MINUTE_MS,
                                timeframe_ms=MINUTE_MS, limit=0)
        self.assertTrue(result["complete"])
        self.assertEqual(ex.calls[0][1], 1)


if __name__ == "__main__":
    unittest.main()
