"""Cross-sectional (universe) holdout for factor experiments.

Why this exists
---------------
The one-time *time* holdout (2025-07-01 .. 2026-11-13) was revealed during P0
and cannot be reused. Waiting for fresh calendar bars is legitimate but slow,
and it leaves the platform with no way to verify generalisation today. A second,
orthogonal axis of clean data is the **asset dimension**: mine on one set of
symbols, evaluate on a set that no evaluation has ever read.

Two supported modes
-------------------
* ``data_feeds.universe_holdout`` (explicit list of pairs). If the listed pairs
  are a subset of ``data_feeds.pairs``, the universe is *split*: train =
  remainder, validation = listed. If they are disjoint from ``pairs``, they are
  treated as **fresh symbols**: train = ``pairs``, validation = listed. That
  second case is the genuinely clean one; the first is a mechanism check only.
* ``data_feeds.universe_holdout_frac`` (0..1) deterministically reserves a
  fraction of ``pairs`` using ``universe_holdout_seed`` (defaults to the run
  seed). Always a split, never "fresh".

A split over symbols that were part of earlier mining universes is flagged
``clean = False``. This is deliberately loud: a leave-K-out split of the same
20 assets proves the plumbing works, but it is NOT independent evidence,
because those assets' histories were already read. Only disjoint new symbols
give a real universe holdout.

Discipline
----------
Every used split is fingerprinted into ``user_workspace/.universe_ledger.json``
and can be revealed only once, mirroring ``HoldoutLedger``. ``strict`` (default)
raises on reuse; ``audit`` warns; ``off`` disables. ``require_fresh`` (default
false) additionally rejects a ``clean = False`` split outright, so a team can
make "no pseudo-holdouts" a hard rule once fresh data is downloaded.

This module never downloads data. Use ``scripts/download_universe_holdout.py``
to fetch the history of the reserved symbols first.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.experiment.manifest import hash_json

logger = logging.getLogger(__name__)

LEDGER_NAME = ".universe_ledger.json"
ENV_VAR = "FACTORMINER_UNIVERSE_LEDGER_PATH"
VALID_MODES = ("strict", "audit", "off")


class UniverseReuseError(RuntimeError):
    """A universe holdout split was already revealed."""


class UniverseNotCleanError(RuntimeError):
    """A split over previously-read assets was used where fresh assets are required."""


def default_ledger_path() -> str:
    env = os.environ.get(ENV_VAR)
    if env:
        return env
    root = Path(__file__).resolve().parents[2]
    return str(root / "user_workspace" / LEDGER_NAME)


def _read_ledger(path: Optional[str] = None) -> List[Dict[str, Any]]:
    p = Path(path or default_ledger_path())
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        logger.warning("Cannot read universe ledger %s: %s", p, exc)
        return []


def _append_ledger(record: Dict[str, Any], path: Optional[str] = None) -> None:
    p = Path(path or default_ledger_path())
    entries = _read_ledger(str(p))
    entries.append(record)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class UniverseSplit:
    enabled: bool
    train_pairs: List[str]
    holdout_pairs: List[str]
    split_hash: str = ""
    source: str = "none"          # "explicit" | "fraction" | "none"
    clean: bool = False           # disjoint fresh symbols => True
    mode: str = "strict"
    require_fresh: bool = False
    note: str = ""

    def summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "source": self.source,
            "clean": self.clean,
            "mode": self.mode,
            "require_fresh": self.require_fresh,
            "split_hash": self.split_hash,
            "n_train": len(self.train_pairs),
            "n_holdout": len(self.holdout_pairs),
            "train_pairs": list(self.train_pairs),
            "holdout_pairs": list(self.holdout_pairs),
            "note": self.note,
        }


def resolve_mode(config: Optional[Dict[str, Any]] = None,
                 explicit: Optional[str] = None) -> str:
    feeds = (config or {}).get("data_feeds") or {}
    for candidate in (
        explicit,
        feeds.get("universe_holdout_mode"),
        os.environ.get("FACTORMINER_UNIVERSE_MODE"),
    ):
        if candidate and str(candidate).lower() in VALID_MODES:
            return str(candidate).lower()
    return "strict"


def resolve_split(config: Dict[str, Any]) -> UniverseSplit:
    """Derive the train/validation universes from config. Pure, no side effects."""
    feeds = config.get("data_feeds") or {}
    all_pairs = [str(p) for p in (feeds.get("pairs") or [])]
    explicit = feeds.get("universe_holdout")
    frac = feeds.get("universe_holdout_frac")
    if not all_pairs and not (explicit or frac):
        # Configs used by unit tests / single-asset runs need no split.
        return UniverseSplit(
            enabled=False,
            train_pairs=[],
            holdout_pairs=[],
            source="none",
            clean=False,
            mode=resolve_mode(config),
            note="no universe configured",
        )
    if not all_pairs:
        raise ValueError("data_feeds.pairs is empty; cannot resolve a universe split")
    if explicit:
        holdout = sorted({str(p) for p in explicit})
        source = "explicit"
    elif frac:
        ratio = float(frac)
        if not 0.0 < ratio < 1.0:
            raise ValueError("universe_holdout_frac must be in (0, 1)")
        k = max(1, min(len(all_pairs) - 1, int(round(len(all_pairs) * ratio))))
        seed = int(
            feeds.get("universe_holdout_seed")
            or (config.get("experiment") or {}).get("seed")
            or 0
        )
        shuffled = list(all_pairs)
        random.Random(seed).shuffle(shuffled)
        holdout = sorted(shuffled[:k])
        source = "fraction"
    else:
        return UniverseSplit(
            enabled=False,
            train_pairs=list(all_pairs),
            holdout_pairs=[],
            source="none",
            clean=False,
            mode=resolve_mode(config),
            note="universe holdout not configured",
        )

    if not holdout:
        raise ValueError("universe_holdout resolved to an empty set")

    overlap = set(holdout) & set(all_pairs)
    if overlap and len(overlap) == len(set(holdout)):
        # Case (a): split the existing universe.
        train = [p for p in all_pairs if p not in overlap]
        clean = False
        note = ("leave-K-out split of the existing universe — mechanism check "
                "only, these assets' histories were already read")
    elif not overlap:
        # Case (b): genuinely new symbols, never part of any mining universe.
        train = list(all_pairs)
        clean = True
        note = "disjoint fresh symbols reserved for validation"
    else:
        raise ValueError(
            "universe_holdout mixes existing pairs with new symbols; declare "
            "either a pure split or a purely disjoint fresh set"
        )

    if len(train) < 3 or len(holdout) < 3:
        raise ValueError(
            f"both universes need >= 3 assets for a cross-sectional book; "
            f"got train={len(train)}, holdout={len(holdout)}"
        )

    return UniverseSplit(
        enabled=True,
        train_pairs=train,
        holdout_pairs=holdout,
        split_hash=hash_json({"train": sorted(train), "holdout": sorted(holdout)}),
        source=source,
        clean=clean,
        mode=resolve_mode(config),
        require_fresh=bool(feeds.get("universe_holdout_require_fresh", False)),
        note=note,
    )


def train_config(config: Dict[str, Any],
                 split: Optional[UniverseSplit] = None) -> Dict[str, Any]:
    """Config for every stage that may only see the training universe."""
    sp = split or resolve_split(config)
    out = copy.deepcopy(config)
    out.setdefault("data_feeds", {})["pairs"] = list(sp.train_pairs)
    out["data_feeds"]["universe_role"] = "train"
    return out


def holdout_config(config: Dict[str, Any],
                   split: Optional[UniverseSplit] = None) -> Dict[str, Any]:
    """Config for OOS recompute / backtest: only the reserved symbols."""
    sp = split or resolve_split(config)
    if not sp.enabled:
        return copy.deepcopy(config)
    out = copy.deepcopy(config)
    out["data_feeds"]["pairs"] = list(sp.holdout_pairs)
    out["data_feeds"]["universe_role"] = "holdout"
    return out


def guard(split: UniverseSplit, *, source: str = "",
          allow_reuse: bool = False,
          ledger_path: Optional[str] = None,
          mode: Optional[str] = None,
          record: bool = True) -> Dict[str, Any]:
    """Enforce one-time use of a universe holdout split."""
    resolved = mode or split.mode
    if resolved not in VALID_MODES:
        resolved = "strict"
    result: Dict[str, Any] = {
        "allowed": True,
        "mode": resolved,
        "source": source,
        "clean": split.clean,
        "split_hash": split.split_hash,
        "reused": False,
        "recorded": False,
        "reason": "clean",
        "ledger_path": ledger_path or default_ledger_path(),
    }
    if not split.enabled:
        result["reason"] = "not_configured"
        return result
    if resolved == "off":
        result["reason"] = "mode_off"
        return result

    entries = _read_ledger(result["ledger_path"])
    prior = next((e for e in entries
                  if e.get("split_hash") == split.split_hash), None)
    if prior:
        result["reused"] = True
        if not allow_reuse and resolved == "strict":
            result["allowed"] = False
            result["reason"] = "burned"
            raise UniverseReuseError(
                f"{source or 'This run'}: universe split {split.split_hash} was "
                f"already revealed at {prior.get('at')}. Re-reading the same "
                f"reserved assets turns the validation in-sample. Declare a new "
                f"disjoint symbol set (and download its history) or pass "
                f"allow_reuse=True only to recompute a published number."
            )
        result["reason"] = "allow_reuse" if allow_reuse else "audit_allowed"

    if split.require_fresh and not split.clean:
        if resolved == "strict":
            result["allowed"] = False
            result["reason"] = "not_clean"
            raise UniverseNotCleanError(
                f"{source or 'This run'}: universe_holdout_require_fresh=True but "
                f"the reserved symbols are a subset of the mining universe. "
                f"Download disjoint fresh symbols first."
            )
        result["reason"] = "audit_not_clean"

    if not split.clean:
        logger.warning(
            "Universe holdout %s is NOT clean (%s). Results are a mechanism "
            "check, not independent out-of-sample evidence.",
            source or "run", split.note,
        )

    if record and not prior:
        _append_ledger({
            "at": datetime.now().isoformat(timespec="seconds"),
            "split_hash": split.split_hash,
            "source": source,
            "clean": split.clean,
            "mode": resolved,
            "n_train": len(split.train_pairs),
            "n_holdout": len(split.holdout_pairs),
            "holdout_pairs": list(split.holdout_pairs),
            "note": split.note,
        }, result["ledger_path"])
        result["recorded"] = True
    return result


def status(ledger_path: Optional[str] = None) -> Dict[str, Any]:
    path = ledger_path or default_ledger_path()
    entries = _read_ledger(path)
    return {
        "ledger_path": path,
        "n_revealed": len(entries),
        "n_clean": sum(1 for e in entries if e.get("clean")),
        "entries": entries,
    }


__all__ = [
    "ENV_VAR",
    "LEDGER_NAME",
    "UniverseNotCleanError",
    "UniverseReuseError",
    "UniverseSplit",
    "default_ledger_path",
    "guard",
    "holdout_config",
    "resolve_mode",
    "resolve_split",
    "status",
    "train_config",
]
