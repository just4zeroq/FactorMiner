"""One source of truth for trading costs across fitness and backtest.

The promotion gate charges ``gate_cost_rate`` per unit of long-short turnover;
the event-driven backtest charges commission + slippage on each side. Those two
numbers describe the same economic cost and must agree, otherwise a factor can
pass the gate yet lose money in the backtest (or vice versa).

This module does not change any number: it derives both from the config,
verifies the identity ``gate_cost_rate == 2 * (commission + slippage)`` and
makes the result part of the run manifest/report.
"""

from __future__ import annotations

from typing import Any, Dict

DEFAULTS = {
    "gate_cost_rate": 0.0018,   # round-trip, 18 bp
    "commission_rate": 0.0004,  # per side, taker
    "slippage_rate": 0.0005,    # per side
}


def resolve_cost_params(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = config or {}
    fitness = cfg.get("fitness", {}) or {}
    backtest = cfg.get("backtest", {}) or {}
    gate = float(fitness.get("gate_cost_rate", DEFAULTS["gate_cost_rate"]))
    commission = float(backtest.get("commission_rate", DEFAULTS["commission_rate"]))
    slippage = float(backtest.get("slippage_rate", DEFAULTS["slippage_rate"]))
    implied = 2.0 * (commission + slippage)
    delta = implied - gate
    return {
        "gate_cost_rate": gate,
        "commission_rate": commission,
        "slippage_rate": slippage,
        "implied_round_trip": round(implied, 8),
        "delta": round(delta, 8),
        "consistent": abs(delta) < 1e-6,
        "identity": "gate_cost_rate = 2 * (commission_rate + slippage_rate)",
        "funding": {
            "mode": (backtest.get("funding") or "constant"),
            "note": "funding is charged separately via crypto_streams when real_funding=true",
        },
    }
