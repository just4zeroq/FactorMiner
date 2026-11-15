"""``factorminer promote`` — move one gated factor from a run into production.

An experiment deliberately writes to ``<run>/factor_db`` so a bad run cannot
contaminate the shared library. That also means a passing factor stays invisible
to ``backtest --db-root factor_db`` until someone copies it. Doing that copy by
hand loses lineage, silently overwrites an existing factor, and leaves no audit
trail. This command copies the metadata, value snapshot and every referenced
artifact as one unit, refuses to overwrite, and appends a promotion record.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.commands.factors import run_dir_for

_FACTOR_ID = re.compile(r"^[A-Za-z0-9_.\-]+$")
_AUDIT_NAME = "reports/promotions.jsonl"
_SUBDIRS = ("sources", "weights", "models")


def _load_meta(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Metadata is not an object: {path}")
    return data


def _referenced_names(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _referenced_names(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _referenced_names(item)
    elif isinstance(value, str) and "." in value and "/" not in value and "\\" not in value:
        yield value


def _source_files(src_db: Path, factor_id: str, meta: Dict[str, Any]) -> List[str]:
    """Relative paths that travel with the factor, including NN/LLM artifacts."""
    found = {f"metadata/{factor_id}.json", f"values/{factor_id}.parquet"}
    referenced = set(_referenced_names(meta.get("logic_reference") or {}))
    for sub in _SUBDIRS:
        folder = src_db / sub
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_file():
                continue
            if path.name.startswith(factor_id) or path.name in referenced:
                found.add(f"{sub}/{path.name}")
    missing = [rel for rel in sorted(found) if not (src_db / rel).is_file()]
    required_missing = [rel for rel in missing if rel.startswith(("metadata/", "values/"))]
    if required_missing:
        raise FileNotFoundError(
            f"Missing required factor files in {src_db}: {', '.join(required_missing)}"
        )
    return [rel for rel in sorted(found) if rel not in missing]


def _append_audit(user_dir: str, record: Dict[str, Any]) -> str:
    path = Path(user_dir) / _AUDIT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return str(path)


def plan_promotion(
    run_id: str,
    factor_id: str,
    *,
    db_root: str = "factor_db",
    user_dir: str = "user_workspace",
    output_root: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Validate, stage and describe a promotion. Performs no writes."""
    if not _FACTOR_ID.match(factor_id):
        raise ValueError(f"Unsafe factor id {factor_id!r}: use letters, digits, _, . or -")
    src_db = Path(
        run_dir_for(run_id, user_dir=user_dir, output_root=output_root)
    ) / "factor_db"
    dst_db = Path(db_root)
    src_meta = src_db / "metadata" / f"{factor_id}.json"
    if not src_meta.is_file():
        raise FileNotFoundError(
            f"Factor {factor_id} not found at {src_meta}. Check --run-id / --factor-id."
        )
    meta = _load_meta(src_meta)
    metrics = meta.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    checks = {
        "gate_pass": bool(metrics.get("gate_pass", False)),
        "inspected": str(meta.get("lifecycle_status", "")).upper() == "INSPECTED",
        "no_existing_target": not (dst_db / "metadata" / f"{factor_id}.json").exists(),
    }
    if not all(checks.values()) and not force:
        failed = [k for k, ok in checks.items() if not ok]
        raise ValueError(
            f"Refusing to promote {factor_id}: {', '.join(failed)}. "
            "Pass --force only when you have independently verified the override."
        )

    files = _source_files(src_db, factor_id, meta)
    staged = {rel: (src_db / rel).read_bytes() for rel in files}

    payload = dict(meta)
    lineage = dict(payload.get("data_lineage") or {})
    lineage["promotion"] = {
        "run_id": run_id,
        "source_db": str(src_db),
        "promoted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "gate_checks": metrics.get("gate_checks") or {},
        "forced": bool(force),
    }
    payload["data_lineage"] = lineage
    staged["metadata/" + factor_id + ".json"] = json.dumps(
        payload, ensure_ascii=False, indent=2
    ).encode("utf-8")
    return {
        "run_id": run_id,
        "factor_id": factor_id,
        "source_db": str(src_db),
        "target_db": str(dst_db),
        "files": sorted(staged),
        "staged": staged,
        "checks": checks,
        "forced": bool(force),
        "gate": {
            "gate_pass": checks["gate_pass"],
            "dsr": metrics.get("dsr"),
            "hac_t": metrics.get("hac_t"),
            "empirical_p": metrics.get("empirical_p"),
        },
    }


