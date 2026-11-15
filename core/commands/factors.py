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


def _expression_of(meta: Dict[str, Any], db_root: Optional[str] = None) -> str:
    """Render the factor's logic as a readable expression.

    ``factors`` used to print only ids and statistics, so answering "what *is*
    ``fac_0622d48d``?" meant opening a 100 KB JSON by hand. This renders the
    stored ``logic_reference`` back into the DSL / source that produced it.

    Best effort by design: an unreadable artifact yields a short placeholder
    instead of failing the whole listing.
    """
    ref = meta.get("logic_reference") or {}
    if not isinstance(ref, dict):
        return ""
    kind = str(ref.get("type") or "")
    try:
        if kind == "json_ast" and isinstance(ref.get("ast"), dict):
            from core.miner.expressions import FactorExpressionAST

            return FactorExpressionAST(ref["ast"]).to_display_string()
        if kind == "python_source":
            source_file = ref.get("source_file") or ""
            if db_root and source_file:
                path = os.path.join(db_root, "sources", os.path.basename(source_file))
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        code = handle.read().strip()
                    return " ".join(code.split())
            return f"<python:{source_file}>" if source_file else "<python_source>"
        if kind == "rl_actions":
            actions = ref.get("actions") or []
            return " ".join(str(a) for a in actions)
        if kind == "nn_channel":
            return (
                f"<nn model={ref.get('model_version')} "
                f"channel={ref.get('channel')}>"
            )
    except Exception:  # pragma: no cover - a bad artifact must not hide the rest
        return "<unrenderable>"

    # Linear combination: keep the member ids and fitted weights visible.
    members = ref.get("member_factor_ids") or meta.get("member_factor_ids") or []
    weights = ref.get("weights")
    if members:
        if isinstance(weights, (list, tuple)) and len(weights) == len(members):
            parts = [
                f"{float(w):.3f}*{m}" for w, m in zip(weights, members)
            ]
        else:
            parts = [str(m) for m in members]
        return " + ".join(parts)
    if isinstance(ref.get("ast"), dict):
        from core.miner.expressions import FactorExpressionAST

        return FactorExpressionAST(ref["ast"]).to_display_string()
    return ""


def _record(raw: Dict[str, Any], *, db_root: Optional[str] = None) -> Dict[str, Any]:
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
        "expression": _expression_of(raw, db_root),
        "hac_t": _number(metrics.get("hac_t")),
        "p_adj": _number(metrics.get("p_adj")),
        "dsr": _number(metrics.get("dsr")),
        "empirical_p": _number(metrics.get("empirical_p")),
        "RankIC": _number(metrics.get("RankIC")),
        "RankIC_oos": _number(metrics.get("RankIC_oos")),
        "fitness_score": _number(metrics.get("fitness_score")),
        "cost_adj_return": _number(metrics.get("cost_adj_return")),
        "turnover": _number(metrics.get("ls_turnover")),
        "coverage": _number(metrics.get("coverage")),
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
                records.append(
                    _record(json.load(handle), db_root=os.path.dirname(meta_dir))
                )
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
    # Stage-A funnel accounting is written by the pipeline, not the metadata, so
    # falling back to nothing here keeps library-only roots working.
    funnel: Dict[str, Any] = {}
    for name in ("metrics.json", "state.json"):
        candidate = os.path.join(run_dir, name)
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8-sig") as handle:
                state = json.load(handle) or {}
            found = (state.get("mining") or {}).get("funnel") or state.get("funnel")
            if found:
                funnel = found
                break
        except (OSError, json.JSONDecodeError):
            continue
    return {
        "run_id": run_id,
        "run_dir": os.path.abspath(run_dir),
        "metadata_dir": os.path.abspath(meta_dir),
        "total": len(records),
        "n_passed": sum(1 for row in records if row["gate_pass"]),
        "by_status": dict(sorted(statuses.items())),
        "reject_reasons": dict(failures.most_common()),
        "funnel": funnel,
        "unreadable": unreadable,
        "factors": records,
    }


def _fmt(value: Any, width: int = 9) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:.4g}".rjust(width)
    return str(value)[:width].rjust(width)


def _print_report(
    payload: Dict[str, Any], *, show_rejects: bool, limit: int, expr_width: int = 54
) -> None:
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
    funnel = payload.get("funnel") or {}
    if funnel:
        print(
            "stage-a      : "
            f"{funnel.get('n_survivors')}/{funnel.get('n_input')} reached the "
            f"permutation null ({funnel.get('reasons') or 'none rejected'})"
        )
    rows = payload["factors"] if show_rejects else payload["factors"][:limit]
    if not rows:
        return
    print()
    print(
        f"{'factor_id':<14} {'status':<22} {'gate':>5} {'hac_t':>8} "
        f"{'p_adj':>8} {'dsr':>8} {'emp_p':>8} {'fit':>8}  expression"
    )
    for row in rows:
        expr = row.get("expression") or ""
        if expr_width > 0 and len(expr) > expr_width:
            expr = expr[: expr_width - 3] + "..."
        print(
            f"{row['factor_id']:<14} {row['lifecycle_status']:<22} "
            f"{('PASS' if row['gate_pass'] else 'FAIL'):>5} "
            f"{_fmt(row['hac_t'], 8)} {_fmt(row['p_adj'], 8)} {_fmt(row['dsr'], 8)} "
            f"{_fmt(row['empirical_p'], 8)} {_fmt(row['fitness_score'], 8)}  "
            f"{expr}"
        )
        if row["failed_checks"]:
            print(f"{'':<14} {'':<22} failed: {','.join(row['failed_checks'])}")
    if not show_rejects and payload["total"] > len(rows):
        print(f"... {payload['total'] - len(rows)} more; use --show-rejects")


