"""Portable hypothesis-budget snapshot (fixes the cross-machine denominator).

The append-only FDR / experiment ledgers are deliberately gitignored (high
volume). Consequence: a fresh clone starts from ``num_trials = 0`` and DSR /
BH-FDR silently under-correct. This module keeps a *committable* compact
snapshot of the cumulative trial count and the raw p-value family, and folds it
into every new run as a baseline.

Semantics: monotonic. ``sync`` never lowers a stored count, so a stale local
ledger cannot reset the global budget.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_PATH = Path("user_workspace") / ".hypothesis_budget.json"
ENV_VAR = "FACTORMINER_BUDGET_PATH"
MAX_PVALUES = 10_000


def _path(path: Optional[Path] = None) -> Path:
    """Explicit arg > env override (used by the test suite) > project default."""
    if path is not None:
        return Path(path)
    env = os.environ.get(ENV_VAR)
    return Path(env) if env else DEFAULT_PATH


class BudgetSnapshotError(RuntimeError):
    """The committed hypothesis budget is unreadable and must not be reset."""


def _empty() -> Dict[str, Any]:
    return {"n_trials": 0, "p_values": [], "runs": 0, "updated_at": None}


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = _path(path)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "n_trials": int(data.get("n_trials", 0) or 0),
            "p_values": [float(x) for x in data.get("p_values", [])],
            "runs": int(data.get("runs", 0) or 0),
            "updated_at": data.get("updated_at"),
        }
    except (OSError, ValueError, TypeError) as exc:
        # X-05: silently returning an empty snapshot let ``sync`` rebuild the
        # budget from zero, violating the module's own monotonicity promise.
        out = _empty()
        out["corrupt"] = True
        out["error"] = str(exc)
        return out


def sync(
    n_trials: int,
    p_values: Sequence[float],
    *,
    path: Optional[Path] = None,
    increment_runs: bool = False,
) -> Dict[str, Any]:
    """Write the snapshot, keeping the maximum of stored and observed values."""
    p = _path(path)
    prior = load(p)
    if prior.get("corrupt"):
        raise BudgetSnapshotError(
            f"Hypothesis budget snapshot {p} is corrupt ({prior.get('error')}); "
            "refusing to overwrite it with a lower trial count. Repair or "
            "delete the file explicitly."
        )
    family = list(prior["p_values"])
    family.extend(float(x) for x in p_values if x is not None)
    # Keep the newest values when the family grows beyond the cap.
    family = [float(x) for x in family[-MAX_PVALUES:]]
    payload = {
        "n_trials": max(int(prior["n_trials"]), int(n_trials)),
        "p_values": family,
        "runs": int(prior["runs"]) + (1 if increment_runs else 0),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return payload


def resolve(
    trace_num_trials: int,
    fdr_cumulative: Sequence[float],
    run_p_values: Sequence[float],
    *,
    path: Optional[Path] = None,
) -> Tuple[int, List[float], Dict[str, Any]]:
    """Return (n_trials_denominator, BH family, snapshot) for a new run.

    ``trace_num_trials`` already includes the local cross-run trace but NOT the
    candidates about to be evaluated this run; callers add those themselves.
    """
    snap = load(path)
    base = max(int(trace_num_trials), int(snap["n_trials"]))
    # A4 / X-05: the old ``local_family if local_family else snapshot`` used
    # ``or`` and DISCARDED the entire historical snapshot whenever the local
    # ledger was non-empty (394 local p-values hid 310 historical ones). The
    # BH family must be the union of all three sources, while the DSR
    # denominator ``n_trials`` uses max() in the same function.
    family = (
        list(fdr_cumulative)
        + list(snap["p_values"])
        + list(run_p_values)
    )
    clean: List[float] = []
    for x in family:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            clean.append(v)
    return base, clean, snap


def status(path: Optional[Path] = None) -> Dict[str, Any]:
    snap = load(path)
    return {
        "snapshot_path": str(_path(path)),
        "n_trials": snap["n_trials"],
        "n_p_values": len(snap["p_values"]),
        "runs": snap["runs"],
        "updated_at": snap["updated_at"],
    }
