"""Incremental data refresh for the configured universe.

The library is only useful if it is current, and the last mile was manual: open
the UI, pick symbols, pick a date range, hope nothing timed out. An unattended
job needs three properties that a button cannot provide.

* **Idempotent** — a run when everything is already current does nothing and
  exits 0, so an over-eager schedule is harmless.
* **Bounded** — only the tail is fetched (from the last stored bar to T-1).
  Full re-downloads are slow and re-introduce history that was already
  validated.
* **Loud on failure** — exit code 1 plus a JSON report, so a scheduler can
  surface a broken feed instead of letting a silently stale library feed a
  mining run.

T-1 is the default cutoff on purpose. Today's daily bar is still forming, and a
partial bar in the panel is the kind of contamination the quality gate exists to
prevent — so anything beyond the cutoff is trimmed after the download.

Usage::

    python scripts/update_data.py --dry-run          # what would be fetched
    python scripts/update_data.py                    # fill up to yesterday
    python scripts/update_data.py --until 2026-11-13
    python scripts/update_data.py --symbols BTC/USDT:USDT,ETH/USDT:USDT

Schedule it externally (Windows Task Scheduler / cron / systemd timer), for
example daily at 08:10 local time::

    10 8 * * *  cd /srv/FactorMiner && venv/bin/python scripts/update_data.py
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.data_feed.freshness import scan_data_root
from core.data_feed.naming import data_path, safe_symbol

DEFAULT_CONFIG = os.path.join("user_workspace", "configs", "configCrossAsset.json")
DEFAULT_REPORT_DIR = os.path.join("user_workspace", "reports", "data_updates")
DEFAULT_START = "2017-01-01"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill the local library up to T-1 for the configured universe.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"mining config (default: {DEFAULT_CONFIG})")
    parser.add_argument("--until", default=None, help="last bar to keep, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--start", default=None, help=f"start date for symbols with no local file (default: {DEFAULT_START})")
    parser.add_argument("--symbols", default=None, help="comma-separated override of the config universe")
    parser.add_argument("--timeframe", default=None, help="override the config timeframe")
    parser.add_argument("--data-root", default="data", help="local data root (default: data)")
    parser.add_argument("--retries", type=int, default=3, help="attempts per symbol (default: 3)")
    parser.add_argument("--backoff", type=float, default=5.0, help="base backoff seconds, doubled per retry (default: 5)")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, help=f"where reports are written (default: {DEFAULT_REPORT_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="report the plan without downloading")
    return parser.parse_args(argv)


def load_universe(config_path: str) -> Dict[str, Any]:
    """Read exchange, market type, timeframe and pairs from a mining config."""
    if not os.path.exists(config_path):
        raise SystemExit(f"config not found: {config_path}")
    with open(config_path, encoding="utf-8-sig") as handle:
        config = json.load(handle)
    feeds = config.get("data_feeds", {}) or {}
    pairs = [pair for pair in feeds.get("pairs", []) if isinstance(pair, str) and pair.strip()]
    if not pairs:
        raise SystemExit(f"config has no data_feeds.pairs: {config_path}")

    # The earliest configured period is the sensible floor for a symbol that has
    # never been downloaded: mining cannot use anything before it.
    periods = list(feeds.get("mine_period", []) or []) + list(feeds.get("test_period", []) or [])
    starts = [period[0] for period in periods if isinstance(period, (list, tuple)) and period]
    return {
        "exchange": feeds.get("exchange", "binance"),
        "trade_type": feeds.get("instrument_type", "futures"),
        "timeframe": feeds.get("timeframe", "1d"),
        "pairs": pairs,
        "earliest_period": min(starts) if starts else DEFAULT_START,
    }


def plan_updates(
    universe: Dict[str, Any],
    until: datetime.date,
    data_root: str = "data",
    start_override: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Work out, per symbol, whether anything is missing and from when to fetch."""
    exchange = universe["exchange"]
    trade_type = universe["trade_type"]
    timeframe = universe["timeframe"]
    default_start = start_override or universe.get("earliest_period") or DEFAULT_START

    existing: Dict[str, Dict[str, Any]] = {}
    for item in scan_data_root(data_root):
        # Stream files (basis / funding / open interest) share the market type
        # and timeframe of the OHLCV file they belong to; treating one as "the"
        # symbol file would report a symbol as current when its bars are missing.
        if item.get("stream"):
            continue
        if (
            item["exchange"] == exchange
            and item["trade_type"] == trade_type
            and item["timeframe"] == timeframe
        ):
            existing[item["safe_symbol"]] = item

    plan: List[Dict[str, Any]] = []
    for pair in universe["pairs"]:
        key = safe_symbol(pair)
        item = existing.get(key)
        record: Dict[str, Any] = {
            "symbol": pair,
            "safe_symbol": key,
            "timeframe": timeframe,
            "trade_type": trade_type,
        }
        if item is None:
            record.update(
                {
                    "status": "new",
                    "last": None,
                    "start": default_start,
                    "end": (until + datetime.timedelta(days=1)).isoformat(),
                    "missing_days": None,
                }
            )
        else:
            last = datetime.date.fromisoformat(item["last"])
            record["last"] = item["last"]
            record["rows"] = item["rows"]
            if last >= until:
                record.update({"status": "current", "missing_days": 0})
            else:
                # One bar of overlap: it costs nothing (the merge dedupes) and it
                # protects against an off-by-one in exchange timezone handling.
                record.update(
                    {
                        "status": "pending",
                        "start": last.isoformat(),
                        "end": (until + datetime.timedelta(days=1)).isoformat(),
                        "missing_days": (until - last).days,
                    }
                )
        plan.append(record)
    return plan


