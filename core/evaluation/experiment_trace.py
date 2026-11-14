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
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.evaluation.annualisation import DEFAULT_ANNUALISATION

logger = logging.getLogger(__name__)


def default_workspace_path(filename: str) -> str:
    """Absolute path under the project's ``user_workspace``, cwd-independent.

    P0-b: a holdout ledger is only a holdout if *every* entry point reads the
    same file. Relative paths silently fork the ledger when a CLI is launched
    from another directory (which is exactly how the old ``_p0`` ledger was
    orphaned), so the default is pinned to the project root.

    When ``FACTORMINER_HOLDOUT_LEDGER_PATH`` is set it overrides *only* the
    holdout ledger path (kept separate from the other workspace files). This is
    what lets the test suite redirect the ledger to a temp dir without forking
    the canonical ``experiment_window.json`` — see ``tests/conftest.py``.
    """
    if filename == ".holdout_ledger.json":
        override = os.environ.get("FACTORMINER_HOLDOUT_LEDGER_PATH")
        if override:
            return override
    root = Path(__file__).resolve().parents[2]
    return str(root / "user_workspace" / filename)


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
        self.path = path or default_workspace_path(".holdout_ledger.json")
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


class FdrLedger:
    """Cumulative raw p-values of every hypothesis actually tested.

    BH-FDR only controls the family error rate if the family is the whole set of
    hypotheses you looked at. Correcting inside one round's short list (2–6
    factors) is close to no correction at all: the other 1,000 candidates that
    run — and every candidate from previous runs — silently raise the chance
    that a survivor is luck.

    The trace keeps counts (for DSR); this ledger keeps the raw p-values that
    BH needs. Stored as one JSON line per run so the file stays append-only.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join("user_workspace", ".fdr_ledger.jsonl")
        self._lock = threading.RLock()
        self._entries: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        self._entries = []
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._entries.append(json.loads(line))
                    except json.JSONDecodeError:  # pragma: no cover - defensive
                        continue
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("FDR ledger unreadable (%s): %s", self.path, exc)

    def _save_entry(self, entry: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def append(
        self,
        p_values: List[float],
        run_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Record one run's raw p-values. Idempotent per ``run_id``."""
        import math

        vals: List[float] = []
        for p in p_values or []:
            try:
                val = float(p)
            except (TypeError, ValueError):
                continue
            if math.isfinite(val):
                vals.append(val)
        if not vals:
            return 0
        with self._lock:
            if run_id and any(e.get("run_id") == run_id for e in self._entries):
                return 0
            entry = {
                "run_id": run_id or f"run_{len(self._entries) + 1}",
                "at": datetime.now().isoformat(timespec="seconds"),
                "n": len(vals),
                "p_values": vals,
                "meta": meta or {},
            }
            self._entries.append(entry)
            self._save_entry(entry)
        return len(vals)

    def cumulative(self) -> List[float]:
        with self._lock:
            out: List[float] = []
            for entry in self._entries:
                out.extend(float(p) for p in entry.get("p_values", []))
            return out

    @property
    def num_tests(self) -> int:
        return len(self.cumulative())

    def budget_report(
        self,
        n_obs: Optional[int] = None,
        budget: Optional[int] = None,
        annualisation: float = DEFAULT_ANNUALISATION,
    ) -> Dict[str, Any]:
        """How much of the hypothesis budget is spent, and what bar it implies.

        The DSR benchmark grows with ``n_trials``: mining more ideas makes every
        individual idea harder to promote. That trade-off used to be invisible —
        the gate simply got stricter with no signal to the researcher. This
        report makes it explicit so a run can be planned (or stopped) knowingly.
        """
        from core.evaluation.significance import multiple_testing_bar

        with self._lock:
            runs = len(self._entries)
        tests = self.num_tests
        report: Dict[str, Any] = {
            "runs": runs,
            "cumulative_tests": tests,
            "budget": int(budget) if budget else None,
            "utilization": (tests / budget) if budget else None,
            "ledger_path": self.path,
        }
        if n_obs:
            report.update(multiple_testing_bar(n_obs, max(1, tests), annualisation))
        return report

    def reset(self) -> None:
        with self._lock:
            self._entries = []
            if self.path and os.path.exists(self.path):
                os.remove(self.path)


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
