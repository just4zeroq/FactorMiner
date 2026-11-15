"""Cross-run evaluation cache + append-only evaluation journal (E1/E2).

Two related problems, one mechanism.

**E2 — no cache.** The same expression is evaluated again in every run and in
every generation that re-proposes it. Expression-level dedup only stops storage,
not computation, so `ts_mean(close,20)` is re-derived from scratch each time it
appears. A cache keyed on (expression, panel, cost config) removes that.

**E1 — resume is stage-level only.** A 15-minute run that dies at generation 3
restarts from generation 1. The journal gives the same skip behaviour for free:
every evaluated candidate is appended to ``evaluations.jsonl``, and a restarted
run finds its metrics without recomputation.

Correctness rules, both of which matter more than the speed:

* the panel fingerprint must change whenever the *data* changes — a cache hit on
  a stale panel would silently replay a verdict from different data;
* the key must include the economics (cost rate, rebalance cadence, quantile),
  because those change ``cost_adj_return`` and therefore the gate verdict.

Both are enforced below; a fingerprint mismatch is a cache miss, never a silent
reuse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


def _hash(payload: Any, length: int = 16) -> str:
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.blake2b(
        text.encode("utf-8"), digest_size=length
    ).hexdigest()


def panel_fingerprint(data: Any, returns: Any) -> str:
    """Cheap, change-sensitive digest of a factor panel.

    Uses shape + index endpoints + finite-value sum/count per stream. That is
    enough to detect a data refresh (which is the only thing that must
    invalidate the cache) without hashing gigabytes.
    """
    try:
        import numpy as np
        import pandas as pd

        parts = []
        frames = []
        if isinstance(data, dict):
            for key in sorted(data, key=str):
                frames.append((str(key), data[key]))
        else:
            frames.append(("panel", data))
        for name, frame in frames:
            if isinstance(frame, (pd.DataFrame, pd.Series)):
                arr = frame.to_numpy(dtype=float, na_value=np.nan)
                finite = np.isfinite(arr)
                parts.append({
                    "name": name,
                    "shape": list(arr.shape),
                    "start": str(frame.index[0]) if len(frame.index) else "",
                    "end": str(frame.index[-1]) if len(frame.index) else "",
                    "finite": int(finite.sum()),
                    "sum": round(float(np.nansum(arr)), 6),
                })
            else:
                parts.append({"name": name, "repr": repr(frame)[:120]})
        if isinstance(returns, (pd.DataFrame, pd.Series)):
            arr = returns.to_numpy(dtype=float, na_value=np.nan)
            parts.append({
                "name": "returns",
                "shape": list(arr.shape),
                "finite": int(np.isfinite(arr).sum()),
                "sum": round(float(np.nansum(arr)), 8),
            })
        return _hash(parts)
    except Exception as exc:  # pragma: no cover - never break evaluation
        logger.debug("panel fingerprint unavailable: %s", exc)
        return "unknown"


def economics_fingerprint(config: Dict[str, Any], gate_rebalance_bars: int) -> str:
    fitness = (config or {}).get("fitness") or {}
    return _hash({
        "version": CACHE_VERSION,
        "cost": fitness.get("gate_cost_rate"),
        "quantile": fitness.get("gate_ls_quantile"),
        "rebalance": int(gate_rebalance_bars or 1),
        "horizon": ((config or {}).get("target") or {}).get("horizon_bars"),
        "entry": ((config or {}).get("target") or {}).get("entry_price"),
        "turnover_max": fitness.get("turnover_max"),
    })


def expression_fingerprint(expr: Any) -> str:
    try:
        source = expr.get_source() if callable(getattr(expr, "get_source", None)) else expr
    except Exception:
        source = repr(expr)
    return _hash(source)


class EvaluationCache:
    """File-backed metric cache with an optional append-only journal."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        root: Optional[str] = None,
        journal_path: Optional[str] = None,
        namespace: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.root = Path(root or os.path.join("user_workspace", ".eval_cache"))
        self.journal_path = journal_path
        self.namespace = namespace
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self._journal_ready = False
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - degrade to no cache
                logger.warning("Evaluation cache disabled (cannot create %s): %s", self.root, exc)
                self.enabled = False

    # ---- keys ----------------------------------------------------------- #
    def key_for(self, expr: Any, panel_fp: str, econ_fp: str) -> str:
        return _hash({
            "v": CACHE_VERSION,
            "expr": expression_fingerprint(expr),
            "panel": panel_fp,
            "econ": econ_fp,
            "ns": self.namespace,
        }, length=24)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    # ---- read/write ------------------------------------------------------ #
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            self.misses += 1
            return None
        self.hits += 1
        return metrics

    def put(self, key: str, metrics: Dict[str, Any]) -> None:
        if not self.enabled or not metrics:
            return
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(path.parent), delete=False
            ) as handle:
                json.dump({"key": key, "metrics": metrics}, handle, default=str)
                tmp = handle.name
            os.replace(tmp, path)
            self.writes += 1
        except OSError as exc:  # pragma: no cover - cache is best effort
            logger.debug("cache write skipped (%s): %s", key[:8], exc)

    # ---- journal --------------------------------------------------------- #
    def journal(self, record: Dict[str, Any]) -> None:
        """Append one evaluated candidate to ``evaluations.jsonl`` (E1)."""
        if not self.journal_path:
            return
        try:
            path = Path(self.journal_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - journal is best effort
            logger.debug("journal append skipped: %s", exc)

    def summary(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "root": str(self.root),
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "journal": self.journal_path,
        }


__all__ = [
    "EvaluationCache",
    "economics_fingerprint",
    "expression_fingerprint",
    "panel_fingerprint",
]
