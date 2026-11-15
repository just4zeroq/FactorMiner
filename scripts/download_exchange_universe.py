"""Discover and download every active USDT market of an exchange.

The production refresh path (``factorminer refresh`` → ``scripts/update_data.py``)
is built to keep a *known* universe current: it plans per symbol, retries, and
verifies the tail. It is deliberately not built to *onboard ~1000 symbols*,
because ``SmartBatchDownloader`` refuses to save unless a file scores 100/100 on
its health check, and a freshly listed micro-cap with one exchange outage would
fail that gate forever while burning 20 repair requests.

This script covers the onboarding case only:

* **Discovery** — ask the exchange for its live markets and write an explicit
  universe file (JSON). The universe is a materialised list, not a filter
  applied at mining time, so "what was mined" stays reproducible after the
  exchange delists something.
* **Bulk download** — plain CCXT pagination via ``paginate_ohlcv`` (the same
  cursor logic the production path uses), writing the canonical
  ``{safe_symbol}-{timeframe}-{trade_type}.feather`` files.
* **Closed bars only** — the forming daily bar is dropped with the shared
  ``bar_clock`` rule, so the panel can never pick up a partial candle.
* **Resumable + loud** — one JSONL line per symbol, an atomic replace on every
  write, and a non-zero exit code when any symbol failed. Re-running only
  fetches the tail.

Anti-junk filters that are *not* secret sauce: stablecoin/fiat bases and
Binance leveraged tokens are excluded because they are not independent
cross-sectional bets (``USDC/USDT`` is a pegged pair, ``BTCUP/USDT`` is a
leveraged claim on BTC). Dated futures are excluded too — they expire, so they
cannot form a continuous panel; only perpetuals are downloaded.

Usage::

    # what would be downloaded
    python scripts/download_exchange_universe.py --discover-only

    # one symbol smoke test
    python scripts/download_exchange_universe.py --limit 1

    # the real thing (both market types)
    python scripts/download_exchange_universe.py --trade-types spot,futures

    # daily maintenance is still ``factorminer refresh``
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.data_feed.bar_clock import bar_is_closed, utc_now_naive  # noqa: E402
from core.data_feed.naming import data_path, safe_symbol  # noqa: E402
from core.data_feed.pagination import paginate_ohlcv  # noqa: E402

DEFAULT_UNIVERSE_DIR = os.path.join("user_workspace", "universes")
DEFAULT_REPORT_DIR = os.path.join("user_workspace", "reports", "universe_downloads")
DEFAULT_START = "2017-01-01"

# Pegged / fiat bases: a cross-section of these measures nothing but the peg.
STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "GUSD", "UST", "USTC",
    "AEUR", "EUR", "GBP", "AUD", "BRL", "TRY", "RUB", "UAH", "ZAR", "BIDR", "IDRT",
    "BKRW", "NGN", "PLN", "RON", "ARS", "JPY", "USD1", "XUSD", "BFUSD", "USDE",
    "USDS", "PYUSD", "RLUSD", "EURI", "EURS", "USDQ", "USDG", "SUSD", "FRAX",
    "LUSD", "MIM", "DOLA", "CUSD", "VAI", "YUSD", "USDX", "USDD", "USDY",
}

# Binance leveraged tokens (BTCUP/BTCDOWN style). Matched exactly, never by
# suffix: ``JUP`` and ``SUP`` are ordinary coins.
LEVERAGED_TOKENS = {
    f"{base}{side}"
    for base in (
        "BTC", "ETH", "BNB", "ADA", "DOT", "LINK", "LTC", "XRP", "EOS", "TRX",
        "BCH", "SUSHI", "YFI", "1INCH", "XTZ", "FIL", "UNI", "AAVE", "SOL", "SRM",
        "SXP", "XLM", "ATOM", "NEO", "QTUM", "IOTA", "ONT", "BAND", "OCEAN",
    )
    for side in ("UP", "DOWN", "BULL", "BEAR")
}

_LEVERAGED_RE = re.compile(r"^\d+(UP|DOWN|BULL|BEAR)$")  # 1000BULL style

_EXCHANGE_LOCK = threading.Lock()
_PRINT_LOCK = threading.Lock()


def _log(message: str, *, err: bool = False) -> None:
    with _PRINT_LOCK:
        print(message, file=sys.stderr if err else sys.stdout, flush=True)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and bulk-download every active USDT market (spot + perp).",
    )
    parser.add_argument("--exchange", default="binance")
    parser.add_argument(
        "--trade-types",
        default="spot,futures",
        help="Comma-separated: spot,futures (default: both)",
    )
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--start", default=DEFAULT_START, help=f"floor for history (default: {DEFAULT_START})")
    parser.add_argument(
        "--until",
        default=None,
        help="last closed bar to keep, YYYY-MM-DD (default: latest closed bar)",
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--universe-dir", default=DEFAULT_UNIVERSE_DIR)
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--workers", type=int, default=4, help="parallel symbols (default: 4)")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="only the first N symbols (smoke test)")
    parser.add_argument("--symbols", default=None, help="comma-separated explicit symbol list")
    parser.add_argument("--discover-only", action="store_true", help="write the universe file and stop")
    parser.add_argument("--refresh-universe", action="store_true", help="ignore a cached universe file")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, download nothing")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _market_ok(market: Dict[str, Any], trade_type: str) -> bool:
    if not market.get("active"):
        return False
    if market.get("quote") != "USDT":
        return False
    base = str(market.get("base") or "")
    if not base or base in STABLE_BASES or base in LEVERAGED_TOKENS or _LEVERAGED_RE.match(base):
        return False
    if trade_type == "spot":
        return bool(market.get("spot"))
    # Perpetual, USDT-margined, linear only. Dated futures expire and cannot
    # form a continuous panel, so they are intentionally excluded.
    return bool(market.get("swap")) and bool(market.get("linear")) and market.get("settle") == "USDT"


def discover_universe(exchange_id: str, trade_type: str) -> List[Dict[str, Any]]:
    from core.data_feed.data_downloader import DataDownloader

    downloader = DataDownloader()
    exchange = downloader.get_exchange_instance(exchange_id=exchange_id, trade_type=trade_type)
    if exchange is None:
        raise RuntimeError(f"cannot build {exchange_id} client")
    for attempt in range(1, 4):
        try:
            exchange.load_markets()
            break
        except Exception as exc:  # noqa: BLE001 - proxy hiccups are normal
            if attempt == 3:
                raise
            _log(f"[discover] {exchange_id}/{trade_type} load_markets failed ({exc}); retry {attempt}/3", err=True)
            time.sleep(3 * attempt)

    rows: List[Dict[str, Any]] = []
    for market in exchange.markets.values():
        if not _market_ok(market, trade_type):
            continue
        info = market.get("info") or {}
        onboard = info.get("onboardDate") or info.get("onboard_date")
        rows.append(
            {
                "symbol": market["symbol"],
                "base": market.get("base"),
                "quote": market.get("quote"),
                "trade_type": trade_type,
                "onboard_ms": int(onboard) if str(onboard or "").isdigit() else None,
            }
        )
    rows.sort(key=lambda item: item["symbol"])
    return rows


def universe_path(universe_dir: str, exchange_id: str, trade_type: str, timeframe: str) -> Path:
    return Path(universe_dir) / f"{exchange_id}_{trade_type}_usdt_{timeframe}.json"


def load_or_discover(
    universe_dir: str,
    exchange_id: str,
    trade_type: str,
    timeframe: str,
    refresh: bool,
) -> Tuple[List[Dict[str, Any]], Path]:
    path = universe_path(universe_dir, exchange_id, trade_type, timeframe)
    if path.exists() and not refresh:
        payload = json.loads(path.read_text(encoding="utf-8"))
        symbols = payload.get("markets") or [{"symbol": s, "trade_type": trade_type} for s in payload.get("symbols", [])]
        _log(f"[universe] {trade_type}: {len(symbols)} symbol(s) from cache {path}")
        return symbols, path

    markets = discover_universe(exchange_id, trade_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "exchange": exchange_id,
        "trade_type": trade_type,
        "timeframe": timeframe,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(markets),
        "filter": {
            "quote": "USDT",
            "active": True,
            "excluded_stable_bases": sorted(STABLE_BASES),
            "excluded_leveraged": True,
            "perpetual_only": trade_type == "futures",
        },
        "symbols": [item["symbol"] for item in markets],
        "markets": markets,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[universe] {trade_type}: {len(markets)} symbol(s) -> {path}")
    return markets, path


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _roundtrip(trade_type: str) -> str:
    return "futures" if trade_type == "futures" else "spot"


def _client_for(trade_type: str):
    from core.data_feed.data_downloader import DataDownloader

    exchange = DataDownloader().get_exchange_instance(exchange_id="binance", trade_type=trade_type)
    if exchange is None:
        raise RuntimeError(f"cannot build binance/{trade_type} client")
    return exchange


def _existing_tail(path: Path) -> Tuple[Optional[pd.Timestamp], int]:
    if not path.exists():
        return None, 0
    try:
        frame = pd.read_feather(path, columns=["date"])
    except Exception:  # noqa: BLE001 - let the caller treat it as unusable
        return None, -1
    if frame.empty:
        return None, 0
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None, -1
    return dates.max().normalize(), int(len(dates))


def _normalise(candles: Iterable[Sequence[Any]], timeframe: str, now: pd.Timestamp) -> pd.DataFrame:
    rows = list(candles)
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["date"] = pd.to_datetime(frame["timestamp"], unit="ms")
    frame = frame.drop(columns=["timestamp"])
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    frame = frame[~frame["date"].duplicated(keep="last")].sort_values("date")

    # Closed bars only: the forming daily candle must never reach the library.
    if not frame.empty:
        last = frame["date"].iloc[-1]
        if not bar_is_closed(last, timeframe, now=now):
            frame = frame.iloc[:-1]
    return frame[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def _trim_after(path: Path, until: dt.date) -> int:
    """Drop bars dated after ``until``; returns how many rows were removed.

    A file downloaded by an earlier (or manual) run can contain the still-forming
    bar. Refusing to touch it because ``last >= until`` would keep a partial
    candle in the library forever, so trim instead of skipping.
    """
    if not path.exists():
        return 0
    frame = pd.read_feather(path)
    if frame.empty or "date" not in frame.columns:
        return 0
    dates = pd.to_datetime(frame["date"], errors="coerce")
    beyond = dates.dt.date > until
    removed = int(beyond.sum())
    if removed:
        frame = frame.loc[~beyond].reset_index(drop=True)
        _write_atomic(frame, path)
    return removed


def _validate(frame: pd.DataFrame, symbol: str) -> List[str]:
    problems: List[str] = []
    if frame.empty:
        return ["no rows"]
    if frame["date"].duplicated().any():
        problems.append("duplicate timestamps")
    if not frame["date"].is_monotonic_increasing:
        problems.append("unsorted dates")
    price = frame[["open", "high", "low", "close"]]
    if (price <= 0).any().any():
        problems.append("non-positive price")
    if (frame["high"] < frame["low"]).any():
        problems.append("high < low")
    if (frame["volume"] < 0).any():
        problems.append("negative volume")
    return problems


def _write_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_feather(tmp)
    os.replace(tmp, path)


def download_one(
    market: Dict[str, Any],
    *,
    trade_type: str,
    timeframe: str,
    start: dt.date,
    until: dt.date,
    data_root: str,
    exchange_id: str,
    retries: int,
    now: pd.Timestamp,
) -> Dict[str, Any]:
    symbol = market["symbol"]
    path = data_path(data_root, exchange_id, symbol, timeframe, trade_type)
    started = time.time()
    record: Dict[str, Any] = {
        "symbol": symbol,
        "trade_type": trade_type,
        "file": str(path),
        "status": "failed",
    }

    tail, existing_rows = _existing_tail(path)
    if existing_rows < 0:
        record.update({"error": "existing file unreadable; not overwritten"})
        return record
    trimmed = _trim_after(path, until)
    if trimmed:
        tail, existing_rows = _existing_tail(path)
        record["trimmed"] = trimmed
    if tail is not None and tail.date() >= until:
        record.update({"status": "current", "rows": existing_rows, "last": str(tail.date())})
        return record

    # History floor: a market's onboard date (futures) or the configured start.
    floor = start
    if market.get("onboard_ms"):
        onboard = pd.to_datetime(market["onboard_ms"], unit="ms").normalize().date()
        floor = max(floor, onboard)
    fetch_from = tail.date() if tail is not None else floor
    if fetch_from > until:
        record.update({"status": "current", "rows": existing_rows, "last": str(tail.date())})
        return record

    since_ms = int(pd.Timestamp(fetch_from).timestamp() * 1000)
    until_ms = int((pd.Timestamp(until) + pd.Timedelta(days=1)).timestamp() * 1000)

    last_error: Optional[str] = None
    page: Optional[Dict[str, Any]] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            exchange = _THREAD_CLIENTS.get(threading.get_ident())
            if exchange is None:
                with _EXCHANGE_LOCK:
                    exchange = _client_for(trade_type)
                _THREAD_CLIENTS[threading.get_ident()] = exchange
            if not getattr(exchange, "markets", None):
                with _EXCHANGE_LOCK:
                    exchange.load_markets()
            page = paginate_ohlcv(
                exchange,
                symbol,
                timeframe,
                since_ms,
                until_ms,
                timeframe_ms=exchange.parse_timeframe(timeframe) * 1000,
                limit=1000,
                retry_attempts=2,
                delay_seconds=0.0,
            )
            if not page["complete"]:
                detail = "; ".join(page["errors"][-2:]) or str(page["stopped"])
                raise RuntimeError(f"incomplete ({page['stopped']}): {detail}")
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the batch
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(30, 2 ** attempt) * attempt)

    if last_error is not None or page is None:
        record.update({"error": last_error or "unknown error", "elapsed_s": round(time.time() - started, 1)})
        return record

    fresh = _normalise(page["candles"], timeframe, now)
    if tail is not None and not fresh.empty:
        fresh = fresh[fresh["date"] > pd.Timestamp(tail)]

    if fresh.empty:
        record.update(
            {
                "status": "current" if tail is not None else "empty",
                "rows": existing_rows,
                "last": str(tail.date()) if tail is not None else None,
                "elapsed_s": round(time.time() - started, 1),
            }
        )
        if tail is None:
            record["error"] = "exchange returned no candles"
        return record

    problems = _validate(fresh, symbol)
    if problems:
        record.update({"error": "validation failed: " + ", ".join(problems), "rows": int(len(fresh))})
        return record

    if tail is not None:
        try:
            existing = pd.read_feather(path)
            existing["date"] = pd.to_datetime(existing["date"])
            if existing["date"].dt.tz is not None:
                existing["date"] = existing["date"].dt.tz_convert("UTC").dt.tz_localize(None)
            merged = (
                pd.concat([existing, fresh], ignore_index=True)
                .drop_duplicates(subset=["date"], keep="last")
                .sort_values("date")
                .reset_index(drop=True)
            )
        except Exception as exc:  # noqa: BLE001 - never clobber an unreadable file
            record.update({"error": f"merge failed, nothing written: {exc}"})
            return record
    else:
        merged = fresh.reset_index(drop=True)

    problems = _validate(merged, symbol)
    if problems:
        record.update({"error": "merged validation failed: " + ", ".join(problems)})
        return record

    try:
        _write_atomic(merged, path)
    except Exception as exc:  # noqa: BLE001
        record.update({"error": f"write failed: {exc}"})
        return record

    record.update(
        {
            "status": "updated" if tail is not None else "new",
            "rows": int(len(merged)),
            "added": int(len(merged) - existing_rows),
            "first": str(merged["date"].iloc[0].date()),
            "last": str(merged["date"].iloc[-1].date()),
            "elapsed_s": round(time.time() - started, 1),
        }
    )
    return record


_THREAD_CLIENTS: Dict[int, Any] = {}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    trade_types = [t.strip() for t in args.trade_types.split(",") if t.strip()]
    for trade_type in trade_types:
        if trade_type not in {"spot", "futures"}:
            _log(f"error: unsupported trade type {trade_type!r}", err=True)
            return 2

    now = utc_now_naive()
    if args.until:
        until = dt.date.fromisoformat(args.until)
    else:
        # Latest bar whose window has closed.
        until = now.normalize().date()
        if not bar_is_closed(pd.Timestamp(until), args.timeframe, now=now):
            until -= dt.timedelta(days=1)
    start = dt.date.fromisoformat(args.start)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    exit_code = 0
    for trade_type in trade_types:
        markets, upath = load_or_discover(
            args.universe_dir, args.exchange, trade_type, args.timeframe, args.refresh_universe
        )
        if args.symbols:
            wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
            markets = [m for m in markets if m["symbol"] in wanted]
        if args.limit:
            markets = markets[: args.limit]

        _log(
            f"[plan] {args.exchange}/{trade_type} {args.timeframe}: {len(markets)} symbol(s), "
            f"{start}..{until}"
        )
        if args.discover_only or args.dry_run:
            for market in markets[:10]:
                _log(f"  - {market['symbol']}")
            if len(markets) > 10:
                _log(f"  ... {len(markets) - 10} more")
            continue

        jsonl_path = report_dir / f"{args.exchange}-{trade_type}-{args.timeframe}-{stamp}.jsonl"
        results: List[Dict[str, Any]] = []
        done = 0
        total = len(markets)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    download_one,
                    market,
                    trade_type=trade_type,
                    timeframe=args.timeframe,
                    start=start,
                    until=until,
                    data_root=args.data_root,
                    exchange_id=args.exchange,
                    retries=args.retries,
                    now=now,
                ): market
                for market in markets
            }
            for future in as_completed(futures):
                market = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001
                    record = {
                        "symbol": market["symbol"],
                        "trade_type": trade_type,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    traceback.print_exc()
                record["ts"] = dt.datetime.now().isoformat(timespec="seconds")
                results.append(record)
                with open(jsonl_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                done += 1
                if record["status"] in {"failed", "empty"}:
                    _log(f"[{done}/{total}] FAIL {record['symbol']}: {record.get('error')}", err=True)
                elif done % 25 == 0 or done == total:
                    _log(f"[{done}/{total}] {record['symbol']} {record['status']} -> {record.get('last')}")

        buckets: Dict[str, int] = {}
        for record in results:
            buckets[record["status"]] = buckets.get(record["status"], 0) + 1
        summary = {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "exchange": args.exchange,
            "trade_type": trade_type,
            "timeframe": args.timeframe,
            "universe_file": str(upath),
            "start": start.isoformat(),
            "until": until.isoformat(),
            "symbols": total,
            "status_counts": buckets,
            "rows_total": int(sum(r.get("rows") or 0 for r in results if r["status"] != "failed")),
            "failures": [
                {"symbol": r["symbol"], "error": r.get("error")} for r in results if r["status"] in {"failed", "empty"}
            ],
        }
        summary_path = report_dir / f"{args.exchange}-{trade_type}-{args.timeframe}-{stamp}-summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"[summary] {trade_type}: {buckets} -> {summary_path}")
        if buckets.get("failed") or buckets.get("empty"):
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
