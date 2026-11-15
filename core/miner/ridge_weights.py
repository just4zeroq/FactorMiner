"""Scale-aware ridge fitting for factor combination layers (review M-02).

Problem
-------
``AlphaPool._optimize_weights`` and ``fit_linear_combination`` used to solve
``(X'X + lambda*I) w = X'y`` directly on *raw* factor panels. Sub-factors live
on wildly different scales (``ts_rank`` in [0, 1], ``cumsum`` ~1e5, ``close``
~1e4), so a single scalar ``lambda`` is effectively infinite regularisation
for small-scale columns and almost no regularisation for large-scale ones.
Without an intercept, non-zero-mean columns also absorb the target mean into
their slopes.

Fix
---
Fit in a standardised space with an *unpenalised* intercept:

1. Apply the finite mask first, then compute per-column mean / std on the
   fitting sample only (never on OOS data).
2. z-score each column; constant columns are flagged degenerate and given a
   zero slope instead of dividing by ~0 (which produced 1e19-scale weights).
3. Prepend a column of ones with zero ridge penalty, so the target mean goes
   into the intercept rather than into the slopes. After standardisation every
   column has unit variance, so ``lambda`` is automatically scale-normalised.
4. Map the fitted slopes back to **raw space** (``w_raw = w_z / std``,
   ``b_raw = b - sum(w_z * mean / std)``) before returning them.

Why the raw-space mapping matters
---------------------------------
``LinearCombinedFactor.compute`` evaluates ``sum(w_j * factor_j)`` on raw
factor panels, and ``backtest_engine.resolve_signal_weights`` reads the stored
weights the same way. Returning z-space slopes would silently change every
combination factor and every backtest. Mapping back keeps the serialised
weights in the exact same space as before, so no scaler has to be persisted or
re-applied on OOS data: OOS reuse of the *same* scaler is achieved by algebra
instead of by state.

The dropped intercept is a single constant added to every cell of every
cross-section. RankIC, rank-based long/short signals and the backtest are all
invariant to such a constant, so it is safe to omit it from the serialised
factor; it is still returned in :meth:`RidgeFit.diagnostics` for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


@dataclass
class RidgeFit:
    """Result of :func:`fit_scale_aware_ridge`.

    ``weights`` are raw-space slopes, directly usable by
    ``LinearCombinedFactor`` / ``AlphaPool.combined_panel``. ``fallback`` is
    True when the fit degenerated and ``weights`` are the equal weights.
    """

    weights: List[float]
    intercept: float
    intercept_standardized: float
    center: List[float]
    scale: List[float]
    standardized_weights: List[float]
    n_obs: int
    degenerate: List[int]
    method: str
    reason: Optional[str] = None
    fallback: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def diagnostics(self) -> Dict[str, Any]:
        """Audit-friendly summary; safe to put in a pool report."""
        return {
            "method": self.method,
            "reason": self.reason,
            "fallback": self.fallback,
            "n_obs": self.n_obs,
            "intercept": round(self.intercept, 8),
            "intercept_standardized": round(self.intercept_standardized, 8),
            "center": [round(float(x), 8) for x in self.center],
            "scale": [round(float(x), 8) for x in self.scale],
            "standardized_weights": [round(float(x), 8) for x in self.standardized_weights],
            "degenerate_columns": list(self.degenerate),
            **self.extra,
        }


def fit_scale_aware_ridge(
    X: np.ndarray,
    y: np.ndarray,
    *,
    reg_lambda: float = 1.0,
    min_obs: int = 60,
    equal_weights: Optional[Sequence[float]] = None,
) -> RidgeFit:
    """Fit scale-normalised ridge slopes and return them in raw X space.

    ``X`` is ``(n, k)`` and ``y`` is ``(n,)``. Non-finite rows are dropped.
    Any degenerate case (too few rows, constant target, singular gram matrix,
    non-finite solution) returns finite equal weights instead of raising.
    """
    x = np.asarray(X, dtype=float)
    yv = np.asarray(y, dtype=float).reshape(-1)
    if x.ndim != 2:
        raise ValueError("X must be a 2-D (n_obs, n_factors) array")
    n_rows, k = x.shape
    if yv.size != n_rows:
        raise ValueError("y must have one entry per row of X")

    eq = np.asarray(
        equal_weights if equal_weights is not None else [1.0 / max(k, 1)] * k,
        dtype=float,
    )
    if eq.size != k:
        eq = np.full(k, 1.0 / max(k, 1))

    def _equal(reason: str, *, n_obs: int = 0, method: str = "equal") -> RidgeFit:
        return RidgeFit(
            weights=[float(v) for v in eq],
            intercept=0.0,
            intercept_standardized=0.0,
            center=[0.0] * k,
            scale=[1.0] * k,
            standardized_weights=[0.0] * k,
            n_obs=n_obs,
            degenerate=[],
            method=method,
            reason=reason,
            fallback=True,
        )

    mask = np.isfinite(x).all(axis=1) & np.isfinite(yv)
    xm, ym = x[mask], yv[mask]
    n_obs = int(xm.shape[0])
    if n_obs < min_obs:
        return _equal("insufficient_rows", n_obs=n_obs)
    if np.unique(ym).size < 2:
        return _equal("constant_target", n_obs=n_obs)

    center = xm.mean(axis=0)
    scale = xm.std(axis=0)
    # Relative threshold: a column of constant 1e12 must still be degenerate.
    degenerate_mask = scale <= 1e-12 * np.maximum(1.0, np.abs(center))
    scale_eff = np.where(degenerate_mask, 1.0, scale)
    z = (xm - center) / scale_eff
    z[:, degenerate_mask] = 0.0  # no information, and never divide by ~0

    design = np.column_stack([np.ones(n_obs), z])
    penalty = np.zeros(k + 1)
    penalty[1:] = float(reg_lambda)  # intercept is NOT penalised
    gram = design.T @ design + np.diag(penalty)
    rhs = design.T @ ym

    method = "ridge"
    try:
        beta = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:
        try:
            beta = np.linalg.lstsq(gram, rhs, rcond=None)[0]
            method = "ridge_lstsq"
        except np.linalg.LinAlgError:
            return _equal("singular_gram", n_obs=n_obs)

    intercept_z = float(beta[0])
    w_z = np.asarray(beta[1:], dtype=float)
    w_z[degenerate_mask] = 0.0

    w_raw = w_z / scale_eff
    intercept_raw = intercept_z - float(np.sum(w_z * center / scale_eff))
    if not (np.all(np.isfinite(w_raw)) and np.isfinite(intercept_raw)):
        return _equal("non_finite_solution", n_obs=n_obs)

    return RidgeFit(
        weights=[float(v) for v in w_raw],
        intercept=intercept_raw,
        intercept_standardized=intercept_z,
        center=[float(v) for v in center],
        scale=[float(v) for v in scale_eff],
        standardized_weights=[float(v) for v in w_z],
        n_obs=n_obs,
        degenerate=[int(i) for i in np.flatnonzero(degenerate_mask)],
        method=method,
        fallback=False,
        extra={
            "reg_lambda": float(reg_lambda),
            "intercept_dropped_by_rank_consumers": True,
        },
    )


__all__ = ["RidgeFit", "fit_scale_aware_ridge"]
