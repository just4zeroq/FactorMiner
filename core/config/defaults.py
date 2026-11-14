"""Single source of truth for shared default configuration constants.

The promotion gate, the long-short book, the walk-forward validation and the
gate-calibration panel all charge the *same* round-trip trading cost. That
number must be defined exactly once and imported by every consumer, otherwise a
factor can pass the gate on one cost basis yet lose money in the backtest on
another (review A2 / F2).

The canonical value is owned by :mod:`core.experiment.costs` — the module that
also enforces the ``gate_cost_rate == 2 * (commission_rate + slippage_rate)``
identity — and re-exported here so ``core.config.defaults`` is the one import
point for shared defaults.
"""

from __future__ import annotations

from core.experiment.costs import DEFAULTS, resolve_cost_params

# Canonical round-trip trading cost (18 bp). Used by the gate, the long-short
# book, walk-forward validation and gate-calibration. DO NOT re-hardcode 0.0018
# anywhere else — import this constant.
DEFAULT_COST_RATE: float = float(DEFAULTS["gate_cost_rate"])

__all__ = ["DEFAULT_COST_RATE", "DEFAULTS", "resolve_cost_params"]
