"""Experiment identity and reproducibility primitives.

A *run manifest* answers the question a mature factor platform must be able to
answer: which config, which code, which operator set, which data snapshot and
which random seed produced this factor -- and how many hypotheses had already
been tried when it was evaluated.

The module is deliberately side-effect free at import time. ``build_manifest``
does the (small) I/O; the director only consumes an already-built manifest that
the pipeline injects into ``config["experiment"]["manifest"]``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def hash_json(obj: Any) -> str:
    """Stable sha256 of a JSON-serialisable object."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def git_sha() -> str:
    """Current HEAD, or ``unknown`` when the source has no .git (tarball builds)."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return out.strip()[:12]
    except Exception:
        return "unknown"


def operator_set_hash() -> str:
    """Fingerprint of the *executable* operator set (builtin + user registered).

    Borrowed from AlphaMaster's ``vocab.py`` idea: when this changes, factor
    values computed with an older operator set must be recomputed rather than
    silently reused.
    """
    try:
        from core.miner.operator_runtime import BUILTIN_OPERATORS
        from core.miner.registry import OperatorRegistry

        names = sorted({*BUILTIN_OPERATORS, *OperatorRegistry._registry})
    except Exception:
        names = []
    return hashlib.sha256("|".join(names).encode("utf-8")).hexdigest()[:12]


def _data_files(config: Dict[str, Any]) -> List[Path]:
    feeds = config.get("data_feeds", {}) or {}
    root = (
        Path("data")
        / str(feeds.get("exchange", "binance"))
        / str(feeds.get("instrument_type", "futures"))
    )
    timeframe = str(feeds.get("timeframe", "1d"))
    if not root.is_dir():
        return []
    out: List[Path] = []
    for path in sorted(root.rglob("*.feather")):
        if path.parent.name == "crypto_streams" or f"-{timeframe}-" in path.name:
            out.append(path)
    return out


def _file_fingerprint(path: Path, chunk: int = 65_536) -> Dict[str, Any]:
    h = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as fh:
        h.update(fh.read(chunk))
        if size > chunk * 2:
            fh.seek(-chunk, os.SEEK_END)
            h.update(fh.read(chunk))
    return {
        "path": path.as_posix(),
        "size": size,
        "digest": h.hexdigest()[:16],
    }


def data_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """Content-addressed snapshot of every data file this run can read."""
    files = []
    for path in _data_files(config):
        try:
            files.append(_file_fingerprint(path))
        except OSError:
            continue
    return {
        "hash": hashlib.sha256(_canonical(files).encode("utf-8")).hexdigest()[:16],
        "n_files": len(files),
        "files": files[:8],
    }


def seed_everything(seed: Optional[int]) -> Optional[int]:
    """Fix the global RNGs used by GP / RL. Returns None when not requested.

    ``numpy``'s legacy global RNG is seeded too because RL samples with
    ``np.random.choice``. NN miners own a ``default_rng(seed)`` already.
    """
    if seed is None:
        return None
    import numpy as np

    value = int(seed)
    random.seed(value)
    np.random.seed(value % (2**32))
    return value


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{os.urandom(3).hex()}"


def build_manifest(
    config: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    config_path: Optional[str] = None,
    n_trials_at_start: int = 0,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the reproducible identity of one mining run."""
    feeds = config.get("data_feeds", {}) or {}
    exp = config.get("experiment", {}) or {}
    seed = exp.get("seed")
    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_hash": hash_json(config),
        "config_path": config_path,
        "git_sha": git_sha(),
        "operator_set_hash": operator_set_hash(),
        "data_snapshot": data_snapshot(config),
        "seed": seed,
        "seed_applied": bool(seed is not None),
        "paradigm": config.get("paradigm"),
        "timeframe": feeds.get("timeframe"),
        "universe": {
            "exchange": feeds.get("exchange"),
            "instrument_type": feeds.get("instrument_type"),
            "pairs": list(feeds.get("pairs", []) or []),
        },
        "windows": {
            "mine": feeds.get("mine_period", []),
            "test": feeds.get("test_period", []),
        },
        "n_trials_at_start": int(n_trials_at_start),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "notes": {
            "llm_nondeterministic": config.get("paradigm") == "LLM",
        },
    }
    if extra:
        manifest.update(extra)
    return manifest


def manifest_summary(manifest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact digest stamped into each factor's ``data_lineage``."""
    if not manifest:
        return {}
    return {
        "run_id": manifest.get("run_id"),
        "config_hash": manifest.get("config_hash"),
        "git_sha": manifest.get("git_sha"),
        "operator_set_hash": manifest.get("operator_set_hash"),
        "data_snapshot_hash": (manifest.get("data_snapshot") or {}).get("hash"),
        "seed": manifest.get("seed"),
    }


def verify_manifest(manifest: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Compare a stored manifest against the current code/data/config.

    Used by ``--resume`` and by the reaudit path: a changed operator set or
    data snapshot means cached factor values must not be reused.
    """
    checks = {
        "config_hash": hash_json(config),
        "git_sha": git_sha(),
        "operator_set_hash": operator_set_hash(),
        "data_snapshot_hash": data_snapshot(config)["hash"],
    }
    observed = {
        "config_hash": manifest.get("config_hash"),
        "git_sha": manifest.get("git_sha"),
        "operator_set_hash": manifest.get("operator_set_hash"),
        "data_snapshot_hash": (manifest.get("data_snapshot") or {}).get("hash"),
    }
    mismatches = {k: {"stored": observed[k], "current": checks[k]}
                  for k in checks if observed.get(k) and observed[k] != checks[k]}
    return {"reproducible": not mismatches, "mismatches": mismatches, "current": checks}
