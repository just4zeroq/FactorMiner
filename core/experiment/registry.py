"""Durable run registry -- the missing link between tasks.json and factor_db.

``tasks.json`` records *task* state; ``factor_db`` records *factors*. Neither
answers "which run produced this factor, with which config". This registry owns
one directory per run (``user_workspace/runs/<run_id>/``) plus an append-only
index, and is the unit that ``--resume`` operates on.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RUNS_DIR = Path("user_workspace") / "runs"
INDEX_NAME = "index.jsonl"

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
STAGE_ORDER = (
    "created",
    "data_checked",
    "mining",
    "gated",
    "validated",
    "combined",
    "backtested",
    "completed",
)

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def create_run(
    manifest: Dict[str, Any],
    config: Dict[str, Any],
    *,
    config_path: Optional[str] = None,
    root: Optional[Path] = None,
) -> Path:
    """Create the run directory, write manifest/config/status, append index."""
    base = Path(root) if root else RUNS_DIR
    rid = manifest["run_id"]
    path = base / rid
    path.mkdir(parents=True, exist_ok=True)
    _atomic_json(path / "manifest.json", manifest)
    _atomic_json(path / "config.json", config)
    status = {
        "run_id": rid,
        "status": "created",
        "stage": "created",
        "created_at": _now(),
        "updated_at": _now(),
        "config_hash": manifest.get("config_hash"),
        "config_path": config_path,
        "completed_stages": ["created"],
        "artifacts": {},
    }
    _atomic_json(path / "status.json", status)
    with _lock:
        with (base / INDEX_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run_id": rid,
                "created_at": status["created_at"],
                "config_hash": status["config_hash"],
                "paradigm": manifest.get("paradigm"),
                "status": "created",
            }, ensure_ascii=False) + "\n")
    return path


def read_status(run_id: str, *, root: Optional[Path] = None) -> Dict[str, Any]:
    base = Path(root) if root else RUNS_DIR
    path = base / run_id / "status.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown run: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def update_status(
    run_id: str,
    stage: str,
    *,
    status: Optional[str] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Advance the stage ledger. Idempotent: re-reporting a stage is a no-op."""
    with _lock:
        current = read_status(run_id, root=root)
        completed = list(current.get("completed_stages", []))
        if stage not in completed:
            completed.append(stage)
        current.update({
            "status": status or ("completed" if stage == "completed" else "running"),
            "stage": stage,
            "updated_at": _now(),
            "completed_stages": completed,
        })
        if artifacts:
            current.setdefault("artifacts", {}).update(artifacts)
        if error:
            current["error"] = error
        base = Path(root) if root else RUNS_DIR
        _atomic_json(base / run_id / "status.json", current)
        return current


def list_runs(*, root: Optional[Path] = None, limit: int = 20) -> List[Dict[str, Any]]:
    base = Path(root) if root else RUNS_DIR
    idx = base / INDEX_NAME
    if not idx.exists():
        return []
    rows = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))[:limit]


def find_resumable(config_hash: str, *, root: Optional[Path] = None) -> Optional[str]:
    """Most recent non-terminal run with the same config, if any."""
    for row in list_runs(root=root, limit=200):
        if row.get("config_hash") != config_hash:
            continue
        try:
            state = read_status(row["run_id"], root=root)
        except (FileNotFoundError, KeyError):
            continue
        if state.get("status") not in TERMINAL_STATUSES:
            return row["run_id"]
    return None


def write_artifact(
    run_id: str,
    name: str,
    payload: Any,
    *,
    root: Optional[Path] = None,
) -> Path:
    """Write a JSON/text artifact into the run directory and register it."""
    base = Path(root) if root else RUNS_DIR
    path = base / run_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        _atomic_json(path, payload)
    update_status(run_id, read_status(run_id, root=root).get("stage", "created"),
                  artifacts={name: str(path)}, root=root)
    return path
