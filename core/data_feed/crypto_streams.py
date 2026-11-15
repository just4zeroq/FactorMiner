"""Crypto-native data streams for perpetual-futures factor mining.

Equity factor libraries assume a single price/volume tape.  Perpetual futures
expose several *additional* tapes that are the actual drivers of crypto alpha:

======================  =====================================================
stream                  meaning
======================  =====================================================
``funding_rate``        periodic long↔short payment; a direct crowding gauge
``open_interest``       outstanding derivative contracts; a leverage gauge
``basis``               perp premium over spot, ``(perp - spot) / spot``
``long_liquidations``   quote volume of liquidated longs (forced selling)
``short_liquidations``  quote volume of liquidated shorts (forced buying)
======================  =====================================================

Storage layout is deliberately parallel to OHLCV so the canonical
``{symbol}-{timeframe}-{trade_type}.feather`` parser is never ambiguous::

    data/<exchange>/<trade_type>/crypto_streams/<safe_symbol>-<timeframe>-<stream>.feather

Each file stores a ``date`` column plus the canonical stream column.  Native
frequencies differ (funding settles at exchange-specific intervals — 8h on most
Binance pairs but 1h/4h/12h on others, and 1h/2h/4h on OKX/Hyperliquid/dYdX; OI
has its own period grid), so
files keep their *native* grid and alignment to the OHLCV bar index happens at
load time via past-only forward fill (``method="ffill"``) — never backfill,
which would leak future information.

CCXT exposes funding-rate and open-interest history for Binance linear
futures.  Liquidation history is **not** available from the free public API;
this module only provides a loader/validator for user-supplied files (export
from a vendor or an exchange data portal), and the feature templates still
work as soon as such a file exists.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Canonical stream names.  These are the terminal names miners reference in
# expression ASTs (e.g. ``{"op": "ts_zscore", "left": "funding_rate"}``).
CRYPTO_STREAM_NAMES: Tuple[str, ...] = (
    "funding_rate",
    "open_interest",
    "basis",
    "long_liquidations",
    "short_liquidations",
)

# Streams that need a vendor/user-supplied file (no free public endpoint).
USER_SUPPLIED_STREAMS: Tuple[str, ...] = ("long_liquidations", "short_liquidations")

# Stream -> column that must exist in the stored feather file.
STREAM_COLUMNS: Dict[str, str] = {
    "funding_rate": "funding_rate",
    "open_interest": "open_interest",
    "basis": "basis",
    "long_liquidations": "long_liquidations",
    "short_liquidations": "short_liquidations",
}


class CryptoStreamUnavailable(RuntimeError):
    """Raised when a crypto stream cannot be fetched or found on disk."""


def _retry(fn, attempts: int = 3, base_delay: float = 0.6, label: str = ""):
    """Retry a network call with exponential backoff (proxy tunnels are flaky)."""
    import time

    last: Optional[Exception] = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - surface the last error
            last = exc
            if i + 1 < attempts:
                logger.debug("%s failed (attempt %d/%d): %s", label, i + 1, attempts, exc)
                time.sleep(base_delay * (2 ** i))
    assert last is not None
    raise last


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def crypto_stream_dir(data_root: Path, exchange: str, trade_type: str) -> Path:
    return Path(data_root) / exchange / trade_type / "crypto_streams"


def crypto_stream_path(
    data_root: Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    trade_type: str,
    stream: str,
) -> Path:
    """Canonical path of one stored crypto stream."""
    from core.data_feed.naming import safe_symbol

    if stream not in CRYPTO_STREAM_NAMES:
        raise ValueError(f"unknown crypto stream '{stream}'; expected one of {CRYPTO_STREAM_NAMES}")
    filename = f"{safe_symbol(symbol)}-{timeframe}-{stream}.feather"
    return crypto_stream_dir(data_root, exchange, trade_type) / filename


# --------------------------------------------------------------------------- #
# Timeframe helpers
# --------------------------------------------------------------------------- #
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    """Parse a ccxt timeframe string (``1m``/``4h``/``1d``) into a Timedelta."""
    tf = str(timeframe).strip().lower()
    if not tf:
        raise ValueError("timeframe must be non-empty")
    unit = tf[-1]
    if unit not in _UNIT_SECONDS:
        raise ValueError(f"unsupported timeframe '{timeframe}'")
    try:
        amount = int(tf[:-1])
    except ValueError as exc:
        raise ValueError(f"unsupported timeframe '{timeframe}'") from exc
    return pd.Timedelta(seconds=amount * _UNIT_SECONDS[unit])


def _ms(dt: pd.Timestamp) -> int:
    return int(pd.Timestamp(dt).timestamp() * 1000)


def _to_naive_utc(ts_ms: int) -> pd.Timestamp:
    # Exchanges sometimes return timestamps with sub-second offsets (e.g.
    # 08:00:00.002); floor to the second so the value lands exactly on its bar
    # and forward-fill does not lag by one period.
    return pd.to_datetime(int(ts_ms), unit="ms", utc=True).tz_localize(None).floor("s")


# Binance open-interest history accepts these periods only.  For a target
# timeframe we use the largest supported period that evenly divides it, so we
# fetch the least amount of data while still aligning exactly on bar closes.
_OI_PERIODS = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]


def oi_period_for(timeframe: str) -> str:
    target = timeframe_to_timedelta(timeframe)
    best = None
    for p in _OI_PERIODS:
        delta = timeframe_to_timedelta(p)
        if delta <= target and target % delta == pd.Timedelta(0):
            best = p
    if best is None:
        # Target is finer than 5m (e.g. 1m): fetch 5m and forward-fill later.
        best = "5m"
    return best


# --------------------------------------------------------------------------- #
# Exchange pagination
# --------------------------------------------------------------------------- #
def _paginate(
    fetch,
    start: pd.Timestamp,
    end: pd.Timestamp,
    limit: int,
    step: pd.Timedelta,
    label: str,
) -> List[dict]:
    """Page a ccxt history endpoint in ``[start, end)`` without re-fetching."""
    rows: List[dict] = []
    since = _ms(start)
    end_ms = _ms(end)
    guard = 0
    while since < end_ms:
        guard += 1
        if guard > 5000:
            raise CryptoStreamUnavailable(f"{label}: pagination exceeded safety limit")
        batch = fetch(since, limit)
        if not batch:
            break
        rows.extend(batch)
        last_ts = int(batch[-1]["timestamp"])
        if last_ts < since:  # exchange returned stale data; stop to avoid a loop
            break
        # Advance past the last returned bar; step keeps a small overlap window
        # so no bar is skipped when the exchange caps ``limit``.
        since = max(last_ts + 1, since + int(step.total_seconds() * 1000))
    # Exchanges honour ``startTime`` but not always an end bound, so the last
    # page can overshoot the requested window; trim it before returning.
    lo_ms, hi_ms = _ms(start), _ms(end)
    return [
        r for r in rows
        if r.get("timestamp") is not None and lo_ms <= int(r["timestamp"]) <= hi_ms
    ]


def _frame_from_rows(rows: Iterable[dict], value_getter, column: str) -> pd.DataFrame:
    records = []
    for r in rows:
        ts = r.get("timestamp")
        value = value_getter(r)
        if ts is None or value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        records.append((_to_naive_utc(ts), value))
    if not records:
        return pd.DataFrame(columns=["date", column])
    df = pd.DataFrame(records, columns=["date", column])
    df = df.dropna(subset=[column]).drop_duplicates(subset=["date"], keep="last")
    return df.sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Downloaders (network)
# --------------------------------------------------------------------------- #
def _funding_settlement_interval(exchange, symbol) -> Optional[pd.Timedelta]:
    """Best-effort detection of a pair's funding settlement interval.

    Returns the interval if the exchange exposes it (e.g. Binance linear
    futures carry ``fundingIntervalHours`` in the market info), else ``None``.
    Used only for logging; pagination correctness never depends on it.
    """
    try:
        market = exchange.market(symbol)
        info = (market or {}).get("info") or {}
        raw = info.get("fundingIntervalHours")
        if raw is None:
            raw = info.get("fundingInterval")
        if raw is not None:
            return pd.Timedelta(hours=float(raw))
    except Exception:  # noqa: BLE001 - many shapes of "unsupported"
        pass
    return None


def download_funding_rate(
    exchange,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    timeframe: str,
) -> pd.DataFrame:
    """Fetch funding-rate history and return ``date, funding_rate``.

    Funding settles at exchange-specific intervals — Binance uses 8h for most
    pairs but 1h/4h/12h for others, while OKX/Hyperliquid/dYdX often settle
    every 1h/2h/4h. The paginator must therefore not assume an 8h grid: it
    advances strictly on the most recent *returned* timestamp, with a 1-second
    floor that is smaller than every real settlement interval. That guarantees
    no settlement is ever skipped, regardless of how often the pair settles.
    """
    detected = _funding_settlement_interval(exchange, symbol)
    if detected is not None:
        logger.info(
            "funding_rate %s: detected settlement interval %s (paginating by returned timestamps)",
            symbol,
            detected,
        )
    # Floor far below any real settlement interval so ``last_ts + 1`` always
    # dominates the cursor advance and we never jump past unsent rows. The page
    # size is governed by ``limit`` (not this floor), so request volume is
    # unchanged for dense history.
    step = pd.Timedelta(seconds=1)
    try:
        rows = _retry(
            lambda: _paginate(
                lambda since, limit: exchange.fetch_funding_rate_history(
                    symbol, since=since, limit=limit
                ),
                start,
                end,
                limit=1000,
                step=step,
                label=f"funding_rate {symbol}",
            ),
            label=f"funding_rate {symbol}",
        )
    except CryptoStreamUnavailable:
        raise
    except Exception as exc:  # network / unsupported market
        raise CryptoStreamUnavailable(f"funding_rate fetch failed for {symbol}: {exc}") from exc

    df = _frame_from_rows(
        rows,
        lambda r: r.get("fundingRate", (r.get("info") or {}).get("fundingRate")),
        "funding_rate",
    )
    if df.empty:
        raise CryptoStreamUnavailable(f"funding_rate returned no rows for {symbol}")
    return df


def download_open_interest(
    exchange,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    timeframe: str,
) -> pd.DataFrame:
    """Fetch open-interest history and return ``date, open_interest``.

    Prefers quote-denominated OI (``openInterestValue``) so the stream is
    comparable across assets with different contract sizes.
    """
    period = oi_period_for(timeframe)
    step = timeframe_to_timedelta(period)
    try:
        rows = _retry(
            lambda: _paginate(
                lambda since, limit: exchange.fetch_open_interest_history(
                    symbol, timeframe=period, since=since, limit=limit
                ),
                start,
                end,
                limit=500,
                step=step,
                label=f"open_interest {symbol}",
            ),
            label=f"open_interest {symbol}",
        )
    except CryptoStreamUnavailable:
        raise
    except Exception as exc:
        raise CryptoStreamUnavailable(f"open_interest fetch failed for {symbol}: {exc}") from exc

    def _oi(r: dict):
        if r.get("openInterestValue") is not None:
            return r["openInterestValue"]
        if r.get("openInterestAmount") is not None:
            return r["openInterestAmount"]
        info = r.get("info") or {}
        return info.get("sumOpenInterestValue") or info.get("sumOpenInterest")

    df = _frame_from_rows(rows, _oi, "open_interest")
    if df.empty:
        raise CryptoStreamUnavailable(f"open_interest returned no rows for {symbol}")
    return df


def download_spot_close(
    exchange,
    spot_symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Fetch spot closes (used only to derive the basis stream)."""
    step = timeframe_to_timedelta(timeframe)

    def _fetch(since: int, limit: int):
        raw = exchange.fetch_ohlcv(spot_symbol, timeframe=timeframe, since=since, limit=limit)
        return [{"timestamp": r[0], "close": r[4]} for r in raw]

    rows = _retry(
        lambda: _paginate(_fetch, start, end, limit=1000, step=step, label=f"spot close {spot_symbol}"),
        label=f"spot close {spot_symbol}",
    )
    return _frame_from_rows(rows, lambda r: r.get("close"), "spot_close")


