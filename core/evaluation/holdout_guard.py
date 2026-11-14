"""Single-entry guard for one-time holdout access (P0-b).

Problem this solves
-------------------
``HoldoutLedger`` implements the right rule (a revealed window can never be
touched again), but before this module only ``FactorMinerDirector._enforce_holdout``
called it, and only when ``evaluation.gates.strict_holdout`` happened to be
configured. Everything else that reads holdout returns — the ``backtest`` and
``inspect`` CLIs, ad-hoc OOS scripts — bypassed it. The ledger path also drifted
with the config: the real P0 reveal lived in ``.holdout_ledger_p0.json`` while
``HoldoutLedger``'s default pointed at an empty ``.holdout_ledger.json``.

``crypto-factor-lab`` has the same bug in reverse (its ledger is enforced only
on the HTTP path, not the CLI), which is why this guard is deliberately a
*standalone module* rather than a method on the director: every entry point
must call it before computing anything on a holdout window.

Usage
-----
    from core.evaluation import holdout_guard

    holdout_guard.guard(
        start, end,
        purpose="final",          # "final" registers a reveal; "explore" only checks
        config=cfg,               # optional: evaluation.gates.holdout_mode
        source="cli:backtest",
    )

Modes (``evaluation.gates.holdout_mode`` or env ``FACTORMINER_HOLDOUT_MODE``):

* ``strict`` (default) — overlapping a burned window raises ``HoldoutReuseError``.
* ``audit``            — logs a warning and continues (for a dry-run period).
* ``off``              — no check at all (escape hatch, still logged as config).

``--allow-holdout-reuse`` permits recomputing an already published result, but
always writes an immutable audit line to ``reports/holdout_overrides.jsonl``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pandas as pd

from core.evaluation.experiment_trace import (
    HoldoutLedger,
    HoldoutReuseError,
    default_workspace_path,
)

logger = logging.getLogger(__name__)

VALID_MODES = ("strict", "audit", "off")
VALID_PURPOSES = ("explore", "final")
MODE_ENV_VAR = "FACTORMINER_HOLDOUT_MODE"

ACCESS_LOG_NAME = "reports/holdout_access.jsonl"
OVERRIDE_LOG_NAME = "reports/holdout_overrides.jsonl"
WINDOW_SPEC_NAME = "experiment_window.json"
LEDGER_NAME = ".holdout_ledger.json"

_LOG_LOCK = threading.RLock()


def default_ledger_path() -> str:
    """The one canonical ledger, independent of the current working directory."""
    return default_workspace_path(LEDGER_NAME)


def load_window_spec(path: Optional[str] = None) -> Dict[str, Any]:
    """Pre-registered fresh window (``user_workspace/experiment_window.json``)."""
    p = Path(path or default_workspace_path(WINDOW_SPEC_NAME))
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        logger.warning("Cannot read holdout window spec %s: %s", p, exc)
        return {}


def resolve_mode(config: Optional[Dict] = None, explicit: Optional[str] = None) -> str:
    """Explicit arg > ``evaluation.gates.holdout_mode`` > env > ``strict``."""
    for candidate in (
        explicit,
        (((config or {}).get("evaluation") or {}).get("gates") or {}).get("holdout_mode"),
        os.environ.get(MODE_ENV_VAR),
    ):
        if candidate and str(candidate).lower() in VALID_MODES:
            return str(candidate).lower()
    return "strict"


def _normalise(value: Any) -> Optional[str]:
    """To ``YYYY-MM-DD`` so ledger string comparison is well-defined."""
    if value is None or value == "":
        return None
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        return str(value)[:10]


def _append_jsonl(filename: str, record: Dict[str, Any]) -> None:
    path = Path(default_workspace_path(filename))
    with _LOG_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - logging must never break a run
            logger.warning("Cannot write holdout audit log %s: %s", path, exc)


def _log_access(result: Dict[str, Any], event: str, **extra: Any) -> None:
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **{k: v for k, v in result.items() if k not in ("overlaps",)},
        "overlaps": [r.get("period_key") for r in result.get("overlaps", [])],
        **extra,
    }
    _append_jsonl(ACCESS_LOG_NAME, record)


def guard(
    start: Any = None,
    end: Any = None,
    *,
    purpose: str = "explore",
    config: Optional[Dict] = None,
    mode: Optional[str] = None,
    allow_reuse: bool = False,
    ledger_path: Optional[str] = None,
    source: str = "",
    note: str = "",
) -> Dict[str, Any]:
    """Check (and, for ``final``, register) access to one evaluation window.

    Raises :class:`HoldoutReuseError` in ``strict`` mode when the window
    overlaps any already-revealed holdout. Returns an audit dict otherwise.
    """
    if purpose not in VALID_PURPOSES:
        raise ValueError(f"purpose must be one of {VALID_PURPOSES}, got {purpose!r}")

    resolved_mode = resolve_mode(config, mode)
    result: Dict[str, Any] = {
        "allowed": True,
        "mode": resolved_mode,
        "purpose": purpose,
        "source": source,
        "start": _normalise(start),
        "end": _normalise(end),
        "original_start": str(start) if start is not None else None,
        "original_end": str(end) if end is not None else None,
        "overlaps": [],
        "revealed": False,
        "override": False,
        "reason": "clean",
        "ledger_path": ledger_path or default_ledger_path(),
    }
    if resolved_mode == "off":
        result["reason"] = "mode=off"
        _log_access(result, "mode_off")
        return result

    if result["start"] is None and result["end"] is None:
        if purpose == "final":
            raise ValueError(
                "A final holdout evaluation must declare --start/--end explicitly; "
                "an unbounded window cannot be registered as a one-time reveal."
            )
        result["reason"] = "unbounded"
        _log_access(result, "unbounded")
        logger.warning(
            "Holdout guard: %s did not declare a window; cannot verify whether "
            "burned holdout returns were read. Pass --start/--end explicitly.",
            source or "caller",
        )
        return result

    # Open-ended windows are treated as extending to infinity so a one-sided
    # range cannot slip past an overlap check.
    lo = result["start"] or "0000-01-01"
    hi = result["end"] or "9999-12-31"
    ledger = HoldoutLedger(ledger_path or default_ledger_path())
    burned = ledger.overlapping(lo, hi)
    result["overlaps"] = [
        {"period_key": r.period_key, "start": r.start, "end": r.end, "at": r.at}
        for r in burned
    ]

    if burned:
        detail = ", ".join(
            f"{r.period_key}({r.start}..{r.end} @ {r.at})" for r in burned
        )
        if allow_reuse:
            result["override"] = True
            result["reason"] = "allow_reuse"
            _log_access(result, "override", burned=detail, note=note)
            _append_jsonl(
                OVERRIDE_LOG_NAME,
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "source": source,
                    "start": lo,
                    "end": hi,
                    "purpose": purpose,
                    "mode": resolved_mode,
                    "burned": result["overlaps"],
                    "note": note or "explicit --allow-holdout-reuse",
                },
            )
            logger.warning(
                "Holdout guard OVERRIDE: %s reuses burned window(s) %s. "
                "Result may only be used to recompute a published number.",
                source or "caller", detail,
            )
            return result

        if resolved_mode == "strict":
            result["allowed"] = False
            result["reason"] = "burned"
            _log_access(result, "blocked", burned=detail)
            raise HoldoutReuseError(
                f"{source or 'This entry point'}: window {lo}..{hi} overlaps "
                f"already-revealed holdout window(s): {detail}. Re-reading a "
                f"burned holdout makes every subsequent result in-sample by "
                f"leakage. Use a fresh pre-registered window (see "
                f"user_workspace/{WINDOW_SPEC_NAME}), or pass "
                f"--allow-holdout-reuse ONLY to recompute a published result "
                f"(an audit line will be written)."
            )

        result["reason"] = "audit_mode_allowed"
        _log_access(result, "audit_allowed", burned=detail)
        logger.warning(
            "Holdout guard (audit mode): %s would be blocked for reusing %s. "
            "Switch evaluation.gates.holdout_mode to 'strict' to enforce.",
            source or "caller", detail,
        )
        return result

    if purpose == "final":
        spec = load_window_spec()
        period_key = f"holdout_{lo}_{hi}"
        label = spec.get("label")
        if spec.get("start") and lo < _normalise(spec["start"]):
            logger.warning(
                "Holdout guard: final window %s starts before the pre-registered "
                "fresh window %s (%s). Registering anyway; confirm this is not "
                "a re-used in-sample period.", lo, spec.get("start"), label,
            )
        ledger.reveal(
            period_key,
            lo,
            hi,
            note=note or f"one-time final verification via {source or 'guard'}"
                        + (f" [{label}]" if label else ""),
        )
        result["revealed"] = True
        result["reason"] = "revealed"
        _log_access(result, "revealed", period_key=period_key)
        logger.info("Holdout guard: registered one-time reveal %s (%s..%s)",
                    period_key, lo, hi)

    return result


def guard_periods(
    periods: Optional[Sequence[Sequence[Any]]],
    **kwargs: Any,
) -> list[Dict[str, Any]]:
    """Guard every ``[start, end]`` pair of an inspector/multi-fold request."""
    if not periods:
        return [guard(None, None, **kwargs)]
    out = []
    for item in periods:
        if isinstance(item, dict):
            start, end = item.get("start"), item.get("end")
        else:
            pair = list(item)
            start = pair[0] if pair else None
            end = pair[1] if len(pair) > 1 else None
        out.append(guard(start, end, **kwargs))
    return out


def guard_config(
    config: Dict[str, Any],
    *,
    source: str = "",
    allow_reuse: bool = False,
    mode: Optional[str] = None,
    ledger_path: Optional[str] = None,
    note: str = "",
) -> list[Dict[str, Any]]:
    """Convenience entry for scripts that hold a full run config.

    Honours ``evaluation.gates.strict_holdout`` when present (final reveal),
    otherwise checks every ``data_feeds.test_period`` window (explore). Drop
    two lines into an OOS script's ``main()`` before it reads any returns::

        from core.evaluation import holdout_guard
        holdout_guard.guard_config(cfg, source="script:my_oos_check")
    """
    gates = ((config or {}).get("evaluation") or {}).get("gates") or {}
    common = dict(
        config=config, mode=mode, allow_reuse=allow_reuse,
        ledger_path=ledger_path, source=source, note=note,
    )
    strict = gates.get("strict_holdout")
    if isinstance(strict, dict):
        return [
            guard(strict.get("start"), strict.get("end"), purpose="final", **common)
        ]
    periods = ((config or {}).get("data_feeds") or {}).get("test_period") or []
    return guard_periods(periods, purpose="explore", **common)


__all__ = [
    "HoldoutReuseError",
    "VALID_MODES",
    "default_ledger_path",
    "guard",
    "guard_config",
    "guard_periods",
    "load_window_spec",
    "resolve_mode",
]
