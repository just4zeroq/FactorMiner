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
    return {"n_trials": 0, "p_values": [], "runs": 0, "updated_at": None, "families": {}}


def _family_block(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    try:
        n = int(data.get("n_trials", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    pvals: List[float] = []
    for x in data.get("p_values", []) or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            pvals.append(v)
    return {
        "n_trials": n,
        "p_values": pvals[-MAX_PVALUES:],
        "runs": int(data.get("runs", 0) or 0),
        "updated_at": data.get("updated_at"),
    }


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = _path(path)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        families_raw = data.get("families") or {}
        families = {
            str(name): _family_block({**block, "updated_at": block.get("updated_at")})
            for name, block in families_raw.items()
            if isinstance(block, dict)
        } if isinstance(families_raw, dict) else {}
        return {
            "n_trials": int(data.get("n_trials", 0) or 0),
            "p_values": [float(x) for x in data.get("p_values", [])],
            "runs": int(data.get("runs", 0) or 0),
            "updated_at": data.get("updated_at"),
            "families": families,
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
    family: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the snapshot, keeping the maximum of stored and observed values.

    ``family`` partitions the hypothesis budget (L1.3). A *family* is declared
    before any data is looked at (``experiment.trial_family``) and is the unit
    multiple-testing correction should be applied to: searching five universes
    is five research programmes, not five times the depth of one. Without a
    family the legacy global denominator is used unchanged, so existing runs
    and existing snapshots keep their meaning.
    """
    p = _path(path)
    prior = load(p)
    if prior.get("corrupt"):
        raise BudgetSnapshotError(
            f"Hypothesis budget snapshot {p} is corrupt ({prior.get('error')}); "
            "refusing to overwrite it with a lower trial count. Repair or "
            "delete the file explicitly."
        )
    family_values = list(prior["p_values"])
    family_values.extend(float(x) for x in p_values if x is not None)
    # Keep the newest values when the family grows beyond the cap.
    family_values = [float(x) for x in family_values[-MAX_PVALUES:]]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "n_trials": max(int(prior["n_trials"]), int(n_trials)),
        "p_values": family_values,
        "runs": int(prior["runs"]) + (1 if increment_runs else 0),
        "updated_at": now,
        "families": dict(prior.get("families") or {}),
    }
    if family:
        block = _family_block(payload["families"].get(family))
        merged = block["p_values"] + [float(x) for x in p_values if x is not None]
        payload["families"][family] = {
            "n_trials": max(int(block["n_trials"]), int(n_trials)),
            "p_values": [float(x) for x in merged[-MAX_PVALUES:]],
            "runs": int(block["runs"]) + (1 if increment_runs else 0),
            "updated_at": now,
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
    family: Optional[str] = None,
) -> Tuple[int, List[float], Dict[str, Any]]:
    """Return (n_trials_denominator, BH family, snapshot) for a new run.

    ``trace_num_trials`` already includes the local cross-run trace but NOT the
    candidates about to be evaluated this run; callers add those themselves.

    When ``family`` is given, the denominator and the BH family come from that
    partition only. ``snapshot["global_n_trials"]`` still carries the
    all-families total so reports can show the full research effort.
    """
    snap = load(path)
    families = snap.get("families") or {}
    # A declared-but-never-synced family has no block yet; falling back to the
    # global denominator is correct (nothing was recorded under that name), but
    # an unknown name must not read as "zero trials so far".
    scope = _family_block(families[family]) if family and family in families else None
    if scope is not None:
        base = max(int(trace_num_trials), int(scope["n_trials"]))
        # A4 / X-05: the old ``local_family if local_family else snapshot`` used
        # ``or`` and DISCARDED the entire historical snapshot whenever the local
        # ledger was non-empty (394 local p-values hid 310 historical ones). The
        # BH family must be the union of all three sources, while the DSR
        # denominator ``n_trials`` uses max() in the same function.
        pooled = (
            list(fdr_cumulative)
            + list(scope["p_values"])
            + list(run_p_values)
        )
    else:
        base = max(int(trace_num_trials), int(snap["n_trials"]))
        pooled = (
            list(fdr_cumulative)
            + list(snap["p_values"])
            + list(run_p_values)
        )
    clean: List[float] = []
    for x in pooled:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            clean.append(v)
    out = dict(snap)
    out["family"] = family
    out["scope_n_trials"] = int(base)
    out["global_n_trials"] = int(snap["n_trials"])
    return base, clean, out


def status(path: Optional[Path] = None) -> Dict[str, Any]:
    snap = load(path)
    return {
        "snapshot_path": str(_path(path)),
        "n_trials": snap["n_trials"],
        "n_p_values": len(snap["p_values"]),
        "runs": snap["runs"],
        "updated_at": snap["updated_at"],
        "families": {
            name: {
                "n_trials": block["n_trials"],
                "n_p_values": len(block["p_values"]),
                "runs": block["runs"],
                "updated_at": block["updated_at"],
            }
            for name, block in (snap.get("families") or {}).items()
        },
    }
