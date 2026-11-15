"""Read-only mechanism comparison: AlphaPool vs a skfolio-style cross-sectional
factor model.

This is NOT skfolio (not installed here). It isolates the single mechanism that
separates the two:

  A  AlphaPool / core/miner/pool.py::_optimize_weights
     one pooled scale-aware ridge over ALL (T*N) training rows -> ONE fixed
     weight vector, reused unchanged on every out-of-sample date.

  B  skfolio / CharacteristicsFactorModel (simulated)
     a cross-sectional regression RE-ESTIMATED ON EVERY DATE using only the
     assets available that date; factor premia are averaged over trailing dates
     (strictly lagged, no look-ahead) and mapped back through that date's
     exposures -> time-varying weights, N(t)-aware by construction.

Both are evaluated with the IDENTICAL production long-short arithmetic
(net_stats / long_short_returns, quantile 0.2, 18bp, sqrt(365) annualisation).
Train split: first 60% of the evaluation window. Test: last 40%, untouched by A.

Usage (from repo root):
    venv/Scripts/python.exe user_workspace/reports/alphapool_vs_crosssection.py
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "user_workspace" / "reports"))

from core.evaluation.significance import ic_series               # noqa: E402
from core.miner.ridge_weights import fit_scale_aware_ridge       # noqa: E402
from pool_cost_baseline import (                                  # noqa: E402
    load_panel, net_stats, FLAT_COST, ANN, QUANTILE,
    EVAL_START, EVAL_END, LOOKBACK, MIN_QV_BARS,
)

OUT = ROOT / "user_workspace" / "reports" / "alphapool_vs_crosssection.json"
W = 60            # trailing window for factor premia (bars)
HOLD = 5
TRAIN_FRAC = 0.60


def cs_zscore(a: np.ndarray) -> np.ndarray:
    """Cross-sectional z-score per date, NaN-safe, then clip to +/-3."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rows (pre-listing)
        mu = np.nanmean(np.where(np.isfinite(a), a, np.nan), axis=1, keepdims=True)
        sd = np.nanstd(np.where(np.isfinite(a), a, np.nan), axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (a - mu) / np.where((sd > 0) & np.isfinite(sd), sd, np.nan)
    return np.clip(z, -3.0, 3.0)


def build_factors(close: pd.DataFrame) -> np.ndarray:
    px = close.to_numpy(dtype=float)
    ret1 = np.vstack([np.full((1, px.shape[1]), np.nan),
                      px[1:] / px[:-1] - 1.0])
    def mom(k):
        out = np.full_like(px, np.nan)
        out[k:] = px[k:] / px[:-k] - 1.0
        return out
    vol = pd.DataFrame(ret1).rolling(20, min_periods=10).std().to_numpy()
    raw = np.stack([mom(20), mom(60), -mom(5), vol], axis=2)  # (T, N, K)
    return np.stack([cs_zscore(raw[:, :, k]) for k in range(raw.shape[2])], axis=2)


def pooled_ridge(X: np.ndarray, r: np.ndarray, split: int) -> np.ndarray:
    """Reproduce AlphaPool._optimize_weights on the training slice."""
    K = X.shape[2]
    x = X[:split].reshape(-1, K)
    y = r[:split].reshape(-1)
    m = np.isfinite(x).all(axis=1) & np.isfinite(y)
    fit = fit_scale_aware_ridge(
        x[m], y[m], reg_lambda=5.0, min_obs=200,
        equal_weights=np.ones(K) / K,
    )
    w = np.asarray(fit.weights, dtype=float)
    s = float(np.sum(np.abs(w)))
    return (w / s if s > 0 else np.ones(K) / K), fit.diagnostics()


def daily_factor_returns(X: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Per-date cross-sectional OLS: b[t] = argmin ||r[t] - X[t] b||.

    Only assets available on date t participate (N(t)-aware by construction),
    matching skfolio's ``min_regression_assets`` guard. b[t] uses r[t], so it
    may only enter predictions for dates > t.
    """
    n_t, n_a, K = X.shape
    f_ret = np.full((n_t, K), np.nan)
    for t in range(n_t):
        valid = np.isfinite(X[t]).all(axis=1) & np.isfinite(r[t])
        if valid.sum() < max(2 * K, 30):     # skfolio min_regression_assets default
            continue
        A = np.column_stack([np.ones(int(valid.sum())), X[t][valid]])
        b, *_ = np.linalg.lstsq(A, r[t][valid], rcond=None)
        f_ret[t] = b[1:]
    return f_ret


def premia_panel(X: np.ndarray, f_ret: np.ndarray, split: int, mode: str) -> np.ndarray:
    """Map expected factor premia back through each date's exposures.

    ``daily``       trailing-W mean of strictly prior b[t] (bare re-estimation)
    ``half_shrunk`` 50/50 blend with the training-period global prior
    ``fixed_prior`` training-period prior only (time-constant premia)

    skfolio exposes this through ``factor_prior_estimator`` / shrinkage knobs
    (ShrunkMu, EWMu, spanned_alpha_shrinkage). Simulating three strengths shows
    how much of the result is the re-estimation itself vs the shrinkage.
    """
    n_t, n_a, K = X.shape
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        global_prior = np.nanmean(f_ret[:split], axis=0)
    global_prior = np.nan_to_num(global_prior, nan=0.0)
    out = np.full((n_t, n_a), np.nan)
    for t in range(n_t):
        hist = f_ret[max(0, t - W):t]        # strictly prior -> no look-ahead
        if np.isfinite(hist).any():
            trailing = np.nan_to_num(np.nanmean(hist, axis=0), nan=0.0)
        else:
            trailing = np.zeros(K)
        if mode == "daily":
            premia = trailing
        elif mode == "half_shrunk":
            premia = 0.5 * trailing + 0.5 * global_prior
        elif mode == "fixed_prior":
            premia = global_prior
        else:
            raise ValueError(mode)
        out[t] = X[t] @ premia
    return out


def pool_masks(qv_roll: np.ndarray, close_np: np.ndarray, n_t: int, n_a: int):
    pit = {"pit_top60": np.zeros((n_t, n_a), bool),
           "pit_all": np.zeros((n_t, n_a), bool)}
    widths = {}
    for name in pit:
        for t in range(n_t):
            ok = np.isfinite(close_np[t]) & np.isfinite(qv_roll[t]) & (qv_roll[t] > 0)
            if name == "pit_all":
                pit[name][t, ok] = True
            else:
                order = [i for i in np.argsort(-np.where(ok, qv_roll[t], -np.inf)) if ok[i]]
                pit[name][t, order[:60]] = True
        widths[name] = [int(pit[name][t].sum()) for t in range(n_t)]
    return pit, widths


def main() -> None:
    C, V, QV, syms = load_panel()
    idx = (C.index >= EVAL_START) & (C.index <= EVAL_END)
    close = C[idx]
    qv_roll = QV.rolling(LOOKBACK, min_periods=MIN_QV_BARS).median()[idx].to_numpy(float)
    close_np = close.to_numpy(float)
    r = (close.shift(-1) / close - 1.0).replace([np.inf, -np.inf], np.nan).to_numpy(float)
    X = build_factors(close)
    n_t, n_a, K = X.shape
    split = int(n_t * TRAIN_FRAC)
    sl = slice(split, n_t)

    w_A, ridge_diag = pooled_ridge(X, r, split)
    f_A = X @ w_A
    f_ret = daily_factor_returns(X, r)
    methods = {"A_alphapool_fixed_ridge": f_A}
    for mode, label in (("daily", "B_cs_bare_daily"),
                        ("half_shrunk", "C_cs_half_shrunk"),
                        ("fixed_prior", "D_cs_fixed_prior")):
        methods[label] = premia_panel(X, f_ret, split, mode)

    # Diagnostics: how noisy are the per-date factor premia?
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        fr_diag = {
            "per_date_factor_return_std": [round(float(x), 8) for x in np.nanstd(f_ret[:split], axis=0)],
            "per_date_factor_return_mean": [round(float(x), 8) for x in np.nanmean(f_ret[:split], axis=0)],
            "lag1_autocorr": [
                round(float(np.corrcoef(f_ret[1:split, k][m], f_ret[:split - 1, k][m])[0, 1]), 4)
                if (m := np.isfinite(f_ret[1:split, k]) & np.isfinite(f_ret[:split - 1, k])).sum() > 30
                else None
                for k in range(K)
            ],
        }

    pits, widths = pool_masks(qv_roll, close_np, n_t, n_a)
    results = []
    for pname, mask in pits.items():
        for mname, f in methods.items():
            fp = np.where(mask, f, np.nan)[sl]
            rp = r[sl]
            row = net_stats(fp, rp, FLAT_COST, HOLD)
            ic = ic_series(fp, rp)
            row.update({
                "pool": pname, "method": mname,
                "rankic": round(float(np.nanmean(ic)), 5),
                "n_test_bars": int(sl.stop - sl.start),
                "n_train_bars": int(split),
                "assets_per_bar_test": {
                    "min": int(np.min(widths[pname][sl])),
                    "median": int(np.median(widths[pname][sl])),
                    "max": int(np.max(widths[pname][sl])),
                },
            })
            results.append(row)

    payload = {
        "window": [str(EVAL_START.date()), str(EVAL_END.date())],
        "train_fraction": TRAIN_FRAC,
        "hold_bars": HOLD,
        "cost_model": "uniform 18bp round-trip",
        "factors": ["mom20", "mom60", "-mom5(reversal)", "realized_vol20"],
        "factor_preprocessing": "per-date cross-sectional z-score clipped to +/-3 (PIT)",
        "method_A": {
            "desc": "AlphaPool style: one scale-aware ridge on pooled (T*N) training rows, weights frozen OOS",
            "weights_l1_normalized": [round(float(x), 5) for x in w_A],
            "ridge_diagnostics": ridge_diag,
        },
        "method_BCD": {
            "desc": ("Simulated skfolio CharacteristicsFactorModel core: per-date cross-sectional "
                     "OLS on assets available that date, then premia mapped back through that "
                     "date's exposures. B = trailing-60 mean of strictly prior factor returns; "
                     "C = 50/50 shrink toward training-period prior; D = training-period prior only."),
            "caveat": ("Mechanism simulation, NOT skfolio (not installed). Omits AssetPanel "
                       "active/estimation masks, alpha estimator, factor covariance construction, "
                       "market-cap weighting, exposure winsorisation and the constrained optimizer. "
                       "skfolio's factor_prior_estimator / shrinkage knobs correspond to C/D."),
            "min_regression_assets": max(2 * K, 30),
            "premia_window": W,
            "factor_return_diagnostics": fr_diag,
        },
        "gate_warning": ("Neither number is a promotion result: no BH-FDR, no DSR, no holdout, "
                         "no funding cost. Only the A-vs-B difference is interpretable."),
        "results": results,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"train {split} bars / test {n_t - split} bars | hold={HOLD} | 18bp | ann={ANN}")
    print(f"fixed ridge weights (l1-normed): {np.round(w_A, 4).tolist()}")
    print(f"factor-return lag1 autocorr: {fr_diag['lag1_autocorr']}"
          f"  std: {fr_diag['per_date_factor_return_std']}")
    print(f"{'pool':<12}{'method':<26}{'width':<16}{'RankIC':>9}{'turn':>8}{'net/bar':>12}{'netSR':>9}")
    for x in results:
        w = x["assets_per_bar_test"]
        print(f"{x['pool']:<12}{x['method']:<26}"
              f"{str((w['min'], w['median'], w['max'])):<16}"
              f"{x['rankic']:>9.5f}{x['turnover']:>8.3f}"
              f"{x['net_per_bar']:>12.6f}{x['net_sharpe_ann']:>9.3f}")
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
