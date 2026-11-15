"""Read-only baseline: does widening the cross-section destroy the signal, or
is the 18bp flat cost the thing that destroys it?

This is a *diagnostic*, not a promotion test. It deliberately uses naive
momentum factors (no mining, no LLM, no gates) so that any difference between
rows can only come from the universe definition and the cost model.

Everything is point-in-time:
  * factor  f[t] = close[t]/close[t-k]-1        (information known at t)
  * label   r[t] = close[t+1]/close[t]-1        (forward, same index as f)
  * liquidity tier at t uses trailing 90 bars of (close*volume) only
  * PIT pools re-rank the universe every bar; static pools are flagged as
    forward-looking (they use end-of-sample liquidity / full-window coverage)

The uniform-cost branch is cross-checked against the production implementation
``core.evaluation.portfolio_eval.long_short_returns``; the assertion fails
loudly if this script ever drifts from the real gate arithmetic.

Usage (from repo root):
    venv/Scripts/python.exe user_workspace/reports/pool_cost_baseline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.evaluation.portfolio_eval import long_short_returns  # noqa: E402

DATA = ROOT / "data" / "binance" / "futures"
CFG = ROOT / "user_workspace" / "configs" / "configAllUSDTFutures.json"
OUT = ROOT / "user_workspace" / "reports" / "pool_cost_baseline.json"

START = pd.Timestamp("2023-01-01")       # warmup for mom60 + rolling 90d liquidity
END = pd.Timestamp("2026-11-14")
EVAL_START = pd.Timestamp("2023-04-01")
EVAL_END = pd.Timestamp("2026-06-30")
ANN = 365.0                               # crypto 7x24, core/evaluation/annualisation.py
QUANTILE = 0.2
FLAT_COST = 0.0018                        # 18bp round trip: 2*(4bp fee + 5bp slip)
FEE_PER_SIDE = 0.0004
BASE_SLIP = 0.0005
SLIP_CAP = 0.0050                         # 50bp/side ceiling for illiquid names
SLIP_CAP_MILD = 0.0015                    # 15bp/side ceiling, sensitivity scenario
LOOKBACK = 90
MIN_QV_BARS = 30


def sym_of(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    syms = sorted({sym_of(p) for p in cfg["data_feeds"]["pairs"]})
    timeline = pd.date_range(START, END, freq="D")
    close, vol = {}, {}
    for s in syms:
        p = DATA / f"{s}-1d-futures.feather"
        if not p.exists():
            continue
        df = pd.read_feather(p, columns=["date", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates("date").set_index("date").sort_index()
        close[s] = df["close"].reindex(timeline)
        vol[s] = df["volume"].reindex(timeline)
    C = pd.DataFrame(close).astype(float)
    V = pd.DataFrame(vol).astype(float)
    QV = (C * V).replace([np.inf, -np.inf], np.nan)
    return C, V, QV, list(C.columns)


def net_stats(
    f: np.ndarray,
    r: np.ndarray,
    cost_mat: np.ndarray | float,
    hold: int,
) -> dict:
    """Mirror of long_short_returns with an optional per-asset/per-bar cost."""
    ranks = pd.DataFrame(f).rank(axis=1, pct=True).to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        is_long = ranks >= (1.0 - QUANTILE)
        is_short = ranks <= QUANTILE
    valid = np.isfinite(f) & np.isfinite(r)
    is_long &= valid
    is_short &= valid
    n_long = is_long.sum(axis=1).astype(float)
    n_short = is_short.sum(axis=1).astype(float)
    tradable = (n_long > 0) & (n_short > 0)

    with np.errstate(invalid="ignore", divide="ignore"):
        w_long = np.where(is_long, 0.5 / np.where(n_long > 0, n_long, np.nan)[:, None], 0.0)
        w_short = np.where(is_short, -0.5 / np.where(n_short > 0, n_short, np.nan)[:, None], 0.0)
    w = np.nan_to_num(w_long + w_short, nan=0.0)
    gross = np.abs(w).sum(axis=1)
    scale = np.divide(1.0, gross, out=np.zeros_like(gross), where=gross > 0)
    w = w * scale[:, None]

    n_rows = w.shape[0]
    idx = np.minimum(np.arange(n_rows) // hold * hold, n_rows - 1)
    w = w[idx]
    tradable = tradable[idx]

    ls = (w * np.nan_to_num(r, nan=0.0)).sum(axis=1)
    ls = np.where(tradable, ls, np.nan)
    finite = np.isfinite(ls)
    if finite.sum() < 2:
        return {"n_obs": 0}

    prev = np.vstack([np.zeros((1, w.shape[1])), w[:-1]])
    delta_w = np.abs(w - prev)             # (T, N): per-asset traded notional
    turnover = 0.5 * delta_w.sum(axis=1)

    if np.isscalar(cost_mat):
        cost = turnover * float(cost_mat)
    else:
        # cost_mat[t, i] is the round-trip rate for asset i on bar t. The cost
        # is charged on EACH asset's own traded notional, not on the row total.
        cost = 0.5 * (delta_w * cost_mat).sum(axis=1)

    net = ls[finite] - cost[finite]
    mu, sd = float(np.mean(net)), float(np.std(net, ddof=1))
    width = np.isfinite(f).sum(axis=1)[finite]
    return {
        "n_obs": int(finite.sum()),
        "median_width": float(np.median(width)),
        "gross_per_bar": round(float(np.mean(ls[finite])), 8),
        "turnover": round(float(np.mean(turnover[finite])), 5),
        "cost_per_bar": round(float(np.mean(cost[finite])), 8),
        "net_per_bar": round(float(mu), 8),
        "net_sharpe_ann": round(float(mu / sd * np.sqrt(ANN)), 4) if sd > 0 else 0.0,
    }


def main() -> None:
    C, V, QV, syms = load_panel()
    idx = (C.index >= EVAL_START) & (C.index <= EVAL_END)
    close = C[idx]
    qv = QV[idx]
    # trailing 90-bar median quote volume, PIT (uses t-89..t only)
    qv_roll = QV.rolling(LOOKBACK, min_periods=MIN_QV_BARS).median()[idx]
    fwd = (close.shift(-1) / close - 1.0).replace([np.inf, -np.inf], np.nan)

    # ---- static (forward-looking) pools, same convention as cross_section_audit.py
    end_qv = QV.loc[QV.index >= pd.Timestamp("2026-08-16")].median()
    static_rank = end_qv.reindex(syms).fillna(0.0).sort_values(ascending=False)
    static = {
        "static_top20": set(static_rank.index[:20]),
        "static_top60": set(static_rank.index[:60]),
        "static_all439": set(syms),
    }
    # ---- PIT pools: re-rank every bar by trailing liquidity
    qv_np = qv_roll.to_numpy(dtype=float)
    close_np = close.to_numpy(dtype=float)
    n_t, n_a = qv_np.shape
    pit = {k: np.zeros((n_t, n_a), dtype=bool) for k in
           ("pit_top20", "pit_top60", "pit_top150", "pit_all")}
    # cost rate matrix from the square-root impact law, anchored on the
    # top-20 median quote volume OF THAT BAR (no future information)
    cost_sqrt = np.full((n_t, n_a), FLAT_COST, dtype=float)
    cost_mild = np.full((n_t, n_a), FLAT_COST, dtype=float)
    for t in range(n_t):
        ok = np.isfinite(close_np[t]) & np.isfinite(qv_np[t]) & (qv_np[t] > 0)
        order = np.argsort(-np.where(ok, qv_np[t], -np.inf))
        order = [i for i in order if ok[i]]
        for k, name in ((20, "pit_top20"), (60, "pit_top60"), (150, "pit_top150")):
            pit[name][t, order[:k]] = True
        pit["pit_all"][t, ok] = True
        ref = float(np.median(qv_np[t, order[:20]])) if len(order) >= 20 else (
            float(np.median(qv_np[t, order])) if order else 1.0)
        if ref > 0 and order:
            ratio = np.sqrt(ref / np.maximum(qv_np[t], 1.0))
            for mat, cap in ((cost_sqrt, SLIP_CAP), (cost_mild, SLIP_CAP_MILD)):
                slip = np.clip(BASE_SLIP * ratio, BASE_SLIP, cap)
                mat[t] = 2.0 * (FEE_PER_SIDE + slip)
                mat[t, ~ok] = FLAT_COST

    factors = {
        "mom20": close / close.shift(20) - 1.0,
        "mom60": close / close.shift(60) - 1.0,
    }
    cost_models = {
        "uniform_18bp": FLAT_COST,
        "sqrt_law_mild": cost_mild,     # 15bp/side cap
        "sqrt_law_tiered": cost_sqrt,   # 50bp/side cap
    }

    results = []
    for pool_name, mask in {**pit, **static}.items():
        for fac_name, fac in factors.items():
            f_all = fac.to_numpy(dtype=float)
            if isinstance(mask, set):
                cols = np.array([s in mask for s in syms])
                f = np.where(cols[None, :], f_all, np.nan)
            else:
                f = np.where(mask, f_all, np.nan)
            r = fwd.to_numpy(dtype=float)
            for hold in (1, 5):
                for cost_name, cm in cost_models.items():
                    row = net_stats(f, r, cm, hold)
                    row.update({
                        "pool": pool_name,
                        "factor": fac_name,
                        "rebalance_bars": hold,
                        "cost_model": cost_name,
                    })
                    # cross-check the uniform branch against production code
                    if cost_name == "uniform_18bp":
                        prod = long_short_returns(
                            pd.DataFrame(f), pd.DataFrame(r),
                            quantile=QUANTILE, cost_rate=FLAT_COST,
                            annualisation=ANN, rebalance_bars=hold,
                        )
                        assert prod["n_obs"] == row["n_obs"], (pool_name, fac_name, hold)
                        assert abs(prod["net_mean_return"] - row["net_per_bar"]) < 1e-8, (
                            pool_name, fac_name, hold,
                            prod["net_mean_return"], row["net_per_bar"])
                    results.append(row)

    # Sanity guard: a per-bar cost above 2% means the cost matrix is being
    # broadcast/aggregated wrongly (this exact bug inflated it 439x once).
    for row in results:
        assert row["n_obs"] == 0 or row["cost_per_bar"] < 0.02, row

    payload = {
        "window": [str(EVAL_START.date()), str(EVAL_END.date())],
        "n_assets_declared": len(syms),
        "cost_assumption": {
            "uniform": "18bp round-trip (4bp fee + 5bp slip, both sides)",
            "tiered": ("round-trip = 2*(4bp fee + clip(5bp*sqrt(QV_top20_median(t)/QV_i(t)),"
                       " 5bp, cap)); cap = 15bp/side (mild) or 50bp/side (harsh)."
                       " trailing-90-bar QV, anchored per bar = PIT"),
            "caveat": ("the square-root impact law is a SENSITIVITY ASSUMPTION, not a "
                       "measured spread/impact. Only the direction of the comparison is "
                       "interpretable, not the absolute net Sharpe."),
        },
        "pool_definitions": {
            "pit_*": "re-ranked every bar on trailing-90d quote volume; no look-ahead",
            "static_*": "ranked on 2026-08..11 quote volume; FORWARD-LOOKING, kept only to reproduce the historical 20-vs-78-vs-439 comparison",
        },
        "known_bias": "all 439 pairs come from a 2026-11 still-listed snapshot; delisted contracts are absent (survivorship bias, not fixed here)",
        "results": results,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # compact console view
    print(f"window {EVAL_START.date()}..{EVAL_END.date()} | {len(syms)} declared assets")
    print(f"{'pool':<15}{'factor':<8}{'rb':>3} {'cost':<17}{'N':>6}{'width':>7}"
          f"{'turn':>8}{'net/bar':>12}{'netSR':>9}")
    for row in results:
        print(f"{row['pool']:<15}{row['factor']:<8}{row['rebalance_bars']:>3} "
              f"{row['cost_model']:<17}{row['n_obs']:>6}{row['median_width']:>7.0f}"
              f"{row['turnover']:>8.3f}{row['net_per_bar']:>12.6f}{row['net_sharpe_ann']:>9.3f}")
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
