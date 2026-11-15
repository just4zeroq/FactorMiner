"""Bar-clock: has a bar's window finished yet?

A bar stamped ``t`` covers ``[t, t + timeframe)`` and is only *closed* once
``t + timeframe <= now(UTC)``. Until then its OHLCV is partial and its forward
return is undefined, so anything that treats it as a finished observation is
reading a value that will still change.

This matters in three separate places, all of which used to disagree:

* the mining panel (``RealDataClient``) — trimmed since D-05;
* the holdout window planner (``core.experiment.windows``) — **not** trimmed,
  so a "ready" window could contain a half-formed bar and burn a holdout
  window on it (review P0-d);
* the freshness report — counted the forming bar as an accumulated bar, which
  made a pre-registered window look complete one bar early.

They now share this module so the rule cannot drift again.

Note the difference between a *bar* (an interval) and a *point stamp*: funding
settlements, open-interest snapshots and other crypto streams are points in
time and only need ``ts <= now``; use :func:`point_is_known` for those.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

import pandas as pd

from core.data_feed.crypto_streams import timeframe_to_timedelta

__all__ = [
    "utc_now_naive",
    "bar_delta",
    "bar_is_closed",
    "last_closed_start",
    "point_is_known",
    "drop_unclosed_last_bar",
    "closed_only",
]


def utc_now_naive(now: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    """``now`` as a tz-naive UTC timestamp (the pipeline's convention)."""
    if now is None:
        return pd.Timestamp.now(tz="UTC").tz_convert(None)
    ts = pd.Timestamp(now)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def bar_delta(timeframe: str) -> pd.Timedelta:
    """Length of one bar. Falls back to 1 day on an unparseable timeframe."""
    try:
        return timeframe_to_timedelta(timeframe)
    except Exception:  # pragma: no cover - defensive
        return pd.Timedelta(days=1)


def bar_is_closed(
    bar_start: pd.Timestamp, timeframe: str, now: Optional[pd.Timestamp] = None
) -> bool:
    """True when the bar that *starts* at ``bar_start`` has finished forming."""
    start = pd.Timestamp(bar_start)
    if start.tzinfo is not None:
        start = start.tz_convert("UTC").tz_localize(None)
    return bool(start + bar_delta(timeframe) <= utc_now_naive(now))


def point_is_known(
    stamp: pd.Timestamp, now: Optional[pd.Timestamp] = None
) -> bool:
    """True for point-in-time stamps (funding settlements, OI snapshots)."""
    ts = pd.Timestamp(stamp)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return bool(ts <= utc_now_naive(now))


def last_closed_start(timeframe: str, now: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    """Latest bar start whose window has closed."""
    return utc_now_naive(now) - bar_delta(timeframe)


def closed_only(
    dates: Iterable[pd.Timestamp],
    timeframe: str,
    now: Optional[pd.Timestamp] = None,
) -> List[pd.Timestamp]:
    """Filter ``dates`` down to the bars that have already closed."""
    cutoff = last_closed_start(timeframe, now)
    out: List[pd.Timestamp] = []
    for value in dates:
        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        if ts <= cutoff:
            out.append(ts)
    return out


def drop_unclosed_last_bar(
    df: pd.DataFrame,
    timeframe: str,
    now: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Drop a trailing bar that is still forming (no-op on closed data)."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    last_start = df.index.max()
    if pd.isna(last_start):
        return df
    if bar_is_closed(last_start, timeframe, now=now):
        return df
    return df[df.index != last_start]
