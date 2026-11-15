"""Pre-flight cost estimate for ``experiment --dry-run`` (F8).

A run takes 10–15 minutes and the only way to learn "how many candidates will
this produce, and can any of them possibly clear DSR?" was to wait for it. This
module answers both from the config alone: candidate-count arithmetic from the
search parameters, wall-clock from the measured per-candidate cost profile, and
the DSR bar the current hypothesis budget implies.

The numbers are estimates, and are labelled as such. Their purpose is to stop a
structurally hopeless run *before* it starts (e.g. a 1-bar horizon on an 18bp
round trip, or a budget that has already pushed the bar above the best signal
this market has shown).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# Measured on a 20-asset x ~1200-bar 1d panel (docs/reviews/
# mining_production_readiness.md §1.1): 4.6s of which 4.6*0.978 is the
# target-permutation null at n_perm=60.
CHEAP_SECONDS = 0.05
PERM_SECONDS_AT_60 = 4.6 * 0.978
DEFAULT_OBS = 1200


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bars_between(periods: Any) -> Optional[int]:
    if not periods:
        return None
    try:
        import pandas as pd

        first = periods[0]
        start, end = first[0], first[-1]
        return max(1, len(pd.date_range(str(start), str(end), freq="D")))
    except Exception:
        return None


def estimate(config: Dict[str, Any], *, n_trials: Optional[int] = None) -> Dict[str, Any]:
    """Return candidate/throughput/gate projections for a config."""
    fitness = config.get("fitness") or {}
    gates = ((config.get("evaluation") or {}).get("gates") or {})
    feeds = config.get("data_feeds") or {}

    population = _as_int(config.get("population_size"), 20)
    iterations = _as_int(config.get("max_iterations"), 5)
    survival = _as_int(config.get("survival_count"), max(1, population // 2))
    elite = _as_int((config.get("gp") or {}).get("elite_count"), 0)

    # Per-epoch: a fresh population plus the survivors carried over; the value
    # dedup filter rejects near-duplicates, measured at roughly a quarter.
    dedup_rate = 0.25
    fresh = population * (1.0 - dedup_rate)
    candidates = int(round(fresh * iterations + survival * max(0, iterations - 1) + elite))

    prefilter_keep = _as_int(fitness.get("prefilter_keep"), 32)
    prefilter_on = bool(fitness.get("prefilter_enabled", True))
    stage_b = min(prefilter_keep, candidates) if prefilter_on else candidates

    n_perm = _as_int(gates.get("null_n_perm"), 0)
    perm_seconds = PERM_SECONDS_AT_60 * (n_perm / 60.0) if n_perm > 0 else 0.0
    seconds = candidates * CHEAP_SECONDS + stage_b * perm_seconds

    n_obs = _bars_between(feeds.get("mine_period")) or DEFAULT_OBS
    horizon = _as_int((config.get("target") or {}).get("horizon_bars"), 1)
    rebalance = _as_int(config.get("rebalance_every"), horizon)
    dsr_bar = _dsr_bar(n_trials, n_obs)

    warnings: List[str] = []
    if horizon <= 1 and rebalance <= 1:
        warnings.append(
            "horizon=1 with 1-bar rebalancing: measured net Sharpe 0.223 vs 0.675 "
            "for the same signal at 5-bar rebalancing — the round trip eats the edge."
        )
    turnover_max = float(fitness.get("turnover_max", 0.0) or 0.0)
    if turnover_max <= 0:
        warnings.append(
            "fitness.turnover_max is 0 (off): nothing stops the search from "
            "breeding candidates that cannot pay the 18bp round trip."
        )
    if n_perm <= 0:
        warnings.append(
            "evaluation.gates.null_n_perm is 0: the empirical-null gate is silent."
        )
    if not prefilter_on:
        warnings.append(
            "fitness.prefilter_enabled is false: every candidate pays the "
            "permutation cost (~4.6s each)."
        )
    if dsr_bar is not None and dsr_bar.get("annualised_sharpe", 0) > 1.0:
        warnings.append(
            f"DSR bar at n_trials={n_trials} is {dsr_bar['annualised_sharpe']:.2f} "
            "annualised Sharpe. Measured best simple signal in this market: 0.675. "
            "Raising the trial count will not help; raise the signal instead."
        )

    return {
        "candidates_est": candidates,
        "stage_b_est": stage_b,
        "stage_a_reject_est": max(0, candidates - stage_b),
        "seconds_est": round(seconds, 1),
        "minutes_est": round(seconds / 60.0, 1),
        "n_perm": n_perm,
        "prefilter_enabled": prefilter_on,
        "prefilter_keep": prefilter_keep,
        "n_trials": n_trials,
        "n_obs_est": n_obs,
        "target": {
            "horizon_bars": horizon,
            "rebalance_every": rebalance,
            "entry_price": (config.get("target") or {}).get("entry_price"),
        },
        "gates": {
            key: gates.get(key)
            for key in (
                "hac_t_min", "fdr_max", "dsr_min", "empirical_p_max",
                "oos_rankic_min", "cost_adj_return_min", "ls_t_min",
            )
            if key in gates
        },
        "dsr_bar": dsr_bar,
        "warnings": warnings,
        "disclaimer": "Estimates from the measured cost profile; not a forecast.",
    }


def _dsr_bar(n_trials: Optional[int], n_obs: int) -> Optional[Dict[str, Any]]:
    if not n_trials or n_trials < 2:
        return None
    try:
        import numpy as np

        from core.evaluation.significance import expected_max_sr

        sigma = 1.0 / math.sqrt(max(n_obs - 1, 1))
        bar = expected_max_sr(int(n_trials), sigma=sigma)
        return {
            "n_trials": int(n_trials),
            "per_period_sharpe": float(bar),
            "annualised_sharpe": float(bar * math.sqrt(252.0)),
        }
    except Exception:  # pragma: no cover - estimate must never break a dry run
        return None


def format_estimate(payload: Dict[str, Any], config_path: str) -> str:
    lines = [
        f"config       : {config_path}",
        f"candidates   : ~{payload['candidates_est']} "
        f"({payload['stage_b_est']} reach the permutation null, "
        f"~{payload['stage_a_reject_est']} rejected by the cheap screen)",
        f"wall clock   : ~{payload['minutes_est']} min "
        f"(n_perm={payload['n_perm']}, prefilter={'on' if payload['prefilter_enabled'] else 'off'})",
        f"target       : horizon={payload['target']['horizon_bars']} "
        f"rebalance={payload['target']['rebalance_every']} "
        f"entry={payload['target']['entry_price']}",
        "gates        : " + ", ".join(
            f"{k}={v}" for k, v in payload["gates"].items()
        ),
        f"n_trials     : {payload['n_trials']}",
    ]
    bar = payload.get("dsr_bar")
    if bar:
        lines.append(
            f"DSR bar      : {bar['per_period_sharpe']:.4f}/period = "
            f"{bar['annualised_sharpe']:.2f} annualised Sharpe at n_trials={bar['n_trials']}"
        )
    if payload["warnings"]:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in payload["warnings"])
    lines.append("")
    lines.append(payload["disclaimer"])
    return "\n".join(lines)


__all__ = ["estimate", "format_estimate"]
