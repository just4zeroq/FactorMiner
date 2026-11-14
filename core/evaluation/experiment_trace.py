"""Statistical gates — Phase 0.1 / 0.2 of the optimization plan.

These two primitives directly attack the root cause of the "IC looks great but
the factor loses money out-of-sample" failure we reproduced in the strict OOS
validation (2025H1: -11.18%, Sharpe -1.14). The missing piece was not operators
or paradigms (we are already first-tier there) — it was *statistical proof that
a factor is real*, i.e. a one-time holdout and a multiple-testing denominator.

Design is grounded in the two most rigorous open-source prototypes we surveyed:

* ``crypto-factor-lab`` — a one-time holdout ledger: once a holdout window has
  been "revealed" it can never be touched again, and re-revealing or overlapping
  an already-revealed window raises immediately (catalog.py:282-299,
  service.py:900-906/955-966).
* ``LLM-Alpha-Mining-Framework`` — every attempted candidate (including failed
  ones) counts toward the multiple-testing denominator, so a lucky hit out of
  thousands of trials cannot masquerade as significance.

Both are persistence-backed so the constraint holds *across process runs*, which
is the whole point: a holdout you can quietly peek at again is not a holdout.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HoldoutReuseError(RuntimeError):
    """Raised when a holdout window is revealed twice or overlaps a revealed one."""


@dataclass
class _Reveal:
    period_key: str
    start: str
    end: str
    at: str
    note: str = ""


class HoldoutLedger:
    """One-time holdout reveal ledger (persistence-backed, cross-run enforced).

    A "reveal" means *looking at the holdout's labels/returns to judge a factor*.
    Once revealed for a given ``period_key`` (e.g. ``"2025H1"``), that window is
    permanently burned — re-revealing it or revealing any window that *overlaps*
    an already-burned window raises :class:`HoldoutReuseError`.

    This prevents the exact leakage we fixed in the backtest engine (direction
    inferred from the very window being evaluated) from creeping back in at the
    mining-evaluation stage.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(
            "user_workspace", ".holdout_ledger.json"
        )
        self._lock = threading.RLock()
        self._reveals: List[_Reveal] = []
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._reveals = [_Reveal(**r) for r in raw]
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("HoldoutLedger corrupt (%s); starting empty", exc)
                self._reveals = []
        else:
            self._reveals = []

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([asdict(r) for r in self._reveals], fh, indent=2)
        os.replace(tmp, self.path)

    # ---- queries -----------------------------------------------------------
    @staticmethod
    def _overlaps(a0: str, a1: str, b0: str, b1: str) -> bool:
        return a0 <= b1 and b0 <= a1

    def is_revealed(self, period_key: str) -> bool:
        with self._lock:
            return any(r.period_key == period_key for r in self._reveals)

    def overlapping(self, start: str, end: str) -> List[_Reveal]:
        with self._lock:
            return [
                r for r in self._reveals
                if self._overlaps(start, end, r.start, r.end)
            ]

    # ---- mutation ----------------------------------------------------------
    def reveal(
        self,
        period_key: str,
        start: str,
        end: str,
        note: str = "",
        force: bool = False,
    ) -> None:
        """Record a holdout reveal, enforcing one-time + non-overlap rules.

        Pass ``force=True`` only from tests / explicit operator overrides.
        """
        with self._lock:
            if self.is_revealed(period_key) and not force:
                prior = next(
                    r for r in self._reveals if r.period_key == period_key
                )
                raise HoldoutReuseError(
                    f"Holdout period '{period_key}' was already revealed "
                    f"on {prior.at} ({prior.start}..{prior.end}). A holdout may "
                    f"only be revealed ONCE. Reusing it makes every subsequent "
                    f"result in-sample-by-leakage."
                )
            overlaps = self.overlapping(start, end)
            if overlaps and not force:
                detail = ", ".join(
                    f"{r.period_key}({r.start}..{r.end})" for r in overlaps
                )
                raise HoldoutReuseError(
                    f"Reveal window {start}..{end} overlaps already-burned "
                    f"holdout window(s): {detail}. Overlapping reveals leak "
                    f"information and invalidate the test."
                )
            self._reveals.append(
                _Reveal(
                    period_key=period_key,
                    start=start,
                    end=end,
                    at=datetime.now().isoformat(timespec="seconds"),
                    note=note,
                )
            )
            self._save()
            logger.info("Holdout '%s' revealed %s..%s", period_key, start, end)

    def reset(self) -> None:
        """Wipe the ledger (tests / fresh experiment)."""
        with self._lock:
            self._reveals = []
            self._save()

    def snapshot(self) -> List[Dict[str, str]]:
        with self._lock:
            return [asdict(r) for r in self._reveals]


@dataclass
class TrialRecord:
    """One evaluated candidate (success OR failure). The denominator of truth."""

    signature: str
    fitness: float = float("nan")
    rank_ic: float = float("nan")
    rank_ic_oos: float = float("nan")
    significant: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ExperimentTrace:
    """Cross-run record of *every* candidate tried — the multiple-testing base.

    The key idea (from LLM-Alpha-Mining-Framework): to judge whether a factor's
    significance is real, you must divide by the number of things you tried. A
    p=0.01 is convincing after 20 trials but meaningless after 50,000. This
    trace persists every attempt so ``num_trials`` stays honest across runs,
    and supports dedup by expression signature.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(
            "user_workspace", ".experiment_trace.jsonl"
        )
        self._lock = threading.RLock()
        self._records: List[TrialRecord] = []
        self._seen: set = set()
        self._load()

    def _load(self) -> None:
        self._records = []
        self._seen = set()
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = TrialRecord(**json.loads(line))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    self._records.append(rec)
                    self._seen.add(rec.signature)

    def has_seen(self, signature: str) -> bool:
        return signature in self._seen

    def record(self, rec: TrialRecord) -> None:
        with self._lock:
            if rec.signature in self._seen:
                # Update in place rather than double-count (keeps num_trials honest).
                for i, existing in enumerate(self._records):
                    if existing.signature == rec.signature:
                        self._records[i] = rec
                        break
            else:
                self._records.append(rec)
                self._seen.add(rec.signature)
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            for rec in self._records:
                fh.write(json.dumps(asdict(rec)) + "\n")

    # ---- analytics ---------------------------------------------------------
    @property
    def num_trials(self) -> int:
        """Total distinct candidates attempted — the multiple-testing denominator."""
        return len(self._records)

    def num_significant(self, fdr: float = 0.05) -> int:
        return sum(1 for r in self._records if r.significant and r.meta.get("p_adj", 1.0) <= fdr)

    def significant_rate(self, fdr: float = 0.05) -> float:
        n = self.num_trials
        return self.num_significant(fdr) / n if n else 0.0

    def reset(self) -> None:
        with self._lock:
            self._records = []
            self._seen = set()
            self._save()

    def all(self) -> List[TrialRecord]:
        return list(self._records)


def signature_of(expr: Any) -> str:
    """Stable signature for a factor expression (AST dict / code / hashable)."""
    import hashlib

    if isinstance(expr, dict):
        payload = json.dumps(expr, sort_keys=True, default=str)
    elif hasattr(expr, "to_dict"):
        payload = json.dumps(expr.to_dict(), sort_keys=True, default=str)
    else:
        payload = repr(expr)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
