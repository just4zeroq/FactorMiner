"""``factorminer factors`` — why did the gates accept or reject these factors?

The verdicts live in ``<run>/factor_db/metadata/*.json`` as
``metrics.gate_checks`` / ``metrics.gate_pass``. Reading a 100 KB JSON by hand
to answer "was it DSR or the empirical null?" is not an operational workflow,
and the Web endpoints that answer it are not part of the production CLI.
This command turns those verdicts into a ranked table and a counter of failed
checks, without recomputing anything.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Any, Dict, List, Optional

def run_dir_for(
    run_id: str, *, user_dir: str = "user_workspace", output_root: Optional[str] = None
) -> str:
    root = output_root or os.path.join(user_dir or "user_workspace", "runs")
    return os.path.join(root, str(run_id))


def _run_dir(args: argparse.Namespace) -> str:
    return run_dir_for(
        args.run_id,
        user_dir=getattr(args, "user_dir", "user_workspace"),
        output_root=getattr(args, "output_root", None),
    )


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if out == out and abs(out) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _record(raw: Dict[str, Any]) -> Dict[str, Any]:
    metrics = raw.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    checks = metrics.get("gate_checks") or {}
    if not isinstance(checks, dict):
        checks = {}
    failed = sorted(str(k) for k, ok in checks.items() if not ok)
    if not checks and metrics.get("gate_pass") is False:
        failed = ["gate_pass_unspecified"]
    return {
        "factor_id": raw.get("factor_id") or "",
        "lifecycle_status": raw.get("lifecycle_status") or "",
        "gate_pass": bool(metrics.get("gate_pass", False)),
        "hac_t": _number(metrics.get("hac_t")),
        "p_adj": _number(metrics.get("p_adj")),
        "dsr": _number(metrics.get("dsr")),
        "empirical_p": _number(metrics.get("empirical_p")),
        "RankIC": _number(metrics.get("RankIC")),
        "RankIC_oos": _number(metrics.get("RankIC_oos")),
        "fitness_score": _number(metrics.get("fitness_score")),
        "cost_adj_return": _number(metrics.get("cost_adj_return")),
        "miner_type": raw.get("miner_type") or "",
        "logic_hash": raw.get("logic_hash") or "",
        "created_at": raw.get("created_at") or "",
        "gate_checks": {str(k): bool(v) for k, v in checks.items()},
        "failed_checks": failed,
    }


def collect(
    run_id: str,
    *,
    user_dir: str = "user_workspace",
    output_root: Optional[str] = None,
    db_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Read verdicts from one run's isolated factor_db. No gates are rerun."""
    root = output_root or os.path.join(user_dir or "user_workspace", "runs")
    run_dir = os.path.join(root, str(run_id))
    meta_dir = db_root or os.path.join(run_dir, "factor_db", "metadata")
    if not os.path.isdir(meta_dir):
        raise FileNotFoundError(
            f"No factor metadata at {meta_dir}. Check --run-id / --user-dir "
            "(the library lives at <run_dir>/factor_db/metadata)."
        )

    records: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    for name in sorted(os.listdir(meta_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(meta_dir, name)
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                records.append(_record(json.load(handle)))
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append(f"{name}: {exc}")
    records.sort(
        key=lambda r: (r["gate_pass"], r["fitness_score"] if r["fitness_score"] is not None else -1e18),
        reverse=True,
    )
    failures = collections.Counter(
        reason for row in records for reason in row["failed_checks"]
    )
    statuses = collections.Counter(row["lifecycle_status"] for row in records)
    return {
        "run_id": run_id,
        "run_dir": os.path.abspath(run_dir),
        "metadata_dir": os.path.abspath(meta_dir),
        "total": len(records),
        "n_passed": sum(1 for row in records if row["gate_pass"]),
        "by_status": dict(sorted(statuses.items())),
        "reject_reasons": dict(failures.most_common()),
        "unreadable": unreadable,
        "factors": records,
    }


def _fmt(value: Any, width: int = 9) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:.4g}".rjust(width)
    return str(value)[:width].rjust(width)


def _print_report(payload: Dict[str, Any], *, show_rejects: bool, limit: int) -> None:
    print(f"run          : {payload['run_id']}")
    print(f"library      : {payload['metadata_dir']}")
    print(
        f"factors      : {payload['total']} total, {payload['n_passed']} passed, "
        f"{payload['total'] - payload['n_passed']} rejected"
    )
    print(f"lifecycle    : {payload['by_status']}")
    print("reject gates : " + (
        ", ".join(f"{k}={v}" for k, v in payload["reject_reasons"].items())
        or "none"
    ))
    rows = payload["factors"] if show_rejects else payload["factors"][:limit]
    if not rows:
        return
    print()
    print(
        f"{'factor_id':<18} {'status':<22} {'gate':>5} {'hac_t':>9} "
        f"{'p_adj':>9} {'dsr':>9} {'emp_p':>9} {'fitness':>9}  failed"
    )
    for row in rows:
        print(
            f"{row['factor_id']:<18} {row['lifecycle_status']:<22} "
            f"{('PASS' if row['gate_pass'] else 'FAIL'):>5} "
            f"{_fmt(row['hac_t'])} {_fmt(row['p_adj'])} {_fmt(row['dsr'])} "
            f"{_fmt(row['empirical_p'])} {_fmt(row['fitness_score'])}  "
            f"{','.join(row['failed_checks']) or '-'}"
        )
    if not show_rejects and payload["total"] > len(rows):
        print(f"... {payload['total'] - len(rows)} more; use --show-rejects")


def run_factors_cmd(args: argparse.Namespace) -> int:
    try:
        payload = collect(
            args.run_id,
            user_dir=getattr(args, "user_dir", "user_workspace"),
            output_root=getattr(args, "output_root", None),
            db_root=getattr(args, "db_root", None),
        )
    except FileNotFoundError as exc:
        print(f"factors: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        _print_report(
            payload,
            show_rejects=bool(getattr(args, "show_rejects", False)),
            limit=int(getattr(args, "limit", 20) or 20),
        )
    if not payload["total"]:
        print("factors: no factor metadata found in this run", file=sys.stderr)
        return 1
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "factors", help="Rank a run's factors and explain gate rejections"
    )
    parser.add_argument("--run-id", required=True, help="Run id under <user-dir>/runs")
    parser.add_argument("--user-dir", default="user_workspace", help="User workspace")
    parser.add_argument("--output-root", default=None, help="Override the runs root")
    parser.add_argument("--db-root", default=None, help="Override the run's factor_db root")
    parser.add_argument("--show-rejects", action="store_true", help="List every candidate")
    parser.add_argument("--limit", type=int, default=20, help="Rows in the default table")
    parser.add_argument("--json", action="store_true", help="Machine-readable verdicts")
    parser.set_defaults(func=run_factors_cmd)


__all__ = ["add_parser", "collect", "run_factors_cmd"]
