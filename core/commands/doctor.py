"""``factorminer doctor`` — preflight before a long run.

A mining experiment runs for tens of minutes and only fails at the very end if
the panel was short, a symbol was missing, or the config never declared a
required block. Every one of those is knowable in a second. ``doctor`` runs the
checks a production job would otherwise discover too late, prints a compact
table, and exits non-zero when anything is blocking.

Checks (FATAL blocks the run, WARN is reported but allows it):

``config``            file parses and ``validate_mining_startup`` accepts it
``universe``          every configured pair has a readable OHLCV file
``coverage``          each configured window is backed by closed bars
``freshness``         newest closed bar vs T-1 (stale data is a WARN by default)
``crypto_streams``    declared streams exist on disk (attach degrades silently)
``data_quality``      ``data_quality.strict`` declared (fail-closed quality gate)
``holdout``           mode, ledger and the pre-registered window plan
``environment``       writability, ``FACTORMINER_PRODUCTION``, optional deps
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from typing import Any, Dict, List, Optional

FATAL = "FATAL"
WARN = "WARN"
OK = "OK"

# Loading user modules compiles every plugin in the workspace; a preflight runs
# several checks against the same directory, so keep the report for the process
# lifetime instead of re-importing on every call.
_LOAD_REPORTS: Dict[str, Any] = {}


def _load_report(user_dir: str):
    from core.utils.dynamic_loader import load_user_modules

    key = os.path.abspath(user_dir)
    if key not in _LOAD_REPORTS:
        _LOAD_REPORTS[key] = load_user_modules(user_dir)
    return _LOAD_REPORTS[key]

def _result(check: str, status: str, detail: str) -> Dict[str, str]:
    return {"check": check, "status": status, "detail": detail}


def _read_closed_dates(path: str, timeframe: str, now) -> Optional[Any]:
    """Closed bar timestamps of a feather file (None when unreadable)."""
    import pandas as pd

    from core.data_feed.bar_clock import closed_only

    try:
        frame = pd.read_feather(path, columns=["date"])
    except Exception:
        try:
            frame = pd.read_feather(path)
        except Exception:
            return None
    if frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return pd.DatetimeIndex(closed_only(list(dates.unique()), timeframe, now=now))


def _check_config(config_path: str, user_dir: str, miner: Optional[str] = None):
    from core.startup_validation import StartupValidationError, validate_mining_startup

    if not os.path.exists(config_path):
        return None, [_result("config", FATAL, f"not found: {config_path}")]
    try:
        with open(config_path, encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except Exception as exc:
        return None, [_result("config", FATAL, f"cannot parse: {exc}")]

    # Mirror the experiment command exactly: an explicit --miner wins, otherwise
    # a config without ``paradigm`` runs GP rather than failing validation here.
    if miner:
        config["paradigm"] = miner
    elif not config.get("paradigm"):
        config["paradigm"] = "GP"

    try:
        validate_mining_startup(config, _load_report(user_dir))
    except StartupValidationError as exc:
        return config, [_result("config", FATAL, str(exc))]
    except Exception as exc:  # a broken plugin must not hide the data checks
        return config, [_result("config", WARN, f"validation skipped: {exc}")]
    return config, [_result("config", OK, "parsed and validated")]


def _check_universe(config: Dict[str, Any], data_root: str, now):
    from core.data_feed.naming import data_path

    feeds = config.get("data_feeds") or {}
    exchange = feeds.get("exchange", "binance")
    trade_type = feeds.get("instrument_type", "futures")
    timeframe = feeds.get("timeframe", "1d")
    pairs = [p for p in (feeds.get("pairs") or []) if isinstance(p, str) and p.strip()]
    if not pairs:
        return {}, [_result("universe", FATAL, "data_feeds.pairs is empty")]

    results: List[Dict[str, str]] = []
    coverage: Dict[str, Any] = {}
    missing: List[str] = []
    unreadable: List[str] = []
    for pair in pairs:
        try:
            path = str(data_path(data_root, exchange, pair, timeframe, trade_type))
        except Exception as exc:
            unreadable.append(f"{pair} ({exc})")
            continue
        if not os.path.exists(path):
            missing.append(pair)
            continue
        dates = _read_closed_dates(path, timeframe, now)
        if dates is None or len(dates) == 0:
            unreadable.append(pair)
            continue
        coverage[pair] = dates

    if missing:
        results.append(
            _result("universe", FATAL, f"{len(missing)} pair(s) have no file: {', '.join(missing[:5])}")
        )
    if unreadable:
        results.append(
            _result("universe", FATAL, f"{len(unreadable)} pair(s) unreadable: {', '.join(unreadable[:5])}")
        )
    if not missing and not unreadable:
        results.append(
            _result("universe", OK, f"{len(pairs)} pair(s) present @ {timeframe}")
        )
    return coverage, results


def _check_coverage(config: Dict[str, Any], coverage: Dict[str, Any], now):
    import pandas as pd

    from core.data_feed.bar_clock import bar_delta

    feeds = config.get("data_feeds") or {}
    periods = []
    for key in ("mine_period", "test_period"):
        for entry in feeds.get(key) or []:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                periods.append((key, str(entry[0]), str(entry[1])))
    if not periods:
        return [_result("coverage", WARN, "no mine_period/test_period declared")]
    if not coverage:
        return [_result("coverage", FATAL, "no readable pair to measure")]

    results = []
    for label, start, end in periods:
        try:
            lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        except Exception:
            results.append(_result("coverage", FATAL, f"{label} '{start}..{end}' is not a date"))
            continue
        gaps = []
        for pair, dates in coverage.items():
            if dates.min() > lo:
                gaps.append(f"{pair} starts {dates.min().date()}")
            elif dates.max() < hi:
                gaps.append(f"{pair} ends {dates.max().date()}")
        if gaps:
            results.append(
                _result(
                    "coverage",
                    FATAL,
                    f"{label} {start}..{end}: {len(gaps)} pair(s) do not cover it "
                    f"({'; '.join(gaps[:3])})",
                )
            )
        else:
            results.append(
                _result(
                    "coverage",
                    OK,
                    f"{label} {start}..{end} covered by {len(coverage)} pair(s)",
                )
            )

    # Freshness of the whole panel: the newest closed bar across the universe.
    newest = min(dates.max() for dates in coverage.values())
    yesterday = (now - bar_delta("1d")).normalize()
    lag_days = int((yesterday - newest).days)
    status = OK if lag_days <= 1 else WARN
    results.append(
        _result(
            "freshness",
            status,
            f"newest closed bar {newest.date()} (T-1 = {yesterday.date()}, lag {lag_days}d)",
        )
    )
    return results


def _check_streams(config: Dict[str, Any], data_root: str):
    from core.data_feed.crypto_streams import (
        CRYPTO_STREAM_NAMES,
        crypto_stream_path,
    )

    feeds = config.get("data_feeds") or {}
    declared = [s for s in (feeds.get("crypto_streams") or []) if isinstance(s, str)]
    if not declared:
        return [
            _result(
                "crypto_streams",
                WARN,
                "none declared: mining sees OHLCV only (funding/OI/basis stay unused)",
            )
        ]
    unknown = [s for s in declared if s not in CRYPTO_STREAM_NAMES]
    if unknown:
        return [_result("crypto_streams", FATAL, f"unknown stream(s): {', '.join(unknown)}")]

    exchange = feeds.get("exchange", "binance")
    trade_type = feeds.get("instrument_type", "futures")
    timeframe = feeds.get("timeframe", "1d")
    pairs = [p for p in (feeds.get("pairs") or []) if isinstance(p, str)]
    missing = 0
    checked = 0
    for pair in pairs:
        for stream in declared:
            checked += 1
            try:
                path = crypto_stream_path(
                    data_root, exchange, pair, timeframe, trade_type, stream
                )
            except Exception:
                missing += 1
                continue
            if not path.exists():
                missing += 1
    if missing:
        # ``_attach_crypto_streams`` skips missing files silently, so a partially
        # downloaded stream set quietly shrinks the search space.
        return [
            _result(
                "crypto_streams",
                WARN,
                f"{missing}/{checked} stream file(s) missing; those columns will be absent",
            )
        ]
    return [_result("crypto_streams", OK, f"{checked} stream file(s) present")]


def _check_data_quality(config: Dict[str, Any]):
    quality = config.get("data_quality")
    if not isinstance(quality, dict):
        return [_result("data_quality", WARN, "block missing: quality gate disabled")]
    if not quality.get("strict"):
        return [_result("data_quality", WARN, "strict=false: critical data issues only warn")]
    return [_result("data_quality", OK, "strict=true (critical issues abort the run)")]


def _check_holdout(config: Dict[str, Any], data_root: str = "data"):
    from core.evaluation.holdout_guard import (
        default_ledger_path,
        is_production,
        load_window_spec,
        resolve_mode,
    )
    from core.evaluation.experiment_trace import HoldoutLedger

    results = []
    mode = resolve_mode(config)
    ledger_path = default_ledger_path()
    try:
        revealed = HoldoutLedger(ledger_path).snapshot()
    except Exception as exc:
        results.append(_result("holdout", FATAL, f"cannot read ledger {ledger_path}: {exc}"))
        return results
    status = OK if (mode == "strict" or not is_production()) else FATAL
    results.append(
        _result(
            "holdout",
            status,
            f"mode={mode}, {len(revealed)} revealed window(s), ledger={os.path.basename(ledger_path)}",
        )
    )

    spec = load_window_spec()
    if not spec:
        results.append(_result("holdout_window", WARN, "no experiment_window.json"))
        return results
    try:
        from core.experiment.windows import plan_windows

        # Scope the scan to the configured root: planning reads every OHLCV
        # file's date column, and the default ``data`` root can hold hundreds of
        # symbols unrelated to this run.
        plan = plan_windows(spec, revealed=revealed, data_root=data_root)
    except Exception as exc:
        results.append(_result("holdout_window", WARN, f"plan failed: {exc}"))
        return results
    ready = int(plan.get("n_ready", 0) or 0)
    current = plan.get("current") or {}
    anchor = plan.get("anchor") or {}
    # "0 ready, 0 planned" on its own reads like a broken pre-registration. The
    # honest message is *why*: windows are chunked forward from the anchor, and
    # when the anchor is still in the future there is nothing to chunk yet.
    detail = (
        f"{ready} ready / {len(plan.get('windows', []))} planned; "
        f"anchor {anchor.get('start')} ({anchor.get('source')}) -> "
        f"{current.get('accumulated', 0)}/{current.get('min_bars')} bars"
    )
    if current.get("eta") and not current.get("ready"):
        detail += f", last bar lands {current.get('eta')} (revealable once it closes)"
    if int(current.get("remaining", 0) or 0) and anchor.get("start"):
        detail += f" (needs {current['remaining']} more {current.get('timeframe')} bar(s))"
    results.append(_result("holdout_window", OK if ready else WARN, detail))
    return results


def _check_environment(config: Dict[str, Any], data_root: str):
    from core.evaluation.holdout_guard import PRODUCTION_ENV_VAR, is_production

    results = []
    production = is_production()
    results.append(
        _result(
            "environment",
            OK,
            f"{PRODUCTION_ENV_VAR}={'1' if production else '0'} (production {'ON' if production else 'OFF'})",
        )
    )
    storage = (config.get("storage") or {}).get("db_root") or "factor_db"
    try:
        os.makedirs(storage, exist_ok=True)
        probe = os.path.join(storage, ".doctor_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        results.append(_result("storage", OK, f"{storage} is writable"))
    except Exception as exc:
        results.append(_result("storage", FATAL, f"{storage} not writable: {exc}"))
    if not os.path.isdir(data_root):
        results.append(_result("data_root", FATAL, f"{data_root} does not exist"))
    return results


def run_checks(
    config_path: str,
    *,
    data_root: str = "data",
    user_dir: str = "user_workspace",
    miner: Optional[str] = None,
    now=None,
) -> List[Dict[str, str]]:
    """Run every preflight check and return ``[{check, status, detail}]``."""
    from core.data_feed.bar_clock import utc_now_naive

    now = utc_now_naive(now)
    config, results = _check_config(config_path, user_dir, miner)
    if config is None:
        return results
    coverage, universe_results = _check_universe(config, data_root, now)
    results += universe_results
    results += _check_coverage(config, coverage, now)
    results += _check_streams(config, data_root)
    results += _check_data_quality(config)
    results += _check_holdout(config, data_root)
    results += _check_environment(config, data_root)
    return results


def _print_table(rows: List[Dict[str, str]]) -> None:
    width = max((len(r["check"]) for r in rows), default=5)
    symbols = {OK: " ok ", WARN: "warn", FATAL: "FAIL"}
    for row in rows:
        print(f"[{symbols.get(row['status'], row['status'])}] {row['check']:<{width}}  {row['detail']}")
    fatal = sum(1 for r in rows if r["status"] == FATAL)
    warn = sum(1 for r in rows if r["status"] == WARN)
    print(f"\n{len(rows)} checks: {fatal} fatal, {warn} warn")


def run_doctor_cmd(args: argparse.Namespace) -> int:
    rows = run_checks(
        args.config,
        data_root=args.data_root,
        user_dir=args.user_dir,
        miner=getattr(args, "miner", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        _print_table(rows)
    fatal = any(r["status"] == FATAL for r in rows)
    if fatal:
        return 1
    if getattr(args, "strict", False) and any(r["status"] == WARN for r in rows):
        return 1
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "doctor",
        help="Preflight a config: data coverage, freshness, gates, environment",
    )
    parser.add_argument("--config", required=True, help="Mining config to check")
    parser.add_argument("--data-root", default="data", help="Local data root (default: data)")
    parser.add_argument("--user-dir", default="user_workspace", help="User workspace for custom miners")
    parser.add_argument("--miner", default=None, help="Override the config paradigm (default: config/GP)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument(
        "--strict", action="store_true", help="Treat warnings as failures"
    )
    parser.set_defaults(func=run_doctor_cmd)