def select_inspected(
    run_id: str,
    *,
    user_dir: str = "user_workspace",
    output_root: Optional[str] = None,
) -> List[str]:
    """Factor ids in a run that are both INSPECTED and gate_pass (P0-2)."""
    src_db = Path(
        run_dir_for(run_id, user_dir=user_dir, output_root=output_root)
    ) / "factor_db"
    meta_dir = src_db / "metadata"
    if not meta_dir.is_dir():
        raise FileNotFoundError(
            f"No factor metadata for run {run_id} at {meta_dir}. "
            "Check --run-id / --user-dir."
        )
    selected: List[str] = []
    for path in sorted(meta_dir.glob("*.json")):
        try:
            meta = _load_meta(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        metrics = meta.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        if (
            bool(metrics.get("gate_pass", False))
            and str(meta.get("lifecycle_status", "")).upper() == "INSPECTED"
        ):
            selected.append(str(meta.get("factor_id") or path.stem))
    return selected


def _commit(plan: Dict[str, Any], user_dir: str) -> Dict[str, Any]:
    """Write a staged plan to disk. Raises on conflict, leaving no partial copy."""
    dst_db = Path(plan["target_db"])
    written: List[str] = []
    try:
        for rel, content in plan["staged"].items():
            target = dst_db / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # Refuse to overwrite: another run may have published the same id
            # while this command was being prepared.
            with target.open("xb") as handle:
                handle.write(content)
            written.append(rel)
    except FileExistsError as exc:
        for rel in written:
            (dst_db / rel).unlink(missing_ok=True)
        raise ValueError(
            f"Factor {plan['factor_id']} appeared in {dst_db} during promotion; "
            "nothing was changed. Inspect before retrying."
        ) from exc
    audit = _append_audit(
        user_dir or "user_workspace",
        {
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "event": "promote",
            "run_id": plan["run_id"],
            "factor_id": plan["factor_id"],
            "source_db": plan["source_db"],
            "target_db": plan["target_db"],
            "files": written,
            "gate": plan["gate"],
            "forced": plan["forced"],
        },
    )
    return {
        "dry_run": False,
        "run_id": plan["run_id"],
        "factor_id": plan["factor_id"],
        "target_db": plan["target_db"],
        "files": written,
        "audit": audit,
    }


def run_promote_cmd(args: argparse.Namespace) -> int:
    factor_ids: List[str] = []
    if getattr(args, "all_inspected", False):
        try:
            factor_ids = select_inspected(
                args.run_id,
                user_dir=getattr(args, "user_dir", "user_workspace"),
                output_root=getattr(args, "output_root", None),
            )
        except FileNotFoundError as exc:
            print(f"promote: {exc}", file=sys.stderr)
            return 1
        if not factor_ids:
            print(
                f"promote: run {args.run_id} has no INSPECTED + gate_pass factor "
                "to publish. Inspect `factors --run-id` for the reject reasons.",
                file=sys.stderr,
            )
            return 1
    else:
        if not getattr(args, "factor_id", None):
            print("promote: --factor-id or --all-inspected is required", file=sys.stderr)
            return 2
        factor_ids = [args.factor_id]

    dry_run = bool(getattr(args, "dry_run", False))
    as_json = bool(getattr(args, "json", False))
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    for factor_id in factor_ids:
        try:
            plan = plan_promotion(
                args.run_id,
                factor_id,
                db_root=getattr(args, "db_root", "factor_db"),
                user_dir=getattr(args, "user_dir", "user_workspace"),
                output_root=getattr(args, "output_root", None),
                force=bool(getattr(args, "force", False)),
            )
            if dry_run:
                result = {
                    "dry_run": True,
                    "run_id": plan["run_id"],
                    "factor_id": plan["factor_id"],
                    "source_db": plan["source_db"],
                    "target_db": plan["target_db"],
                    "files": plan["files"],
                    "checks": plan["checks"],
                    "gate": plan["gate"],
                }
            else:
                result = _commit(
                    plan, getattr(args, "user_dir", "user_workspace")
                )
        except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
            # One bad factor must not abort the rest of the batch.
            failures.append({"factor_id": factor_id, "error": str(exc)})
            continue
        results.append(result)

    payload = {
        "run_id": args.run_id,
        "requested": len(factor_ids),
        "promoted": results,
        "failed": failures,
        "n_promoted": len(results),
        "n_failed": len(failures),
        "dry_run": dry_run,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        action = "would promote" if dry_run else "promoted"
        for result in results:
            print(
                f"{action} {result['factor_id']} from run {result['run_id']} "
                f"-> {result['target_db']}"
            )
            if not dry_run:
                print(f"  files: {len(result['files'])}  audit: {result['audit']}")
        for failure in failures:
            print(
                f"FAILED {failure['factor_id']}: {failure['error']}", file=sys.stderr
            )
        print(
            f"{len(results)}/{len(factor_ids)} factor(s) "
            f"{'validated' if dry_run else 'published'}, {len(failures)} failed"
        )
    if failures and not results:
        return 1
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "promote", help="Publish gated factors from a run into factor_db"
    )
    parser.add_argument("--run-id", required=True, help="Run holding the factor")
    parser.add_argument(
        "--factor-id", default=None,
        help="Factor to publish (omit when using --all-inspected)",
    )
    parser.add_argument(
        "--all-inspected", action="store_true",
        help="Publish every INSPECTED + gate_pass factor in the run (batch)",
    )
    parser.add_argument("--db-root", default="factor_db", help="Production factor_db root")
    parser.add_argument("--user-dir", default="user_workspace", help="User workspace")
    parser.add_argument("--output-root", default=None, help="Override the runs root")
    parser.add_argument("--dry-run", action="store_true", help="Validate and list files only")
    parser.add_argument("--force", action="store_true", help="Override failed gate checks (audited)")
    parser.add_argument("--json", action="store_true", help="Machine-readable result")
    parser.set_defaults(func=run_promote_cmd)


__all__ = ["add_parser", "plan_promotion", "run_promote_cmd"]
