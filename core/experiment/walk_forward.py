"""P3: time isolation with *per-fold refitting*.

The survey found two failure modes in other frameworks:

* no purge / embargo at all, so a label computed at the train/validation
  boundary leaks into the next split;
* `AlphaMaster`'s "walk-forward": compute factor values on the FULL sample
  once, then slice the scores into folds. No refit happens, so every fold is
  evaluated with weights that already saw its validation data.

Here each fold fits its own ridge weights on that fold's training slice only
(including the per-fold normalisation statistics), then evaluates on a
validation slice separated by ``horizon_bars`` purge + ``embargo_bars``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.config.defaults import DEFAULT_COST_RATE

DEFAULT_WF_CFG: Dict[str, Any] = {
    "n_folds": 5,
    "embargo_bars": 5,
    "min_train_frac": 0.40,
    "min_train_bars": 60,
    "ridge_alpha": 5.0,
    # Round-trip cost per unit of turnover — must match the gate and backtest
    # cost basis (review A2 / F2). Single source: core.config.defaults.
    "cost_rate": DEFAULT_COST_RATE,
    "quantile": 0.20,
}


def build_folds(
    index: Sequence[Any],
    *,
    n_folds: int = 5,
    horizon_bars: int = 1,
    embargo_bars: int = 5,
    min_train_frac: float = 0.40,
    min_train_bars: int = 60,
) -> List[Dict[str, Any]]:
    """Expanding-window folds separated by purge + embargo.

    ``train_end`` is pulled back by ``horizon_bars + 1`` so the last training
    label (t+horizon) cannot land inside the validation window; the validation
    start is pushed forward by ``embargo_bars`` for serial-correlation decay.
    """
    n = len(index)
    min_train = max(int(n * min_train_frac), min_train_bars, 2)
    if n <= min_train + horizon_bars + embargo_bars + 2:
        return []
    span = n - min_train
    fold_len = max(span // max(int(n_folds), 1), 1)
    folds: List[Dict[str, Any]] = []
    for k in range(int(n_folds)):
        val_start = min_train + k * fold_len
        val_end = min(val_start + fold_len, n) if k < int(n_folds) - 1 else n
        train_end = val_start - int(horizon_bars) - 1
        embargoed_start = val_start + int(embargo_bars)
        if train_end < min_train_bars // 2 or embargoed_start >= val_end - 1:
            continue
        folds.append({
            "fold": k + 1,
            "train_start": str(index[0]),
            "train_end": str(index[train_end]),
            "val_start": str(index[embargoed_start]),
            "val_end": str(index[val_end - 1]),
            "train_slice": (0, train_end + 1),
            "val_slice": (embargoed_start, val_end),
            "purge_bars": int(horizon_bars),
            "embargo_bars": int(embargo_bars),
            "gap_bars": embargoed_start - train_end - 1,
        })
    return folds


def load_snapshots(
    db_root: str,
    factor_ids: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
    """Read the persisted long-format factor snapshots into aligned wide panels."""
    values_dir = Path(db_root) / "values"
    files = sorted(values_dir.glob("*.parquet"))
    if factor_ids:
        wanted = set(factor_ids)
        files = [f for f in files if f.stem in wanted]
    panels: Dict[str, pd.DataFrame] = {}
    returns: Optional[pd.DataFrame] = None
    for f in files:
        try:
            raw = pd.read_parquet(f)
        except Exception:
            continue
        if not {"timestamp", "asset", "factor"}.issubset(raw.columns):
            continue
        panel = raw.pivot(index="timestamp", columns="asset", values="factor")
        panels[f.stem] = panel.sort_index()
        if returns is None and "forward_return" in raw.columns:
            returns = raw.pivot(
                index="timestamp", columns="asset", values="forward_return"
            ).sort_index()
    if returns is None:
        returns = pd.DataFrame()
    return panels, returns


def _align(
    panels: Dict[str, pd.DataFrame], returns: pd.DataFrame
) -> Tuple[np.ndarray, np.ndarray, pd.Index]:
    if not panels or returns.empty:
        raise ValueError("No factor panels or forward returns available")
    common_idx = returns.index
    for p in panels.values():
        common_idx = common_idx.intersection(p.index)
    common_cols = returns.columns
    for p in panels.values():
        common_cols = common_cols.intersection(p.columns)
    common_idx = pd.Index(sorted(common_idx))
    common_cols = pd.Index(sorted(common_cols))
    if len(common_idx) < 10 or len(common_cols) < 2:
        raise ValueError("Aligned panel too small for walk-forward validation")
    f = np.stack([
        p.loc[common_idx, common_cols].to_numpy(dtype=float) for p in panels.values()
    ])
    r = returns.loc[common_idx, common_cols].to_numpy(dtype=float)
    return f, r, common_idx


def _standardise(train: np.ndarray, other: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Fit mean/std on TRAIN only; apply the same transform to the other slice."""
    mean = np.nanmean(train, axis=(1, 2), keepdims=True)
    std = np.nanstd(train, axis=(1, 2), keepdims=True)
    std = np.where(std > 1e-12, std, 1.0)
    return (
        np.nan_to_num((train - mean) / std, nan=0.0, posinf=0.0, neginf=0.0),
        np.nan_to_num((other - mean) / std, nan=0.0, posinf=0.0, neginf=0.0),
    )


