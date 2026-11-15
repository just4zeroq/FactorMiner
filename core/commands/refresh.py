"""``factorminer refresh`` — bring the local library up to T-1.

Thin CLI wrapper around ``scripts/update_data.py``, which is the single tested
implementation of "what is missing, download it, trim the half-formed tail".
Duplicating that planning logic in ``core/`` would create a second source of
truth for the one thing the operating discipline depends on, so this module
only maps arguments and normalises the exit code.

Why it exists as a command at all: every mining run must start from data that
is current, otherwise the panel silently trails the market and the "latest
data" rule becomes a piece of folklore. A scheduler should call this, then
``factorminer experiment`` (or just ``factorminer pipeline``).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from typing import List, Optional

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
)


def _load_update_module():
    """Import ``scripts/update_data.py`` by path (``scripts`` is not a package)."""
    name = "_factorminer_update_data"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_SCRIPTS_DIR, "update_data.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def refresh_argv(args: argparse.Namespace) -> List[str]:
    """Translate the public CLI namespace into ``update_data`` flags."""
    argv: List[str] = ["--config", str(args.config)]
    if getattr(args, "until", None):
        argv += ["--until", args.until]
    if getattr(args, "start", None):
        argv += ["--start", args.start]
    if getattr(args, "symbols", None):
        argv += ["--symbols", args.symbols]
    if getattr(args, "timeframe", None):
        argv += ["--timeframe", args.timeframe]
    if getattr(args, "data_root", None):
        argv += ["--data-root", args.data_root]
    if getattr(args, "retries", None) is not None:
        argv += ["--retries", str(args.retries)]
    if getattr(args, "report_dir", None):
        argv += ["--report-dir", args.report_dir]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    return argv


def run_refresh_cmd(args: argparse.Namespace) -> int:
    """Execute the refresh. Returns 0 only when every symbol is current."""
    module = _load_update_module()
    argv = refresh_argv(args)
    print("[refresh] " + " ".join(argv), flush=True)
    ohlcv_code = int(module.main(argv))

    if getattr(args, "skip_streams", False):
        return ohlcv_code
    stream_code = refresh_crypto_streams(
        args.config,
        data_root=getattr(args, "data_root", "data") or "data",
        until=getattr(args, "until", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        symbols=getattr(args, "symbols", None),
    )
    return ohlcv_code or stream_code


def _config_streams(config_path: str):
    import json

    with open(config_path, encoding="utf-8-sig") as handle:
        config = json.load(handle)
    feeds = config.get("data_feeds") or {}
    streams = [s for s in (feeds.get("crypto_streams") or []) if isinstance(s, str)]
    pairs = [p for p in (feeds.get("pairs") or []) if isinstance(p, str) and p.strip()]
    periods = list(feeds.get("mine_period") or []) + list(feeds.get("test_period") or [])
    starts = [
        str(period[0]) for period in periods
        if isinstance(period, (list, tuple)) and period
    ]
    return {
        "streams": streams,
        "pairs": pairs,
        "exchange": feeds.get("exchange", "binance"),
        "trade_type": feeds.get("instrument_type", "futures"),
        "timeframe": feeds.get("timeframe", "1d"),
        "earliest": min(starts) if starts else None,
    }


def refresh_crypto_streams(
    config_path: str,
    *,
    data_root: str = "data",
    until: Optional[str] = None,
    dry_run: bool = False,
    symbols: Optional[str] = None,
) -> int:
    """Bring the config's declared crypto streams up to ``until`` (default T-1).

    ``RealDataClient`` attaches streams with a past-only forward fill and skips
    missing files, so a stale or partially downloaded stream set does not fail
    anything — it just quietly shrinks the search space (funding / OI / basis
    become flat or absent). Refreshing them here keeps that promise honest.
    Returns 0 when every (pair, stream) is current.
    """
    import datetime
    from pathlib import Path

    import pandas as pd

    meta = _config_streams(config_path)
    streams = meta["streams"]
    if not streams:
        return 0
    pairs = meta["pairs"]
    if symbols:
        pairs = [s.strip() for s in symbols.split(",") if s.strip()]

    end = (
        pd.Timestamp(str(until))
        if until
        else pd.Timestamp(datetime.date.today() - datetime.timedelta(days=1))
    )
    floor = pd.Timestamp(meta["earliest"]) if meta["earliest"] else None

    from core.data_feed.crypto_streams import (
        crypto_stream_path,
        materialize_crypto_streams,
    )

    failures = 0
    for pair in pairs:
        start = None
        for stream in streams:
            try:
                path = crypto_stream_path(
                    Path(data_root), meta["exchange"], pair,
                    meta["timeframe"], meta["trade_type"], stream,
                )
            except Exception:
                path = None
            if path is not None and path.exists():
                try:
                    last = pd.to_datetime(
                        pd.read_feather(path, columns=["date"])["date"]
                    ).max()
                    if pd.notna(last):
                        candidate = last + pd.Timedelta(days=1)
                        start = candidate if start is None else min(start, candidate)
                except Exception:
                    pass
        if start is None:
            start = floor
        if start is None:
            print(f"[streams] {pair}: no start date (no file, no period) — skipped")
            failures += 1
            continue
        if start > end:
            print(f"[streams] {pair}: current through {start.date()} — nothing to do")
            continue
        if dry_run:
            print(f"[streams] {pair}: would fetch {start.date()}..{end.date()} ({', '.join(streams)})")
            continue
        status = materialize_crypto_streams(
            Path(data_root),
            meta["exchange"],
            pair,
            meta["timeframe"],
            meta["trade_type"],
            start,
            end + pd.Timedelta(days=1),
            streams=streams,
        )
        bad = [name for name, info in status.items() if not info.get("ok")]
        rows = sum(int(info.get("rows") or 0) for info in status.values())
        if bad:
            failures += 1
            print(f"[streams] {pair}: FAILED {', '.join(bad)}")
        else:
            print(f"[streams] {pair}: {start.date()}..{end.date()} +{rows} row(s)")
    if failures:
        print(f"[streams] {failures} pair(s) not current", flush=True)
        return 1
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "refresh",
        help="Bring the local OHLCV library up to T-1 for a config's universe",
    )
    parser.add_argument("--config", required=True, help="Mining config (universe/timeframe come from it)")
    parser.add_argument("--until", default=None, help="Last bar to keep, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--start", default=None, help="Floor for symbols with no local file")
    parser.add_argument("--symbols", default=None, help="Comma-separated override of the config universe")
    parser.add_argument("--timeframe", default=None, help="Override the config timeframe")
    parser.add_argument("--data-root", default="data", help="Local data root (default: data)")
    parser.add_argument("--retries", type=int, default=3, help="Attempts per symbol (default: 3)")
    parser.add_argument("--report-dir", default=None, help="Where the refresh report is written")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without downloading")
    parser.add_argument(
        "--skip-streams",
        action="store_true",
        help="Do not refresh data_feeds.crypto_streams (funding/open_interest/basis)",
    )
    parser.set_defaults(func=run_refresh_cmd)