def compare_runs(
    run_ids: List[str],
    *,
    user_dir: str = "user_workspace",
    output_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Side-by-side verdicts for several runs (F2).

    Answers the operational question a single-run table cannot: *what changed
    between the run I trusted last week and this one?* Returns per-run summaries
    plus the factor sets that newly pass / newly fail / disappeared, keyed by
    ``logic_hash`` so a renamed but identical expression is still recognised.
    """
    per_run: Dict[str, Any] = {}
    by_hash: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for run_id in run_ids:
        payload = collect(run_id, user_dir=user_dir, output_root=output_root)
        per_run[run_id] = {
            "total": payload["total"],
            "n_passed": payload["n_passed"],
            "by_status": payload["by_status"],
            "reject_reasons": payload["reject_reasons"],
            "funnel": payload.get("funnel"),
        }
        table: Dict[str, Dict[str, Any]] = {}
        for row in payload["factors"]:
            key = row.get("logic_hash") or row["factor_id"]
            table[str(key)] = {
                "factor_id": row["factor_id"],
                "gate_pass": row["gate_pass"],
                "fitness": row["fitness_score"],
                "expression": row.get("expression") or "",
            }
        by_hash[run_id] = table

    transitions: Dict[str, Any] = {}
    if len(run_ids) >= 2:
        base, latest = run_ids[0], run_ids[-1]
        base_h, latest_h = by_hash[base], by_hash[latest]
        transitions = {
            "baseline": base,
            "latest": latest,
            "newly_passing": sorted(
                (latest_h[k]["factor_id"] for k in latest_h
                 if latest_h[k]["gate_pass"] and not (base_h.get(k) or {}).get("gate_pass")),
            ),
            "newly_failing": sorted(
                (latest_h[k]["factor_id"] for k in latest_h
                 if not latest_h[k]["gate_pass"] and (base_h.get(k) or {}).get("gate_pass")),
            ),
            "appeared": sorted(
                (latest_h[k]["factor_id"] for k in latest_h if k not in base_h),
            ),
            "disappeared": sorted(
                (base_h[k]["factor_id"] for k in base_h if k not in latest_h),
            ),
        }
    return {"runs": list(run_ids), "per_run": per_run, "transitions": transitions}


def _print_compare(payload: Dict[str, Any]) -> None:
    print(f"{'run':<26} {'total':>6} {'passed':>7}  reject gates")
    for run_id, summary in payload["per_run"].items():
        gates = ", ".join(f"{k}={v}" for k, v in summary["reject_reasons"].items()) or "-"
        print(f"{run_id:<26} {summary['total']:>6} {summary['n_passed']:>7}  {gates}")
    tr = payload.get("transitions") or {}
    if not tr:
        return
    print()
    for label in ("newly_passing", "newly_failing", "appeared", "disappeared"):
        ids = tr.get(label) or []
        print(f"{label:<14}: {len(ids)}" + (f"  {', '.join(ids[:8])}" if ids else ""))


def run_factors_cmd(args: argparse.Namespace) -> int:
    user_dir = getattr(args, "user_dir", "user_workspace")
    output_root = getattr(args, "output_root", None)
    compare_ids = [
        x.strip() for x in str(getattr(args, "compare", "") or "").split(",") if x.strip()
    ]
    if compare_ids:
        try:
            payload = compare_runs(
                compare_ids, user_dir=user_dir, output_root=output_root
            )
        except FileNotFoundError as exc:
            print(f"factors: {exc}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        else:
            _print_compare(payload)
        return 0

    try:
        payload = collect(
            args.run_id,
            user_dir=user_dir,
            output_root=output_root,
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
            expr_width=int(getattr(args, "expr_width", 54) or 54),
        )
    if not payload["total"]:
        print("factors: no factor metadata found in this run", file=sys.stderr)
        return 1
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "factors", help="Rank a run's factors and explain gate rejections"
    )
    parser.add_argument("--run-id", default=None, help="Run id under <user-dir>/runs")
    parser.add_argument("--user-dir", default="user_workspace", help="User workspace")
    parser.add_argument("--output-root", default=None, help="Override the runs root")
    parser.add_argument("--db-root", default=None, help="Override the run's factor_db root")
    parser.add_argument("--show-rejects", action="store_true", help="List every candidate")
    parser.add_argument("--limit", type=int, default=20, help="Rows in the default table")
    parser.add_argument(
        "--expr-width", type=int, default=54,
        help="Characters of the expression column to print (0 = full)",
    )
    parser.add_argument(
        "--compare", default=None,
        help="Comma-separated run ids: compare verdicts across runs (F2)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable verdicts")
    parser.set_defaults(func=run_factors_cmd)


__all__ = ["add_parser", "collect", "run_factors_cmd"]