def _fit_ridge(z_train: np.ndarray, r_train: np.ndarray, alpha: float) -> np.ndarray:
    """Per-fold weights: least squares from standardised factors to forward returns."""
    k = z_train.shape[0]
    x = z_train.reshape(k, -1).T
    y = np.nan_to_num(r_train.reshape(-1), nan=0.0)
    ok = np.isfinite(x).all(axis=1) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.shape[0] < k + 1:
        return np.full(k, 1.0 / max(k, 1))
    gram = x.T @ x + float(alpha) * np.eye(k)
    try:
        return np.linalg.solve(gram, x.T @ y)
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(gram, x.T @ y, rcond=None)[0]


def _rank_ic(combined: np.ndarray, r: np.ndarray) -> float:
    vals = []
    for row_c, row_r in zip(combined, r):
        ok = np.isfinite(row_c) & np.isfinite(row_r)
        if ok.sum() < 3:
            continue
        a, b = row_c[ok], row_r[ok]
        ra = pd.Series(a).rank().to_numpy()
        rb = pd.Series(b).rank().to_numpy()
        if ra.std() > 1e-12 and rb.std() > 1e-12:
            vals.append(float(np.corrcoef(ra, rb)[0, 1]))
    return float(np.nanmean(vals)) if vals else 0.0


def _net_return(combined: np.ndarray, r: np.ndarray, quantile: float,
                cost_rate: float) -> Tuple[float, float]:
    """Long top-q / short bottom-q each bar, net of turnover costs."""
    n_assets = combined.shape[1]
    k = max(int(n_assets * quantile), 1)
    gross, turnover, prev = [], [], None
    for row_c, row_r in zip(combined, r):
        ok = np.isfinite(row_c) & np.isfinite(row_r)
        if ok.sum() < 2 * k:
            continue
        # Rank ONLY among valid assets. Using `np.where(ok, row_c, -np.inf)`
        # pushed invalid entries to the front of the ascending sort, so when
        # there were >= k invalid columns the short leg could pick all-NaN
        # positions and the whole bar's gross return was silently dropped by
        # np.nanmean (review F4/C2). Selecting valid indices first closes that.
        valid = np.where(ok)[0]
        order = valid[np.argsort(row_c[valid], kind="stable")]
        longs, shorts = set(order[-k:].tolist()), set(order[:k].tolist())
        gross.append(float(np.nanmean(row_r[list(longs)]) - np.nanmean(row_r[list(shorts)])))
        if prev is not None:
            changed = len(prev[0] ^ longs) + len(prev[1] ^ shorts)
            turnover.append(changed / (2 * k))
        prev = (longs, shorts)
    if not gross:
        return 0.0, 0.0
    g = float(np.nanmean(gross))
    t = float(np.nanmean(turnover)) if turnover else 0.0
    return g - float(cost_rate) * t, t


