"""Static AlphaPool — Phase 2 combination layer.

A fixed-capacity pool of factors. A new factor is admitted only if its
cross-sectional IC **time-series** is not redundant (mutual-IC > 0.99 rejected,
à la AlphaGen) with the existing members. After every admission the whole-pool
linear weights are re-optimized (ridge least-squares on the IC target, à la
AlphaForge's per-period ``lstsq`` with an optional forward shift to avoid
look-ahead), so the pool directly maximizes *combined* IC rather than single-
factor IC.

Look-ahead safety
----------------
* Weights are always fit on the factor panels passed in (typically in-sample).
* The weight-regression target can be shifted forward by ``weight_fit_shift``
  steps (AlphaForge's ``shift=21`` guard) so the fit predicts returns that are
  ``weight_fit_shift`` periods *ahead* of the factor values, never overlapping
  the window the factor was computed from.
* The combined factor's OOS IC (computed by the evaluator afterward) is
  therefore an honest generalization measure, not inflated by in-sample fit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.evaluation.significance import ic_series, newey_west_tstat
from core.miner.ridge_weights import fit_scale_aware_ridge


# --------------------------------------------------------------------------- #
# Panel helpers
# --------------------------------------------------------------------------- #
def long_to_wide(
    long_df: pd.DataFrame,
    value_col: str = "factor",
    index_col: str = "timestamp",
    col_col: str = "asset",
) -> pd.DataFrame:
    """Pivot a long ``(index, asset, value)`` table into a ``(T, N)`` wide panel."""
    return long_df.pivot(index=index_col, columns=col_col, values=value_col).sort_index()


def _to_wide_array(obj: Any, ref_index=None, ref_columns=None) -> np.ndarray:
    """Coerce a DataFrame/Series/ndarray to a ``(T, N)`` float ndarray."""
    if isinstance(obj, pd.DataFrame):
        if ref_index is not None:
            obj = obj.reindex(index=ref_index, columns=ref_columns)
        return obj.to_numpy(dtype=float, na_value=float("nan"))
    if isinstance(obj, pd.Series):
        return obj.to_numpy(dtype=float, na_value=float("nan")).reshape(-1, 1)
    arr = np.asarray(obj, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    return arr


def _safe_pearson(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 10:
        return None
    aa = a[m] - a[m].mean()
    bb = b[m] - b[m].mean()
    denom = float(np.sqrt((aa**2).sum() * (bb**2).sum()))
    return float((aa * bb).sum() / denom) if denom > 0 else None


# --------------------------------------------------------------------------- #
# AlphaPool
# --------------------------------------------------------------------------- #
class AlphaPool:
    """Fixed-capacity pool that combines non-redundant factors via ridge weights."""

    def __init__(
        self,
        capacity: int = 10,
        mutual_ic_max: float = 0.99,
        weight_fit_shift: int = 21,
        reg_lambda: float = 5.0,
        min_obs: int = 200,
        weight_norm: str = "l1",
    ):
        self.capacity = int(capacity)
        self.mutual_ic_max = float(mutual_ic_max)
        self.weight_fit_shift = int(weight_fit_shift)
        self.reg_lambda = float(reg_lambda)
        self.min_obs = int(min_obs)
        self.weight_norm = weight_norm
        self._ids: List[str] = []
        self._panels: List[np.ndarray] = []  # aligned (T, N) arrays
        self._weights: List[float] = []
        self._dates = None
        self._assets = None
        self._returns: Optional[np.ndarray] = None  # (T, N) forward-return target
        self._admit_log: List[Dict[str, Any]] = []

    # ---- target --------------------------------------------------------- #
    def set_target(self, returns, index=None, columns=None) -> None:
        if isinstance(returns, pd.DataFrame):
            self._dates = returns.index
            self._assets = returns.columns
            self._returns = returns.to_numpy(dtype=float, na_value=float("nan"))
        else:
            arr = _to_wide_array(returns)
            self._returns = arr
            self._dates = index
            self._assets = columns

    def _target_for_shift(self, shift: int) -> np.ndarray:
        if shift <= 0:
            return self._returns
        # Predict returns ``shift`` steps ahead: roll forward then NaN the tail
        # so the circular wrap-around cannot leak future information.
        tgt = np.roll(self._returns, -shift, axis=0)
        tgt[-shift:, :] = np.nan
        return tgt

    # ---- IC helpers ----------------------------------------------------- #
    def _ic_ts(self, panel: np.ndarray) -> np.ndarray:
        if self._returns is None:
            raise RuntimeError("AlphaPool.set_target must be called before IC computation")
        return ic_series(panel, self._returns)

    def factor_rankic(self, panel: np.ndarray) -> float:
        ic = self._ic_ts(panel)
        return float(np.nanmean(ic)) if ic.size else 0.0

    def mutual_ic(self, panel: np.ndarray) -> float:
        """Max |Pearson corr| of the new factor's IC time-series with each member.

        A high value means the candidate predicts the *same* cross-sectional
        pattern as an existing member -> redundant.
        """
        if not self._panels:
            return 0.0
        new_ic = self._ic_ts(panel)
        best = 0.0
        for p in self._panels:
            c = _safe_pearson(new_ic, self._ic_ts(p))
            if c is not None:
                best = max(best, abs(c))
        return best

    # ---- admission ------------------------------------------------------ #
    def evaluate_admission(self, panel: np.ndarray) -> Tuple[bool, str, float]:
        if self._returns is None:
            return False, "no_target", 0.0
        mic = self.mutual_ic(panel)
        if mic > self.mutual_ic_max:
            return False, f"redundant(mutual_ic={mic:.3f})", mic
        return True, "ok", mic

    def admit(self, fid: str, panel, index=None, columns=None) -> Tuple[bool, str]:
        if self._returns is None:
            return False, "no_target"
        panel = _to_wide_array(panel, self._dates, self._assets)
        if len(self._panels) >= self.capacity:
            return self._admit_with_replacement(fid, panel)
        ok, reason, mic = self.evaluate_admission(panel)
        if not ok:
            self._admit_log.append(
                {"id": fid, "action": "rejected", "reason": reason, "mutual_ic": round(mic, 4)}
            )
            return False, reason
        self._ids.append(fid)
        self._panels.append(panel)
        self._weights.append(0.0)
        self._optimize_weights()
        self._admit_log.append(
            {
                "id": fid,
                "action": "admitted",
                "reason": reason,
                "mutual_ic": round(mic, 4),
                "rankic": round(self.factor_rankic(panel), 5),
            }
        )
        return True, reason

    def _admit_with_replacement(self, fid: str, panel: np.ndarray) -> Tuple[bool, str]:
        """Pool is full: replace the member whose swap most improves pooled IC."""
        base_ic = self._combined_rankic_from(self._panels, self._weights)
        best_idx, best_ic = None, base_ic
        for i in range(len(self._panels)):
            trial = list(self._panels)
            trial[i] = panel
            ic = self._combined_rankic_from(trial, self._weights)
            if ic > best_ic:
                best_ic, best_idx = ic, i
        if best_idx is None:
            self._admit_log.append(
                {"id": fid, "action": "rejected", "reason": "no_improvement",
                 "mutual_ic": round(self.mutual_ic(panel), 4)}
            )
            return False, "no_improvement"
        replaced = self._ids[best_idx]
        self._ids[best_idx] = fid
        self._panels[best_idx] = panel
        self._optimize_weights()
        self._admit_log.append(
            {
                "id": fid,
                "action": "replaced",
                "reason": f"replaced {replaced}",
                "mutual_ic": round(self.mutual_ic(panel), 4),
                "rankic": round(self.factor_rankic(panel), 5),
            }
        )
        return True, f"replaced {replaced}"

    # ---- weight optimization ------------------------------------------- #
    def _optimize_weights(self) -> None:
        if not self._panels:
            self._weights = []
            self._ridge_diag: Dict[str, Any] = {}
            return
        K = len(self._panels)
        target = self._target_for_shift(self.weight_fit_shift)
        X = np.column_stack([p.reshape(-1) for p in self._panels])  # (T*N, K)
        y = target.reshape(-1)
        mask = np.isfinite(X).all(axis=1) & np.isfinite(y)

        # M-02: raw panels span ~1e5 in scale, so a single lambda on X'X was
        # infinite shrinkage for small columns and none for large ones. Fit on
        # z-scores with an unpenalised intercept, then map back to raw space.
        fit = None
        if mask.sum() < self.min_obs or len(np.unique(y[mask])) < 2 or X[mask].shape[1] != K:
            w = np.ones(K) / K  # fallback: equal weights
            self._ridge_diag = {
                "method": "equal", "fallback": True,
                "reason": "insufficient_rows_or_constant_target",
                "n_obs": int(mask.sum()),
            }
        else:
            fit = fit_scale_aware_ridge(
                X[mask], y[mask], reg_lambda=self.reg_lambda,
                min_obs=self.min_obs, equal_weights=np.ones(K) / K,
            )
            w = np.asarray(fit.weights, dtype=float)
            self._ridge_diag = fit.diagnostics()
        if self.weight_norm == "l1":
            s = float(np.sum(np.abs(w)))
            w = w / s if s > 0 else np.ones(K) / K
        elif self.weight_norm == "l2":
            s = float(np.sqrt((w**2).sum()))
            w = w / s if s > 0 else np.ones(K) / K
        self._weights = [float(x) for x in w]

    # ---- combination ---------------------------------------------------- #
    def combined_panel(self) -> Optional[np.ndarray]:
        if not self._panels:
            return None
        out = np.zeros_like(self._panels[0])
        for p, w in zip(self._panels, self._weights):
            out = out + w * p
        return out

    def _combined_rankic_from(self, panels: List[np.ndarray], weights) -> float:
        out = np.zeros_like(panels[0])
        for i, p in enumerate(panels):
            out = out + weights[i] * p
        return self.factor_rankic(out)

    def pooled_rankic(self, eval_shift: int = 0) -> float:
        """Combined-factor IC against the (optionally shifted) forward return.

        ``eval_shift=0`` reports the native-horizon IC (directly comparable to
        single-factor RankIC); pass ``weight_fit_shift`` to evaluate on the same
        horizon the weights were fit on.
        """
        if self._returns is None:
            return 0.0
        cp = self.combined_panel()
        if cp is None:
            return 0.0
        tgt = self._target_for_shift(eval_shift) if eval_shift else self._returns
        ic = ic_series(cp, tgt)
        return float(np.nanmean(ic)) if ic.size else 0.0

    def pooled_hac_t(self, eval_shift: int = 0) -> float:
        if self._returns is None:
            return 0.0
        cp = self.combined_panel()
        if cp is None:
            return 0.0
        tgt = self._target_for_shift(eval_shift) if eval_shift else self._returns
        ic = ic_series(cp, tgt)
        return float(newey_west_tstat(ic))

    def report(self, eval_shift: int = 0) -> Dict[str, Any]:
        return {
            "capacity": self.capacity,
            "size": len(self._panels),
            "ids": list(self._ids),
            "weights": [round(w, 4) for w in self._weights],
            "pooled_rankic": round(self.pooled_rankic(eval_shift), 5),
            "pooled_hac_t": round(self.pooled_hac_t(eval_shift), 3),
            "admit_log": self._admit_log,
            "ridge": getattr(self, "_ridge_diag", {}),
        }
