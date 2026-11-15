"""Seed a search from factors that already passed the gates (F5).

Every run used to start from scratch: the only thing carried across runs was a
set of logic hashes used for *deduplication*, so a thousand-run research
programme accumulated verdicts but never accumulated *knowledge*. The result was
"run it 100 times and it never gets stronger", which is exactly what the
operational review observed.

This module reads the promoted library and returns its strongest expressions as
ASTs the paradigm can inject into its initial population.

Why this is not a holdout leak: the promoted factors were discovered on the
in-sample window, their verdicts are already recorded, and re-using them as
seeds spends no new information about the validation window. It is the same
information the researcher already has on their desk.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _score(meta: Dict[str, Any]) -> float:
    metrics = meta.get("metrics") or {}
    if not isinstance(metrics, dict):
        return -1e18
    for key in ("fitness_score", "RankIC"):
        try:
            value = metrics.get(key)
            if value is None:
                continue
            out = float(value)
            if out == out:  # not NaN
                return out
        except (TypeError, ValueError):
            continue
    return -1e18


def load_seed_asts(
    db_root: str,
    top_k: int = 20,
    *,
    promoted_only: bool = True,
) -> List[Dict[str, Any]]:
    """Return up to ``top_k`` stored ASTs, best first.

    ``promoted_only`` keeps only factors whose gate verdict is green and whose
    lifecycle is ``INSPECTED``; a run that wants raw material can set it false.
    Anything that is not a GP AST (LLM source, NN channel, RL actions) is
    skipped — those paradigms cannot be re-expressed as an AST.
    """
    meta_dir = Path(db_root) / "metadata"
    if not meta_dir.is_dir():
        return []
    candidates: List[tuple] = []
    for path in meta_dir.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        metrics = meta.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        if promoted_only:
            if not bool(metrics.get("gate_pass", False)):
                continue
            if str(meta.get("lifecycle_status", "")).upper() != "INSPECTED":
                continue
        ref = meta.get("logic_reference") or {}
        if not isinstance(ref, dict) or ref.get("type") != "json_ast":
            continue
        ast = ref.get("ast")
        if not isinstance(ast, dict) or "op" not in ast:
            continue
        candidates.append((_score(meta), str(meta.get("factor_id") or path.stem), ast))
    candidates.sort(key=lambda item: item[0], reverse=True)
    limit = max(0, int(top_k or 0))
    picked = candidates[:limit] if limit else []
    if picked:
        logger.info(
            "GP: seeding from %d promoted factor(s) in %s (top score %.4f)",
            len(picked), db_root, picked[0][0],
        )
    return [ast for _, _, ast in picked]


__all__ = ["load_seed_asts"]