def walk_forward_validate(
    panels: Dict[str, pd.DataFrame],
    returns: pd.DataFrame,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Expanding-window validation with an independent refit on every fold."""
    wf_overrides = ((config or {}).get("experiment", {}) or {}).get("walk_forward", {}) or {}
    cfg = {**DEFAULT_WF_CFG, **wf_overrides}
    # X-04: the walk-forward book must charge exactly the same round-trip cost
    # as the promotion gate. Default it from the canonical cost identity rather
    # than from a second constant; an explicit walk_forward.cost_rate still wins.
    if "cost_rate" not in wf_overrides:
        from core.experiment.costs import resolve_cost_params
        cfg["cost_rate"] = float(
            resolve_cost_params(config or {})["gate_cost_rate"]
        )
    horizon = int(((config or {}).get("target") or {}).get("horizon_bars", 1) or 1)
    f, r, index = _align(panels, returns)
    folds = build_folds(
        index,
        n_folds=int(cfg["n_folds"]),
        horizon_bars=horizon,
        embargo_bars=int(cfg["embargo_bars"]),
        min_train_frac=float(cfg["min_train_frac"]),
        min_train_bars=int(cfg["min_train_bars"]),
    )
    results: List[Dict[str, Any]] = []
    for fold in folds:
        t0, t1 = fold["train_slice"]
        v0, v1 = fold["val_slice"]
        z_train, z_val = _standardise(f[:, t0:t1, :], f[:, v0:v1, :])
        weights = _fit_ridge(z_train, r[t0:t1], cfg["ridge_alpha"])
        combined = np.tensordot(weights, z_val, axes=(0, 0))
        ric = _rank_ic(combined, r[v0:v1])
        net, turnover = _net_return(
            combined, r[v0:v1], float(cfg["quantile"]), float(cfg["cost_rate"])
        )
        results.append({
            **fold,
            "weights": [round(float(w), 6) for w in weights],
            "rank_ic": round(ric, 6),
            "net_return": round(net, 6),
            "turnover": round(turnover, 6),
        })
    ics = [x["rank_ic"] for x in results]
    nets = [x["net_return"] for x in results]
    return {
        # X-07: zero folds used to be reported as a clean green validation with
        # mean RankIC 0.0. Surface it as an explicit failure so the pipeline
        # cannot record ``validated`` on an experiment that validated nothing.
        "ok": bool(results),
        "n_folds": len(results),
        "refit_per_fold": True,
        "purge_bars": horizon,
        "embargo_bars": int(cfg["embargo_bars"]),
        "factor_ids": list(panels.keys()),
        "n_bars": len(index),
        "n_assets": r.shape[1],
        "folds": results,
        "mean_rank_ic": round(float(np.mean(ics)), 6) if ics else 0.0,
        "min_rank_ic": round(float(np.min(ics)), 6) if ics else 0.0,
        "mean_net_return": round(float(np.mean(nets)), 6) if nets else 0.0,
        "positive_folds": sum(1 for x in nets if x > 0),
        "all_folds_positive": bool(nets) and all(x > 0 for x in nets),
        "direction_consistent": bool(ics) and (all(x >= 0 for x in ics) or all(x <= 0 for x in ics)),
        "weights_differ_across_folds": (
            len({tuple(x["weights"]) for x in results}) > 1 if results else False
        ),
    }


def run_walk_forward(
    db_root: str,
    factor_ids: Optional[Sequence[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience entry used by the pipeline: read snapshots then validate."""
    panels, returns = load_snapshots(db_root, factor_ids)
    return walk_forward_validate(panels, returns, config)
