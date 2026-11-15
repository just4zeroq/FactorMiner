"""Stage-A cheap screen for the promotion funnel (L1.1).

Measured cost profile of one candidate evaluation (20-asset 1d panel, 1200 bars):

    ic_series                 25 ms   0.5%
    newey_west_tstat         0.1 ms    ~0%
    long_short_returns        17 ms   0.4%
    target_permutation (60) 4599 ms  97.8%

Everything the cheap screen needs is therefore *already computed* by the time a
candidate reaches the director; the only expensive step left is the target
permutation null. Running that null for every candidate is what makes 100k
candidates a 76-hour job. This module decides which candidates are worth it.

The screen is deliberately **not** a loosened promotion gate. A candidate that
fails Stage A is never promoted and never reported as "tested" — it is dropped
with an explicit reason, so no verdict is derived from a missing statistic
(the old gate treated "empirical_p not computed" as a pass, which is exactly
the failure mode this module prevents).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def screen_candidate(
    metrics: Dict[str, Any],
    *,
    min_abs_rankic: float = 0.01,
    min_cost_adj: float = 0.0,
    coverage_floor: float = 0.5,
    turnover_max: float = 0.0,
) -> Tuple[bool, str]:
    """Return ``(keep, reason)`` for one candidate's already-computed metrics."""
    if not isinstance(metrics, dict) or not metrics:
        return False, "no_metrics"

    if metrics.get("turnover_blocked"):
        return False, "turnover_above_cap"

    turnover = _num(metrics.get("ls_turnover"))
    if turnover_max > 0 and turnover is not None and turnover > turnover_max:
        return False, "turnover_above_cap"

    coverage = _num(metrics.get("coverage"))
    if coverage is None:
        # Older / scalar-only paths may not emit coverage; do not reject on a
        # missing diagnostic, the gate still has cost + significance.
        coverage = 1.0
    if coverage < coverage_floor:
        return False, "coverage_below_floor"

    rankic = _num(metrics.get("RankIC"))
    if rankic is None:
        return False, "rankic_unavailable"
    if abs(rankic) < min_abs_rankic:
        return False, "rankic_below_floor"

    cost_adj = _num(metrics.get("cost_adj_return"))
    if cost_adj is None:
        # No long/short book was buildable (e.g. single-asset sequential mode
        # without a temporal verdict). Let Stage B decide rather than reject on
        # a missing diagnostic.
        return True, "stage_b"
    if cost_adj < min_cost_adj:
        return False, "cost_adj_negative"

    return True, "stage_b"


def stage_a(
    metrics_list: Sequence[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
    *,
    keep: Optional[int] = None,
) -> Dict[str, Any]:
    """Split indices into Stage-B survivors and Stage-A rejects.

    ``keep`` caps how many candidates survive (ranked by fitness), so the cost
    of Stage B is bounded by a constant instead of by the search size.

    Returns::

        {"survivors": [int], "rejected": {int: reason}, "reasons": {reason: n}}
    """
    cfg = cfg or {}
    min_abs_rankic = float(cfg.get("prefilter_min_abs_rankic", 0.01) or 0.0)
    min_cost_adj = float(cfg.get("prefilter_min_cost_adj", 0.0) or 0.0)
    coverage_floor = float(cfg.get("prefilter_coverage_floor", 0.5) or 0.0)
    turnover_max = float(cfg.get("turnover_max", 0.0) or 0.0)
    cap = keep if keep is not None else cfg.get("prefilter_keep")
    cap = int(cap) if cap is not None else 0

    survivors: List[int] = []
    rejected: Dict[int, str] = {}
    reasons: Dict[str, int] = {}
    for i, metrics in enumerate(metrics_list):
        ok, reason = screen_candidate(
            metrics or {},
            min_abs_rankic=min_abs_rankic,
            min_cost_adj=min_cost_adj,
            coverage_floor=coverage_floor,
            turnover_max=turnover_max,
        )
        if ok:
            survivors.append(i)
        else:
            rejected[i] = reason
            reasons[reason] = reasons.get(reason, 0) + 1

    dropped_by_cap = 0
    if cap > 0 and len(survivors) > cap:
        def _fitness(i: int) -> float:
            return _num((metrics_list[i] or {}).get("fitness_score"), -1e18) or -1e18

        ordered = sorted(survivors, key=_fitness, reverse=True)
        kept = set(ordered[:cap])
        dropped_by_cap = len(survivors) - cap
        for i in survivors:
            if i not in kept:
                rejected[i] = "rank_cut"
        reasons["rank_cut"] = reasons.get("rank_cut", 0) + dropped_by_cap
        survivors = sorted(kept)

    return {
        "survivors": survivors,
        "rejected": rejected,
        "reasons": reasons,
        "dropped_by_cap": dropped_by_cap,
    }


__all__ = ["screen_candidate", "stage_a"]
