"""``factorminer pipeline`` — one unattended command for a production run.

The operating loop is always the same three steps, in this order:

1. **refresh** — bring every configured symbol up to the last closed bar (T-1);
2. **doctor** — refuse to start a 40-minute run on a short, stale or
   misconfigured panel;
3. **experiment** — pre-register → mine → gates → walk-forward → OOS → backtest
   → report.

Doing it by hand is exactly where discipline leaks: a skipped refresh mines a
stale panel, a skipped doctor discovers a missing pair after an hour. This
command runs the three in sequence, writes a machine-readable summary a
scheduler can alert on, and returns a single exit code.

Exit codes: ``0`` everything ran, ``1`` a blocking failure (doctor fatal or the
experiment failed), ``2`` usage error. A *refresh* failure alone is a warning —
mining still proceeds on whatever is locally current, and the summary records
exactly which symbols were stale, so a nightly job can page someone without
losing the run. ``--stale-is-fatal`` turns both signals into a hard stop before
mining. The data root is read from ``data_feeds.data_root`` unless explicitly
overridden: defaulting it to ``data`` once again let the doctor check one
library while the miner opened another.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from typing import Any, Dict, List, Optional


def _stage(name: str, code: int, detail: str = "") -> Dict[str, Any]:
    return {"stage": name, "exit_code": int(code), "detail": detail}


def _experiment_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        config=args.config,
        miner=getattr(args, "miner", None),
        iterations=getattr(args, "iterations", None),
        seed=getattr(args, "seed", None),
        resume=bool(getattr(args, "resume", False)),
        run_id=getattr(args, "run_id", None),
        user_dir=getattr(args, "user_dir", None),
        output_root=getattr(args, "output_root", None),
        holdout_mode=getattr(args, "holdout_mode", None),
        allow_holdout_reuse=bool(getattr(args, "allow_holdout_reuse", False)),
        # ``pipeline --data-root`` must reach the panel loader too, otherwise the
        # doctor step validates a directory the experiment never opens.
        data_root=getattr(args, "data_root", None),
        preflight=False,
    )


def _refresh_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        config=args.config,
        until=getattr(args, "until", None),
        start=None,
        symbols=None,
        timeframe=None,
        data_root=getattr(args, "data_root", None),
        retries=getattr(args, "retries", 3),
        report_dir=None,
        dry_run=bool(getattr(args, "dry_run", False)),
        skip_streams=bool(getattr(args, "skip_streams", False)),
    )


def _write_summary(summary: Dict[str, Any], directory: str) -> Optional[str]:
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = os.path.join(directory, f"pipeline_{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False, default=str)
        return path
    except Exception:
        return None


def _resolve_root(args: argparse.Namespace) -> str:
    """One data root for refresh + doctor + experiment (config unless overridden)."""
    from core.data_feed.naming import resolve_data_root

    config: Dict[str, Any] = {}
    try:
        with open(args.config, "r", encoding="utf-8-sig") as handle:
            config = json.load(handle) or {}
    except (OSError, json.JSONDecodeError):
        pass  # doctor will report the unreadable config as FATAL
    declared = getattr(args, "data_root", None) or (config.get("data_feeds") or {}).get("data_root")
    return str(resolve_data_root(declared))


def _abort_stale(
    summary: Dict[str, Any], reason: str, summary_dir: str
) -> int:
    """Stop before mining when --stale-is-fatal is set and data is not current."""
    summary["stale"] = reason
    summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    summary["exit_code"] = 1
    print(f"\nRefusing to mine on stale data: {reason}", flush=True)
    path = _write_summary(summary, summary_dir)
    if path:
        print(f"summary: {path}", flush=True)
    return 1


def run_pipeline_cmd(args: argparse.Namespace) -> int:
    from core.commands import doctor as doctor_cmd
    from core.commands import refresh as refresh_cmd
    from core.commands.run_experiment import run_experiment_cmd

    started = datetime.datetime.now()
    stages: List[Dict[str, Any]] = []
    data_root = _resolve_root(args)
    summary: Dict[str, Any] = {
        "config": args.config,
        "data_root": data_root,
        "stale_is_fatal": bool(getattr(args, "stale_is_fatal", False)),
        "started_at": started.isoformat(timespec="seconds"),
        "stages": stages,
    }

    # 1. refresh -----------------------------------------------------------
    if getattr(args, "skip_refresh", False):
        stages.append(_stage("refresh", 0, "skipped (--skip-refresh)"))
    else:
        print("\n=== [1/3] REFRESH ===", flush=True)
        code = refresh_cmd.run_refresh_cmd(_refresh_namespace(args))
        stages.append(
            _stage(
                "refresh",
                code,
                "dry-run" if getattr(args, "dry_run", False) else ("ok" if code == 0 else "stale symbols remain"),
            )
        )
        if (
            code != 0
            and getattr(args, "stale_is_fatal", False)
            and not getattr(args, "dry_run", False)
        ):
            return _abort_stale(
                summary, "refresh could not bring every symbol up to T-1", args.summary_dir
            )

    # 2. doctor ------------------------------------------------------------
    if getattr(args, "skip_doctor", False):
        stages.append(_stage("doctor", 0, "skipped (--skip-doctor)"))
    else:
        print("\n=== [2/3] DOCTOR ===", flush=True)
        rows = doctor_cmd.run_checks(
            args.config,
            data_root=data_root,
            user_dir=getattr(args, "user_dir", None) or "user_workspace",
        )
        doctor_cmd._print_table(rows)
        fatal = [r for r in rows if r["status"] == doctor_cmd.FATAL]
        summary["doctor"] = rows
        stages.append(
            _stage(
                "doctor",
                1 if fatal else 0,
                f"{len(fatal)} fatal, {sum(1 for r in rows if r['status'] == doctor_cmd.WARN)} warn",
            )
        )
        if fatal:
            print("\nRefusing to start: fix the FATAL checks above.", flush=True)
            summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            summary["exit_code"] = 1
            path = _write_summary(summary, args.summary_dir)
            if path:
                print(f"summary: {path}", flush=True)
            return 1
        stale_rows = [r["detail"] for r in rows if r["check"] == "freshness" and r["status"] == doctor_cmd.WARN]
        if stale_rows and getattr(args, "stale_is_fatal", False):
            return _abort_stale(summary, f"freshness: {stale_rows[0]}", args.summary_dir)

    if getattr(args, "dry_run", False):
        stages.append(_stage("experiment", 0, "skipped (--dry-run)"))
        summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        summary["exit_code"] = 0
        path = _write_summary(summary, args.summary_dir)
        if path:
            print(f"summary: {path}", flush=True)
        return 0

    # 3. experiment --------------------------------------------------------
    print("\n=== [3/3] EXPERIMENT ===", flush=True)
    try:
        result = run_experiment_cmd(_experiment_namespace(args))
    except SystemExit as exc:  # a hard validation exit inside the command
        code = int(exc.code or 1)
        stages.append(_stage("experiment", code, "aborted"))
        summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        summary["exit_code"] = code
        _write_summary(summary, args.summary_dir)
        return code

    state = (result or {}).get("state", {}) if isinstance(result, dict) else {}
    mining = state.get("mining", {}) if isinstance(state, dict) else {}
    status = str(state.get("status", "")).lower() if isinstance(state, dict) else ""
    experiment_failed = status in {"failed", "error", "cancelled", "interrupted"}
    stages.append(
        _stage(
            "experiment",
            1 if experiment_failed else 0,
            f"run_id={result.get('run_id')}" if isinstance(result, dict) else "done",
        )
    )
    summary.update(
        {
            "run_id": result.get("run_id") if isinstance(result, dict) else None,
            "run_dir": result.get("run_dir") if isinstance(result, dict) else None,
            "candidates": mining.get("n_candidates"),
            "promoted": mining.get("n_promoted"),
            "status": status or None,
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    )

    exit_code = 1 if experiment_failed else 0
    if getattr(args, "fail_if_no_promotion", False) and not int(mining.get("n_promoted") or 0):
        exit_code = 1
        summary["no_promotion"] = True
    summary["exit_code"] = exit_code

    duration = (datetime.datetime.now() - started).total_seconds()
    print("\n=== PIPELINE SUMMARY ===")
    for stage in stages:
        print(f"  {stage['stage']:<12} exit={stage['exit_code']}  {stage['detail']}")
    print(f"  duration     {duration:.0f}s")
    path = _write_summary(summary, args.summary_dir)
    if path:
        print(f"  summary      {path}")
    return exit_code


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "pipeline",
        help="Unattended production run: refresh -> doctor -> experiment",
    )
    parser.add_argument("--config", required=True, help="Mining config")
    parser.add_argument("--miner", default=None, help="Override the config paradigm")
    parser.add_argument("--iterations", type=int, default=None, help="Override max_iterations")
    parser.add_argument("--seed", type=int, default=None, help="Fix the RNG seed")
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted run")
    parser.add_argument("--run-id", default=None, help="Explicit run id")
    parser.add_argument("--user-dir", default=None, help="Override the user workspace")
    parser.add_argument("--output-root", default=None, help="Override the runs root")
    parser.add_argument(
        "--data-root", default=None,
        help="Override data_feeds.data_root (default: honour the config)",
    )
    parser.add_argument("--until", default=None, help="Refresh up to this date (default: yesterday)")
    parser.add_argument("--retries", type=int, default=3, help="Download attempts per symbol")
    parser.add_argument("--holdout-mode", default=None, choices=["strict", "audit", "off"])
    parser.add_argument("--allow-holdout-reuse", action="store_true")
    parser.add_argument("--skip-refresh", action="store_true", help="Do not touch the data library")
    parser.add_argument(
        "--skip-streams",
        action="store_true",
        help="Refresh OHLCV but not data_feeds.crypto_streams",
    )
    parser.add_argument("--skip-doctor", action="store_true", help="Do not preflight")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refresh plan + doctor only, no mining",
    )
    parser.add_argument(
        "--fail-if-no-promotion",
        action="store_true",
        help="Exit non-zero when the gates promoted nothing (for alerting)",
    )
    parser.add_argument(
        "--stale-is-fatal",
        action="store_true",
        help="Abort before mining when refresh fails or freshness is WARN",
    )
    parser.add_argument(
        "--summary-dir",
        default=os.path.join("user_workspace", "reports"),
        help="Where pipeline_<ts>.json is written",
    )
    parser.set_defaults(func=run_pipeline_cmd)
