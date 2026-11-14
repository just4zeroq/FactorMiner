"""Data freshness: is the local library current, and is the next experiment ripe?

Two questions the UI could not answer before:

1. *Which assets are stale?* ``/api/data_coverage`` only inspects one symbol at a
   time, so three files stuck two months back were invisible.
2. *When can the next pre-registered experiment start?* The P0.5 forward test
   needs a window that has never been looked at. That is a calendar question,
   and it belongs in the system rather than in someone's memory — otherwise the
   only thing standing between a clean holdout and a peek is discipline.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from core.data_feed.naming import parse_data_filename

logger = logging.getLogger(__name__)

# How many days of lag is acceptable before a file is called stale. Daily bars
# are only expected to be missing the in-progress day.
STALE_AFTER_DAYS = {"1d": 2, "4h": 1, "1h": 1, "15m": 1, "5m": 1, "1m": 1}

TIMEFRAME_DAYS = {"1w": 7.0, "1d": 1.0, "4h": 1 / 6, "1h": 1 / 24, "15m": 1 / 96, "5m": 1 / 288}


def display_symbol(safe_name: str, trade_type: str) -> str:
    """Turn the on-disk safe name back into CCXT notation.

    Files store ``BTC_USDT_USDT`` because ``/`` and ``:`` are unsafe; a trader
    reading the freshness table expects ``BTC/USDT:USDT``. This is display only
    and lossy by nature, so the raw name is kept alongside it.
    """
    parts = safe_name.split("_")
    if trade_type == "futures" and len(parts) >= 3 and parts[-1] == parts[-2]:
        return f"{'_'.join(parts[:-2])}/{parts[-2]}:{parts[-1]}"
    if len(parts) >= 2:
        return f"{'_'.join(parts[:-1])}/{parts[-1]}"
    return safe_name


def _read_last_date(path: str) -> Optional[tuple]:
    """Return (first, last, rows) for a canonical feather file."""
    try:
        frame = pd.read_feather(path, columns=["date"])
    except Exception:
        try:
            frame = pd.read_feather(path)
        except Exception as exc:
            logger.warning("Unreadable data file %s: %s", path, exc)
            return None
    if frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.min(), dates.max(), int(len(frame))


def _candidate_files(data_root: str):
    """Yield (path, exchange, market_type, stream) for every feather under the root.

    Two directory shapes exist. Plain OHLCV lives at
    ``{root}/{exchange}/{trade_type}/{file}``; crypto-native streams live one
    level deeper at ``{root}/{exchange}/{trade_type}/crypto_streams/{file}``.
    Walking only the first shape is what let 62 stream files stay invisible.
    """
    for exchange in sorted(os.listdir(data_root)):
        exchange_dir = os.path.join(data_root, exchange)
        if not os.path.isdir(exchange_dir):
            continue
        for market in sorted(os.listdir(exchange_dir)):
            market_dir = os.path.join(exchange_dir, market)
            if not os.path.isdir(market_dir):
                continue
            for filename in sorted(os.listdir(market_dir)):
                path = os.path.join(market_dir, filename)
                if os.path.isfile(path) and filename.endswith(".feather"):
                    yield path, exchange, market, None
            for sub in sorted(os.listdir(market_dir)):
                sub_dir = os.path.join(market_dir, sub)
                if not os.path.isdir(sub_dir):
                    continue
                for filename in sorted(os.listdir(sub_dir)):
                    path = os.path.join(sub_dir, filename)
                    if os.path.isfile(path) and filename.endswith(".feather"):
                        yield path, exchange, market, sub


def scan_library(data_root: str = "data") -> tuple:
    """Return ``(items, skipped)`` for the whole library.

    ``skipped`` matters as much as ``items``: a file that exists but cannot be
    read or has no timestamp column is worse than a missing one, because nothing
    downstream notices. Hiding those was how 30 MB of untimestamped 1m data sat
    in the library while every report said it was fine.
    """
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    if not os.path.isdir(data_root):
        return items, skipped

    for path, exchange, market, stream in _candidate_files(data_root):
        filename = os.path.basename(path)
        try:
            symbol, timeframe, parsed_type = parse_data_filename(filename)
        except ValueError as exc:
            skipped.append({"path": path, "reason": f"non-canonical name ({exc})"})
            continue
        summary = _read_last_date(path)
        if summary is None:
            skipped.append({"path": path, "reason": "no readable 'date' column"})
            continue
        first, last, rows = summary
        # Inside a stream directory the filename's last token is the stream
        # (basis / funding_rate / open_interest), not the market type.
        resolved_type = market if stream else (parsed_type or market)
        record = {
            "symbol": display_symbol(symbol, resolved_type),
            "safe_symbol": symbol,
            "exchange": exchange,
            "trade_type": resolved_type,
            "timeframe": timeframe,
            "start": first.strftime("%Y-%m-%d"),
            "last": last.strftime("%Y-%m-%d"),
            "rows": rows,
            "_last_ts": last,
        }
        if stream:
            record["stream"] = parsed_type or stream
        items.append(record)
    return items, skipped


def scan_data_root(data_root: str = "data") -> List[Dict[str, Any]]:
    """Every readable data file under ``{data_root}/{exchange}/{trade_type}/``."""
    return scan_library(data_root)[0]


def annotate_freshness(
    items: Iterable[Dict[str, Any]],
    today: Optional[datetime.date] = None,
    stale_after_days: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    today = today or datetime.date.today()
    thresholds = {**STALE_AFTER_DAYS, **(stale_after_days or {})}
    out: List[Dict[str, Any]] = []
    for item in items:
        record = dict(item)
        last_ts = record.pop("_last_ts", None)
        if last_ts is None and record.get("last"):
            # Reports round-trip through JSON, where the private timestamp is
            # gone; fall back to the serialised date so the function still works.
            parsed = pd.to_datetime(record["last"], errors="coerce")
            last_ts = None if pd.isna(parsed) else parsed
        if last_ts is None:
            out.append(record)
            continue
        lag = (today - last_ts.date()).days
        limit = thresholds.get(record["timeframe"], 1)
        record["lag_days"] = lag
        record["stale"] = lag > limit
        record["stale_after_days"] = limit
        out.append(record)
    return out


def experiment_progress(
    spec: Optional[dict],
    items: Iterable[Dict[str, Any]],
    today: Optional[datetime.date] = None,
) -> Optional[Dict[str, Any]]:
    """Progress toward a pre-registered window that has never been revealed.

    ``spec`` comes from config: ``{label, start, min_bars, timeframe}``. The
    count is taken from the freshest covered file at that timeframe — the window
    only becomes usable once *every* asset has the bars, so the minimum wins.
    """
    if not spec:
        return None
    timeframe = spec.get("timeframe", "1d")
    start = pd.Timestamp(spec["start"])
    min_bars = int(spec.get("min_bars", 90))
    today = today or datetime.date.today()

    relevant = [
        item for item in items if item.get("timeframe") == timeframe and not item.get("stream")
    ]
    if not relevant:
        return {
            "label": spec.get("label", "experiment"),
            "start": start.strftime("%Y-%m-%d"),
            "min_bars": min_bars,
            "timeframe": timeframe,
            "accumulated": 0,
            "coverage": 0,
            "ready": False,
            "eta": None,
            "note": "no data at this timeframe",
        }

    # The window is only clean if every asset has it, so use the shortest tape.
    last_dates = [pd.Timestamp(item["last"]) for item in relevant]
    last = min(last_dates)
    accumulated = 0
    if last >= start:
        accumulated = int((last.normalize() - start.normalize()).days) + 1
    day_step = TIMEFRAME_DAYS.get(timeframe, 1.0)
    remaining_bars = max(0, min_bars - accumulated)
    eta = None
    if remaining_bars == 0:
        eta = last.strftime("%Y-%m-%d")
    else:
        eta_date = start + datetime.timedelta(days=round(min_bars * day_step) - 1)
        eta = eta_date.strftime("%Y-%m-%d")

    return {
        "label": spec.get("label", "experiment"),
        "start": start.strftime("%Y-%m-%d"),
        "min_bars": min_bars,
        "timeframe": timeframe,
        "accumulated": accumulated,
        "remaining": remaining_bars,
        "coverage": round(min(1.0, accumulated / max(1, min_bars)), 4),
        "ready": accumulated >= min_bars,
        "eta": eta,
        "as_of": last.strftime("%Y-%m-%d"),
        "today": today.isoformat(),
    }


def freshness_report(
    data_root: str = "data",
    experiment_spec: Optional[dict] = None,
    stale_after_days: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    items, skipped = scan_library(data_root)
    items = annotate_freshness(items, stale_after_days=stale_after_days)
    stale = [item for item in items if item.get("stale")]
    ohlcv = [item for item in items if not item.get("stream")]

    by_timeframe: Dict[str, int] = {}
    stale_by_timeframe: Dict[str, int] = {}
    for item in ohlcv:
        by_timeframe[item["timeframe"]] = by_timeframe.get(item["timeframe"], 0) + 1
    for item in stale:
        if not item.get("stream"):
            stale_by_timeframe[item["timeframe"]] = stale_by_timeframe.get(item["timeframe"], 0) + 1

    latest = max((item["last"] for item in ohlcv), default=None)
    # A single dead intraday file should not read as "the library is broken",
    # but it should not hide either — so the raw count is kept and the split by
    # timeframe is reported next to it. Symbols are de-duplicated because one
    # asset can have several stale timeframes.
    stale_symbols: List[str] = []
    for item in stale:
        if not item.get("stream") and item["symbol"] not in stale_symbols:
            stale_symbols.append(item["symbol"])

    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "files": len(items),
            "assets_1d": by_timeframe.get("1d", 0),
            "streams": len(items) - len(ohlcv),
            "stale": len([item for item in stale if not item.get("stream")]),
            "latest": latest,
            "stale_symbols": stale_symbols[:20],
            "timeframes": by_timeframe,
            "stale_by_timeframe": stale_by_timeframe,
            "unreadable": {
                "count": len(skipped),
                "paths": [entry["path"] for entry in skipped[:10]],
                "details": skipped[:10],
            },
        },
        "items": sorted(items, key=lambda item: (item["timeframe"], item["symbol"])),
        "experiment": experiment_progress(experiment_spec, items),
    }
