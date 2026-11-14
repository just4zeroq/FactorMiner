"""Return-denominated portfolio evaluation for a factor panel.

Why this module exists
----------------------
The Phase-1 gate judged tradability with ``mean IC - cost_rate * turnover``.
That number is *dimensionless* (an IC), so a "cost" subtracted from it has no
economic meaning, and the P0 holdout experiment showed the practical
consequence: two factors with strongly significant OOS RankIC (t ≈ 4.3) still
lost 6.4% after costs as an actual long-short book.

This module answers the question the gate should have been asking: *if I trade
this factor, what does one period of the resulting book earn, net of the cost
of turning it over?* Everything here is expressed as a fraction of capital per
period, so the output is directly comparable to a backtest.

Conventions
-----------
* Weights are dollar-neutral with **gross exposure 1.0** (50% long / 50%
  short). A long-only book would be gross 1.0 too, so Sharpes are comparable.
* ``turnover`` is the conventional portfolio turnover ratio: ``0.5 * sum|dw|``.
  Fully replacing the book once = 1.0.
* ``cost_rate`` is the **round-trip** cost of one unit of turnover (fee +
  slippage + funding, both sides). ``cost = turnover * cost_rate``.
* The factor is oriented first (long the side with the positive in-sample IC),
  matching what the backtest does via ``infer_orientation``.
* ``rebalance_bars`` controls the trading frequency. Weights are formed only
  on the rebalance grid and then held, so both the realised return and the
  turnover reflect the horizon the book is actually traded at. Judging a
  5-day strategy on daily-rebalanced turnover charges it 5x the cost it would
  really pay, which is what made every realistic crypto factor look dead.

No look-ahead: weights at ``t`` use only the factor value at ``t``; the return
realised over ``t`` is the caller's forward return, which the evaluator already
shifts.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


DEFAULT_LS_CFG: Dict[str, Any] = {
    "quantile": 0.2,          # top/bottom 20% of the cross-section
    "cost_rate": 0.0018,      # round-trip cost per unit of turnover (18 bp)
    "annualisation": 252.0,   # periods per year, only used for the Sharpe
    "rebalance_bars": 1,      # hold the book this many bars between rebalances
    "temporal_lookback": 252, # trailing window for the single-asset percentile
}


def _as_frame(x: Any) -> Optional[pd.DataFrame]:
    if isinstance(x, pd.DataFrame):
        return x
    return None


def long_short_returns(
    factor: Any,
    returns: Any,
    quantile: float = 0.2,
    orientation: float = 1.0,
    cost_rate: float = 0.0,
    annualisation: float = 252.0,
    rebalance_bars: int = 1,
) -> Dict[str, float]:
    """Build the quantile long-short book and summarise its net performance.

    Returns a flat dict of floats (JSON/parquet friendly)::

        mean_return, std_return, sharpe, hit_rate, turnover,
        cost, cost_adj_return, n_obs, n_assets_long, gross

    ``mean_return`` is per period (e.g. per day), not annualised; ``sharpe`` is
    annualised with ``sqrt(annualisation)``. All values are 0.0 when the panel
    is too small to build a book, so callers can treat this as a total function.
    """
    empty = {
        "mean_return": 0.0,
        "std_return": 0.0,
        "sharpe": 0.0,
        "hit_rate": 0.0,
        "turnover": 0.0,
        "cost": 0.0,
        "cost_adj_return": 0.0,
        "n_obs": 0,
        "n_assets_long": 0,
        "gross": 1.0,
        "ls_t": 0.0,
    }
    f_df = _as_frame(factor)
    r_df = _as_frame(returns)
    if f_df is None or r_df is None or f_df.empty or r_df.empty:
        return empty

    r_df = r_df.reindex(index=f_df.index, columns=f_df.columns)
    f = f_df.to_numpy(dtype=float) * float(orientation)
    r = r_df.to_numpy(dtype=float)
    if f.size == 0:
        return empty

    q = float(quantile)
    if not 0.0 < q < 0.5:
        q = DEFAULT_LS_CFG["quantile"]

    # Cross-sectional percentile ranks, NaN-safe (pandas ranks drop NaN).
    ranks = pd.DataFrame(f).rank(axis=1, pct=True).to_numpy(dtype=float)

    with np.errstate(invalid="ignore"):
        # After orientation the signal is "higher = better", so the book is
        # long the top quantile and short the bottom quantile.
        is_long = ranks >= (1.0 - q)
        is_short = ranks <= q
    valid = np.isfinite(f) & np.isfinite(r)
    is_long &= valid
    is_short &= valid

    n_long = is_long.sum(axis=1).astype(float)
    n_short = is_short.sum(axis=1).astype(float)
    # Need both legs populated, otherwise the period is not tradable.
    tradable = (n_long > 0) & (n_short > 0)

    with np.errstate(invalid="ignore", divide="ignore"):
        w_long = np.where(
            is_long,
            0.5 / np.where(n_long > 0, n_long, np.nan)[:, None],
            0.0,
        )
        w_short = np.where(
            is_short,
            -0.5 / np.where(n_short > 0, n_short, np.nan)[:, None],
            0.0,
        )
    w = np.nan_to_num(w_long + w_short, nan=0.0)

    # Gross is 1.0 by construction; rescale defensively for rows where an asset
    # was dropped by the availability mask after the weights were formed.
    gross = np.abs(w).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(gross > 0, 1.0 / gross, 0.0)
    w = w * scale[:, None]

    # Hold the book between rebalances: form weights on the rebalance grid only
    # and carry them forward, so turnover reflects the trading frequency.
    hold = max(1, int(rebalance_bars))
    if hold > 1:
        n_rows = w.shape[0]
        idx = np.minimum(np.arange(n_rows) // hold * hold, n_rows - 1)
        w = w[idx]
        tradable = tradable[idx]

    r_filled = np.nan_to_num(r, nan=0.0)
    ls = (w * r_filled).sum(axis=1)
    ls = np.where(tradable, ls, np.nan)

    finite = np.isfinite(ls)
    if finite.sum() < 2:
        return empty
    ls = ls[finite]

    prev = np.vstack([np.zeros((1, w.shape[1])), w[:-1]])
    traded = np.abs(w - prev).sum(axis=1)
    turnover_series = 0.5 * traded
    turnover_series = turnover_series[finite]

    mean_ret = float(np.mean(ls))
    std_ret = float(np.std(ls, ddof=1)) if ls.size > 1 else 0.0
    turnover = float(np.mean(turnover_series))
    cost = turnover * float(cost_rate)
    sharpe = (
        (mean_ret / std_ret) * np.sqrt(float(annualisation)) if std_ret > 0 else 0.0
    )
    return {
        "mean_return": mean_ret,
        "std_return": std_ret,
        "sharpe": float(sharpe),
        "hit_rate": float(np.mean(ls > 0)),
        "turnover": turnover,
        "cost": float(cost),
        "cost_adj_return": float(mean_ret - cost),
        "n_obs": int(ls.size),
        "n_assets_long": int(np.median(n_long[finite])) if finite.any() else 0,
        "gross": 1.0,
        "ls_t": ls_t_stat(ls, lags=max(5, hold)),
    }


def timing_returns(
    factor: Any,
    returns: Any,
    lookback: int = 252,
    quantile: float = 0.3,
    orientation: float = 1.0,
    cost_rate: float = 0.0,
    annualisation: float = 252.0,
    rebalance_bars: int = 1,
) -> Dict[str, float]:
    """Evaluate a single-asset factor as a long/short *timing* strategy.

    A single asset has no cross-section, so a rank IC is not tradable. What is
    tradable is a position rule: go long when the factor sits in its own top
    quantile, short when it sits in the bottom quantile, using a trailing
    window so the decision only uses information available at the time.

    Returns the same keys as :func:`long_short_returns`, plus ``exposure``
    (share of bars with a non-zero position).
    """
    empty = {
        "mean_return": 0.0,
        "std_return": 0.0,
        "sharpe": 0.0,
        "hit_rate": 0.0,
        "turnover": 0.0,
        "cost": 0.0,
        "cost_adj_return": 0.0,
        "n_obs": 0,
        "n_assets_long": 1,
        "gross": 1.0,
        "ls_t": 0.0,
        "exposure": 0.0,
    }
    if not isinstance(factor, pd.Series) or not isinstance(returns, pd.Series):
        return empty

    r = returns.reindex(factor.index)
    df = pd.DataFrame({"f": factor.astype(float), "r": r.astype(float)}).dropna()
    if df.shape[0] < 30:
        return empty

    win = max(20, int(lookback))
    minp = max(20, win // 5)
    oriented = df["f"] * float(orientation)
    # Percentile of the current observation inside its own trailing window.
    pct = oriented.rolling(win, min_periods=minp).rank(pct=True)

    q = float(quantile)
    if not 0.0 < q < 0.5:
        q = 0.3
    pos = np.where(pct >= (1.0 - q), 1.0, np.where(pct <= q, -1.0, 0.0))
    pos = np.where(np.isfinite(pct.to_numpy()), pos, np.nan)
    pos = pd.Series(pos, index=df.index)

    hold = max(1, int(rebalance_bars))
    if hold > 1:
        # Only re-decide every `hold` bars; carry the previous position between.
        decided = pos.iloc[::hold].reindex(pos.index, method="ffill")
        pos = decided

    position = pos.to_numpy(dtype=float)
    ret = df["r"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        strat = position * ret
    finite = np.isfinite(strat)
    if finite.sum() < 10:
        return empty
    strat = strat[finite]
    position = position[finite]

    prev = np.concatenate([[0.0], position[:-1]])
    turnover_series = 0.5 * np.abs(position - prev)

    mean_ret = float(strat.mean())
    std_ret = float(strat.std(ddof=1)) if strat.size > 1 else 0.0
    turnover = float(turnover_series.mean())
    cost = turnover * float(cost_rate)
    sharpe = (
        (mean_ret / std_ret) * np.sqrt(float(annualisation)) if std_ret > 0 else 0.0
    )
    return {
        "mean_return": mean_ret,
        "std_return": std_ret,
        "sharpe": float(sharpe),
        "hit_rate": float((strat > 0).mean()),
        "turnover": turnover,
        "cost": float(cost),
        "cost_adj_return": float(mean_ret - cost),
        "n_obs": int(strat.size),
        "n_assets_long": 1,
        "gross": 1.0,
        "ls_t": ls_t_stat(strat, lags=max(5, hold)),
        "exposure": float(np.mean(position != 0)),
    }


def ls_t_stat(ls_series: np.ndarray, lags: int = 5) -> float:
    """Newey-West HAC t-stat of the mean of a return series (zero null)."""
    x = np.asarray(ls_series, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3:
        return 0.0
    mu = float(x.mean())
    e = x - mu
    var = float(np.dot(e, e)) / n
    for lag in range(1, min(lags, n - 1) + 1):
        w = 1.0 - lag / (lags + 1.0)
        cov = float(np.dot(e[lag:], e[:-lag])) / n
        var += 2.0 * w * cov
    if var <= 0:
        return 0.0
    se = np.sqrt(var / n)
    return float(mu / se) if se > 0 else 0.0