def trim_after(path: str, cutoff: datetime.date) -> int:
    """Drop bars dated after ``cutoff``; return how many rows were removed.

    The downloader fetches forward from a start timestamp with a row limit, so
    the final batch can reach into the in-progress bar. For an unattended job
    that runs every day that is not theoretical — it would put a partial daily
    candle into the panel.
    """
    if not os.path.exists(path):
        return 0
    frame = pd.read_feather(path)
    if frame.empty or "date" not in frame.columns:
        return 0
    dates = pd.to_datetime(frame["date"], errors="coerce")
    beyond = dates.dt.date > cutoff
    removed = int(beyond.sum())
    if removed:
        frame = frame.loc[~beyond].reset_index(drop=True)
        frame.to_feather(path)
    return removed


def _download_once(record: Dict[str, Any], exchange: str, data_root: str = "data") -> Dict[str, Any]:
    from core.data_feed.batch_downloader import SmartBatchDownloader

    downloader = SmartBatchDownloader(data_root=data_root)
    return downloader.download_ohlcv_batch(
        exchange_id=exchange,
        symbol=record["symbol"],
        timeframe=record["timeframe"],
        start_date=record["start"],
        end_date=record["end"],
        trade_type=record["trade_type"],
        progress_callback=None,
        download_mode="fill_gap",
    )


