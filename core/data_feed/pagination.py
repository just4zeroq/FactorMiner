"""Cursor-based OHLCV pagination shared by the downloaders.

Why this exists
---------------
Both downloaders used to advance by a *wall-clock* window (30 days / N days)
while asking CCXT for at most ``limit`` candles starting at that window's
beginning. For a 1m bar, 1000 candles is only 16.7 hours, so each 30-day
window lost ~29 days of data; the coverage score was then computed from the
first and last surviving timestamp and happily reported 100/100. Failures were
also swallowed: an exception or an empty batch just ``break``/``continue`` and
the caller still saved the file as a success.

The only correct cursor is the **last timestamp actually returned**, plus one
bar. Empty responses must still advance by a bounded amount, otherwise the
loop never terminates (``_download_missing_data`` had exactly this bug). Any
batch that stops early is reported back so the caller can fail instead of
writing a truncated file.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1000
MAX_EMPTY_BATCHES = 3


def paginate_ohlcv(
    exchange: Any,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    *,
    timeframe_ms: Optional[float] = None,
    limit: int = DEFAULT_LIMIT,
    retry_attempts: int = 0,
    delay_seconds: float = 0.0,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    max_empty_batches: int = MAX_EMPTY_BATCHES,
    max_batches: Optional[int] = None,
) -> dict:
    """Fetch every candle in ``[since_ms, until_ms)``.

    Returns a result dict: ``candles``, ``batches``, ``cursor``, ``complete``,
    ``stopped`` (None / 'error' / 'stalled' / 'no_data' / 'batch_limit') and
    ``errors``. ``complete`` is true only when the cursor reached ``until_ms``
    without an abnormal stop — callers must NOT save partial data otherwise.
    """
    if timeframe_ms is None:
        timeframe_ms = float(exchange.parse_timeframe(timeframe)) * 1000.0
    timeframe_ms = int(timeframe_ms)
    if timeframe_ms <= 0:
        raise ValueError("timeframe_ms must be positive")

    limit = max(1, int(limit))
    since_ms = int(since_ms)
    until_ms = int(until_ms)
    cursor = since_ms
    span = max(1, until_ms - since_ms)

    if max_batches is None:
        # Three times the theoretical number of full batches, plus headroom
        # for empty windows. Bounded so a misbehaving exchange cannot hang.
        max_batches = int(span / (limit * timeframe_ms)) * 3 + max_empty_batches + 10

    candles: list = []
    errors: list[str] = []
    batches = 0
    empty_streak = 0
    stopped: Optional[str] = None

    while cursor < until_ms:
        if batches >= max_batches:
            stopped = "batch_limit"
            errors.append(f"exceeded {max_batches} batches at cursor {cursor}")
            break
        batches += 1

        ohlcv = None
        for attempt in range(int(retry_attempts) + 1):
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe, cursor, limit=limit)
                break
            except Exception as exc:  # noqa: BLE001 - network errors are expected
                errors.append(f"batch {batches} attempt {attempt + 1}: {exc}")
                if attempt < retry_attempts and delay_seconds:
                    time.sleep(delay_seconds * 2)
        if ohlcv is None:
            stopped = "error"
            logger.error("OHLCV batch %s failed after %s attempts", batches, retry_attempts + 1)
            break

        if not ohlcv:
            empty_streak += 1
            if empty_streak >= max_empty_batches:
                stopped = "no_data"
                errors.append(
                    f"{empty_streak} consecutive empty batches at cursor {cursor}"
                )
                break
            # Bounded forward move: the exchange may list later than `since`,
            # but never leave the cursor unchanged (that was an infinite loop).
            cursor += limit * timeframe_ms
        else:
            empty_streak = 0
            rows = [row for row in ohlcv if int(row[0]) >= cursor]
            if not rows:
                stopped = "stalled"
                errors.append(f"batch {batches} returned only candles before the cursor")
                break
            candles.extend(rows)
            last_ms = max(int(row[0]) for row in rows)
            next_cursor = last_ms + timeframe_ms
            if next_cursor <= cursor:
                stopped = "stalled"
                errors.append(f"batch {batches} did not advance the cursor ({cursor})")
                break
            cursor = next_cursor

        if progress_callback:
            pct = min(100, max(0, int((cursor - since_ms) / span * 100)))
            progress_callback(pct, f"已下载 {len(candles)} 条数据...")
        if delay_seconds:
            time.sleep(delay_seconds)

    complete = stopped is None and cursor >= until_ms
    return {
        "candles": candles,
        "batches": batches,
        "cursor": cursor,
        "complete": complete,
        "stopped": stopped,
        "errors": errors,
    }