def build_basis_frame(perp_close: pd.Series, spot_close: pd.Series) -> pd.DataFrame:
    """Compute ``basis = (perp - spot) / spot`` on the intersection of stamps."""
    perp = perp_close.dropna().sort_index()
    spot = spot_close.dropna().sort_index()
    perp = perp[~perp.index.duplicated(keep="last")]
    spot = spot[~spot.index.duplicated(keep="last")]
    aligned = pd.concat({"perp": perp, "spot": spot}, axis=1, join="inner").dropna()
    if aligned.empty:
        return pd.DataFrame(columns=["date", "basis"])
    basis = (aligned["perp"] - aligned["spot"]) / (aligned["spot"].abs() + 1e-12)
    out = basis.rename("basis").reset_index()
    out.columns = ["date", "basis"]
    return out


def derive_spot_symbol(futures_symbol: str) -> str:
    """``BTC/USDT:USDT`` -> ``BTC/USDT`` (drop the settlement suffix)."""
    if ":" not in futures_symbol:
        raise ValueError(
            f"cannot derive spot symbol from '{futures_symbol}'; pass spot_symbol explicitly"
        )
    return futures_symbol.split(":", 1)[0]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _write_stream(df: pd.DataFrame, path: Path, column: str) -> None:
    """Persist one stream, merging with any existing file (incremental-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    if "date" not in out.columns:
        index_name = out.index.name or "index"
        out = out.reset_index().rename(columns={index_name: "date"})
    out["date"] = pd.to_datetime(out["date"])
    out = out[["date", column]].drop_duplicates(subset=["date"], keep="last")

    if path.exists():
        try:
            old = pd.read_feather(path)
            if "date" in old.columns and column in old.columns:
                old = old[["date", column]].copy()
                old["date"] = pd.to_datetime(old["date"])
                out = pd.concat([old, out], ignore_index=True)
                out = out.drop_duplicates(subset=["date"], keep="last")
        except Exception as exc:  # corrupt/legacy file -> replace it
            logger.warning("could not merge existing %s (%s); overwriting", path.name, exc)

    out = out.sort_values("date").reset_index(drop=True)
    out.to_feather(path)


def materialize_crypto_streams(
    data_root: Path,
    exchange_id: str,
    symbol: str,
    timeframe: str,
    trade_type: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    streams: Optional[Iterable[str]] = None,
    spot_symbol: Optional[str] = None,
    exchange=None,
    spot_exchange=None,
) -> Dict[str, Dict[str, object]]:
    """Fetch and persist the requested crypto streams.

    Returns a per-stream status dict ``{stream: {"ok": bool, "path": str,
    "rows": int|None, "error": str|None}}``.  Individual failures never abort
    the others — a missing stream simply means the corresponding feature
    templates stay dormant.
    """
    requested = list(streams or ("funding_rate", "open_interest", "basis"))
    status: Dict[str, Dict[str, object]] = {}

    # The downloader pulls in ccxt, which is only needed for network-backed
    # streams.  User-supplied streams (liquidations) must stay usable in a
    # minimal environment (tests / offline hosts) — import lazily so a missing
    # ccxt does not break file-only materialisation.
    dl = None
    download_error: Optional[Exception] = None
    needs_downloader = any(s not in USER_SUPPLIED_STREAMS for s in requested)
    if needs_downloader:
        try:
            from core.data_feed.data_downloader import DataDownloader

            dl = DataDownloader()
        except Exception as exc:  # pragma: no cover - depends on environment
            download_error = exc
            logger.warning("crypto data downloader unavailable: %s", exc)

    if dl is not None and exchange is None and any(s != "basis" for s in requested):
        exchange = dl.get_exchange_instance(exchange_id=exchange_id, trade_type=trade_type)
    if dl is not None and spot_exchange is None and "basis" in requested:
        spot_exchange = dl.get_exchange_instance(exchange_id=exchange_id, trade_type="spot")

    perp_close = None
    spot_close = None
    if "basis" in requested:
        # Perp closes come from the already-materialized OHLCV file.
        from core.data_feed.naming import data_path

        ohlcv_path = data_path(data_root, exchange_id, symbol, timeframe, trade_type)
        if ohlcv_path.exists():
            ohlcv = pd.read_feather(ohlcv_path)
            date_col = "date" if "date" in ohlcv.columns else ohlcv.columns[0]
            ohlcv[date_col] = pd.to_datetime(ohlcv[date_col])
            perp_close = ohlcv.set_index(date_col)["close"].sort_index()
        if spot_exchange is not None:
            try:
                spot_sym = spot_symbol or derive_spot_symbol(symbol)
                spot_df = download_spot_close(spot_exchange, spot_sym, timeframe, start, end)
                if not spot_df.empty:
                    spot_close = spot_df.set_index("date")["spot_close"]
            except Exception as exc:
                logger.warning("basis: spot download failed (%s)", exc)

    for stream in requested:
        path = crypto_stream_path(data_root, exchange_id, symbol, timeframe, trade_type, stream)
        try:
            if stream in USER_SUPPLIED_STREAMS:
                if not path.exists():
                    raise CryptoStreamUnavailable(
                        f"{stream} has no free public endpoint; place a feather file at {path}"
                    )
                df = pd.read_feather(path)
                _validate_stored(df, stream)
                status[stream] = {"ok": True, "path": str(path), "rows": len(df), "error": None}
                continue

            if stream in ("funding_rate", "open_interest") and dl is None:
                raise CryptoStreamUnavailable(
                    f"data downloader unavailable for {stream}: {download_error}"
                )

            if stream == "funding_rate":
                if exchange is None:
                    raise CryptoStreamUnavailable("no futures exchange instance")
                df = download_funding_rate(exchange, symbol, start, end, timeframe)
            elif stream == "open_interest":
                if exchange is None:
                    raise CryptoStreamUnavailable("no futures exchange instance")
                df = download_open_interest(exchange, symbol, start, end, timeframe)
            elif stream == "basis":
                if perp_close is None:
                    raise CryptoStreamUnavailable(
                        "perp OHLCV missing; download it before deriving basis"
                    )
                if spot_close is None:
                    raise CryptoStreamUnavailable(
                        "spot OHLCV unavailable; pass spot_symbol or download spot data"
                    )
                df = build_basis_frame(perp_close, spot_close)
            else:  # pragma: no cover - guarded by crypto_stream_path
                raise CryptoStreamUnavailable(f"unknown stream {stream}")

            if df is None or df.empty:
                raise CryptoStreamUnavailable(f"{stream} returned an empty frame")
            _write_stream(df, path, STREAM_COLUMNS[stream])
            status[stream] = {"ok": True, "path": str(path), "rows": len(df), "error": None}
            logger.info("crypto stream %s stored: %s (%d rows)", stream, path, len(df))
        except Exception as exc:
            status[stream] = {"ok": False, "path": str(path), "rows": None, "error": str(exc)}
            logger.warning("crypto stream %s unavailable: %s", stream, exc)
    return status


# --------------------------------------------------------------------------- #
# Loading & alignment
# --------------------------------------------------------------------------- #
def _validate_stored(df: pd.DataFrame, stream: str) -> pd.DataFrame:
    column = STREAM_COLUMNS[stream]
    if column not in df.columns:
        raise CryptoStreamUnavailable(
            f"stored {stream} file lacks required column '{column}' "
            f"(found: {list(df.columns)})"
        )
    out = df.copy()
    if "date" not in out.columns:
        # User-supplied files may store the timestamp as the index instead.
        if isinstance(out.index, pd.DatetimeIndex):
            index_name = out.index.name or "index"
            out = out.reset_index().rename(columns={index_name: "date"})
        else:
            raise CryptoStreamUnavailable(f"stored {stream} file lacks a 'date' column")
    out["date"] = pd.to_datetime(out["date"])
    out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date")
    return out


def align_series_to_index(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Past-only alignment: reindex with forward fill (never backfill)."""
    s = pd.Series(series).copy()
    s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if s.empty:
        return pd.Series(np.nan, index=index)
    return s.reindex(index, method="ffill")


def load_crypto_streams(
    data_root: Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    trade_type: str,
    index: pd.DatetimeIndex,
    streams: Optional[Iterable[str]] = None,
) -> Dict[str, pd.Series]:
    """Load stored streams and align them to the OHLCV bar index.

    Missing files are skipped silently (logged at debug level); the caller
    receives only the streams that actually exist.
    """
    out: Dict[str, pd.Series] = {}
    requested = list(streams or CRYPTO_STREAM_NAMES)
    for stream in requested:
        path = crypto_stream_path(data_root, exchange, symbol, timeframe, trade_type, stream)
        if not path.exists():
            logger.debug("crypto stream %s not found at %s", stream, path)
            continue
        try:
            df = _validate_stored(pd.read_feather(path), stream)
            column = STREAM_COLUMNS[stream]
            series = df.set_index("date")[column]
            out[stream] = align_series_to_index(series, index)
        except Exception as exc:
            logger.warning("failed to load crypto stream %s: %s", stream, exc)
    return out