def run_plan(
    plan: List[Dict[str, Any]],
    universe: Dict[str, Any],
    until: datetime.date,
    data_root: str = "data",
    retries: int = 3,
    backoff: float = 5.0,
) -> List[Dict[str, Any]]:
    """Execute pending rows of the plan, one symbol at a time.

    Serial on purpose: exchanges rate-limit, and a failure here should be
    attributable to one symbol rather than to concurrency.
    """
    results: List[Dict[str, Any]] = []
    for record in plan:
        result = dict(record)
        # "new" means the file does not exist yet, which is exactly the case a
        # fresh symbol needs. Skipping it here would exit 0 having done nothing.
        if record["status"] not in {"pending", "new"}:
            results.append(result)
            continue

        path = data_path(
            data_root,
            universe["exchange"],
            record["symbol"],
            record["timeframe"],
            record["trade_type"],
        )
        last_error = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                response = _download_once(record, universe["exchange"], data_root)
                if response.get("success") is False:
                    raise RuntimeError(response.get("error") or "downloader reported failure")
                last_error = None
                result["attempts"] = attempt
                break
            except Exception as exc:  # noqa: BLE001 - a failed feed must not kill the run
                last_error = f"{type(exc).__name__}: {exc}"
                result["attempts"] = attempt
                if attempt < retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))

        if last_error is not None:
            result.update({"status": "failed", "error": last_error})
            results.append(result)
            continue

        try:
            result["trimmed_rows"] = trim_after(str(path), until)
            # T-04: mirror plan_updates' filters. Stream files share the same
            # safe_symbol as OHLCV; without this filter a funding update could
            # mask a failed OHLCV download and the script would exit 0.
            fresh = {
                item["safe_symbol"]: item
                for item in scan_data_root(data_root)
                if not item.get("stream")
                and item["exchange"] == universe["exchange"]
                and item["trade_type"] == record["trade_type"]
                and item["timeframe"] == record["timeframe"]
            }
            current = fresh.get(record["safe_symbol"])
            if current is None:
                result.update({"status": "failed", "error": "file missing after download"})
            else:
                result["new_last"] = current["last"]
                result["new_rows"] = current["rows"]
                if datetime.date.fromisoformat(current["last"]) >= until:
                    result["status"] = "updated"
                else:
                    result.update(
                        {
                            "status": "partial",
                            "error": f"exchange has no bars through {until.isoformat()}",
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            result.update({"status": "failed", "error": f"verification failed: {exc}"})

        results.append(result)
    return results


def _print_table(results: List[Dict[str, Any]]) -> None:
    header = f"{'symbol':<22}{'last':<12}{'until':<12}{'status':<10}detail"
    print(header)
    print("-" * len(header))
    for item in results:
        detail = ""
        if item["status"] == "updated":
            gap = item.get("missing_days")
            detail = f"+{gap}d -> {item.get('new_last')}" if gap else f"-> {item.get('new_last')}"
            if item.get("trimmed_rows"):
                detail += f" (trimmed {item['trimmed_rows']} partial bar(s))"
        elif item["status"] == "current":
            detail = "already current"
        elif item["status"] == "new":
            detail = f"new file from {item['start']}"
        elif item["status"] == "partial":
            detail = item.get("error", "")
        elif item["status"] == "failed":
            detail = item.get("error", "")
        print(f"{item['symbol']:<22}{str(item.get('last') or '-'):<12}{item['end'][:10] if item.get('end') else '-':<12}{item['status']:<10}{detail}")


def write_report(results: List[Dict[str, Any]], args: argparse.Namespace, until: datetime.date, universe: Dict[str, Any]) -> str:
    os.makedirs(args.report_dir, exist_ok=True)
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "config": args.config,
        "exchange": universe["exchange"],
        "trade_type": universe["trade_type"],
        "timeframe": universe["timeframe"],
        "until": until.isoformat(),
        "dry_run": bool(args.dry_run),
        "summary": {
            "symbols": len(results),
            "updated": sum(1 for item in results if item["status"] == "updated"),
            "current": sum(1 for item in results if item["status"] == "current"),
            "new": sum(1 for item in results if item["status"] == "new"),
            "partial": sum(1 for item in results if item["status"] == "partial"),
            "failed": sum(1 for item in results if item["status"] == "failed"),
            "pending": sum(1 for item in results if item["status"] == "pending"),
            "trimmed_rows": sum(int(item.get("trimmed_rows") or 0) for item in results),
        },
        "items": results,
    }
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(args.report_dir, f"update-{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    # A stable filename makes the scheduler's "did it work" question trivial.
    with open(os.path.join(args.report_dir, "latest.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    until = (
        datetime.date.fromisoformat(args.until)
        if args.until
        else datetime.date.today() - datetime.timedelta(days=1)
    )

    try:
        universe = load_universe(args.config)
    except SystemExit as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.symbols:
        universe["pairs"] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.timeframe:
        universe["timeframe"] = args.timeframe

    print(
        f"{universe['exchange']}/{universe['trade_type']} {universe['timeframe']} "
        f"| {len(universe['pairs'])} symbols | cutoff {until.isoformat()}"
    )
    plan = plan_updates(universe, until, args.data_root, args.start)
    pending = [item for item in plan if item["status"] in {"pending", "new"}]

    if args.dry_run:
        _print_table(plan)
        print(f"\ndry run: {len(pending)} of {len(plan)} symbol(s) need data; nothing downloaded.")
        return 0

    if not pending:
        _print_table(plan)
        print("\nnothing to do: library is current through the cutoff.")
        write_report(plan, args, until, universe)
        return 0

    try:
        results = run_plan(plan, universe, until, args.data_root, args.retries, args.backoff)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - unexpected failure still deserves a report
        traceback.print_exc()
        return 2

    _print_table(results)
    path = write_report(results, args, until, universe)
    unfinished = [item for item in results if item["status"] in {"failed", "partial", "pending", "new"}]
    print(f"\nreport: {path}")
    if unfinished:
        print(f"{len(unfinished)} symbol(s) did not reach the cutoff:", file=sys.stderr)
        for item in unfinished:
            print(f"  {item['symbol']}: {item.get('error') or item['status']}", file=sys.stderr)
        return 1
    print(f"done: {len(results)} symbol(s) current through {until.isoformat()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
