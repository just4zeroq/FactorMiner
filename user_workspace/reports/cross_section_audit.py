"""Read-only feasibility audit for a few-hundred-coin cross-sectional panel.

Answers, from the local feather files only (no downloads, no writes to data/):

1. How many of the configured pairs actually have a 1d OHLCV file?
2. How dense is the (T, N) panel over the mining window?  New listings create
   long NaN prefixes; ``data_quality.min_coverage`` silently drops those assets.
3. Is liquidity concentrated enough that a single 18bp cost is indefensible?

Usage (from repo root):
    venv/Scripts/python.exe user_workspace/reports/cross_section_audit.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "binance" / "futures"
CFG = ROOT / "user_workspace" / "configs" / "configAllUSDTFutures.json"
OUT = ROOT / "user_workspace" / "reports" / "cross_section_audit.json"

MINE_START = pd.Timestamp("2023-04-01")
MINE_END = pd.Timestamp("2026-06-30")
LIQ_START = pd.Timestamp("2026-08-16")  # trailing ~90 bars ending 2026-11-14


def file_for(pair: str) -> Path:
    """'0G/USDT:USDT' -> '0G_USDT_USDT-1d-futures.feather'."""
    safe = pair.replace("/", "_").replace(":", "_")
    return DATA / f"{safe}-1d-futures.feather"


def main() -> None:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    feeds = cfg["data_feeds"]
    pairs = feeds["pairs"]
    min_cov = cfg.get("data_quality", {}).get("min_coverage", 0.9)

    declared_syms = {file_for(p).name.split("-1d-futures.feather")[0] for p in pairs}
    files = sorted(DATA.glob("*-1d-futures.feather"))
    timeline = pd.date_range(MINE_START, MINE_END, freq="D")
    n_bars = len(timeline)
    rows = []
    daily_counts = {}
    for p in files:
        sym = p.name.split("-1d-futures.feather")[0]
        df = pd.read_feather(p, columns=["date", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates("date").set_index("date").sort_index()
        win = df.loc[(df.index >= MINE_START) & (df.index <= MINE_END)]
        valid = win["close"].notna() & np.isfinite(win["close"])
        if sym in declared_syms:
            for d in win.index[valid]:
                daily_counts[d] = daily_counts.get(d, 0) + 1
        liq = df.loc[df.index >= LIQ_START]
        quote = (liq["close"] * liq["volume"]).replace([np.inf, -np.inf], np.nan).dropna()
        ret = df["close"].pct_change().abs().replace([np.inf, -np.inf], np.nan).dropna()
        rows.append({
            "symbol": sym,
            "bars_total": int(len(df)),
            "first": str(df.index.min().date()) if len(df) else None,
            "last": str(df.index.max().date()) if len(df) else None,
            "bars_in_mine_window": int(len(win)),
            # Denominator is the FIXED window length, not len(win): a coin that
            # listed in 2025 must show coverage ~0.3, not 1.0. Using the file's
            # own length silently hides pre-listing absence (survivorship bias).
            "coverage": round(float(valid.sum()) / n_bars, 4),
            "quote_vol_median_90d": float(quote.median()) if len(quote) else 0.0,
            "abs_ret_median": float(ret.median()) if len(ret) else None,
            "zero_volume_ratio": float((liq["volume"] <= 0).mean()) if len(liq) else None,
        })

    by_sym = {r["symbol"]: r for r in rows}
    missing = [p for p in pairs if file_for(p).name.split("-1d-futures.feather")[0] not in by_sym]
    declared = [r for r in rows if r["symbol"] in
                {file_for(p).name.split("-1d-futures.feather")[0] for p in pairs}]

    counts = np.array([daily_counts.get(d, 0) for d in timeline])
    by_year = {}
    for y in sorted({d.year for d in timeline}):
        mask = np.array([d.year == y for d in timeline])
        by_year[str(y)] = {
            "mean_assets": round(float(counts[mask].mean()), 1),
            "p10_assets": int(np.percentile(counts[mask], 10)),
            "median_assets": int(np.median(counts[mask])),
            "max_assets": int(counts[mask].max()),
        }

    # Liquidity tiers, ranked by trailing 90d quote volume among declared pairs.
    ranked = sorted(declared, key=lambda r: r["quote_vol_median_90d"], reverse=True)
    total_qv = sum(r["quote_vol_median_90d"] for r in ranked) or 1.0
    tiers = {}
    for name, lo, hi in [("top20", 0, 20), ("21-60", 20, 60),
                         ("61-150", 60, 150), ("151+", 150, len(ranked))]:
        seg = ranked[lo:hi]
        if not seg:
            continue
        tiers[name] = {
            "n": len(seg),
            "quote_vol_share": round(sum(r["quote_vol_median_90d"] for r in seg) / total_qv, 4),
            "median_quote_vol": round(float(np.median([r["quote_vol_median_90d"] for r in seg])), 0),
            "median_abs_daily_ret": round(float(np.median([r["abs_ret_median"] for r in seg
                                                            if r["abs_ret_median"] is not None])), 5),
            "median_zero_volume_ratio": round(float(np.median([r["zero_volume_ratio"] for r in seg
                                                               if r["zero_volume_ratio"] is not None])), 4),
        }

    eligible = [r for r in declared if r["coverage"] >= min_cov]
    result = {
        "config": str(CFG.relative_to(ROOT)),
        "window": [str(MINE_START.date()), str(MINE_END.date())],
        "min_coverage": min_cov,
        "pairs_declared": len(pairs),
        "files_on_disk": len(files),
        "pairs_missing_file": len(missing),
        "missing_sample": [pair for pair in missing[:10]],
        "assets_passing_coverage": len(eligible),
        "assets_failing_coverage": len(declared) - len(eligible),
        "coverage_distribution": {
            "p10": round(float(np.percentile([r["coverage"] for r in declared], 10)), 3),
            "p25": round(float(np.percentile([r["coverage"] for r in declared], 25)), 3),
            "median": round(float(np.median([r["coverage"] for r in declared])), 3),
            "p75": round(float(np.percentile([r["coverage"] for r in declared], 75)), 3),
        } if declared else {},
        "daily_available_assets_by_year": by_year,
        "effective_cells": int(counts.sum()),
        "full_grid_cells": int(len(timeline) * len(declared)),
        "panel_density": round(float(counts.sum()) / (len(timeline) * len(declared)), 4)
                         if declared else 0.0,
        "liquidity_tiers": tiers,
        "cost_model": "single global 18bp round-trip (commission 4bp + slippage 5bp, both sides)",
        "per_asset": rows,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"declared pairs        : {result['pairs_declared']}")
    print(f"1d files on disk      : {result['files_on_disk']}")
    print(f"missing files         : {result['pairs_missing_file']}")
    print(f"pass min_coverage     : {result['assets_passing_coverage']} / {len(declared)}")
    print(f"coverage quartiles    : {result['coverage_distribution']}")
    print(f"panel density         : {result['panel_density']:.1%} "
          f"({result['effective_cells']:,} / {result['full_grid_cells']:,} cells)")
    for y, v in by_year.items():
        print(f"  {y}: mean {v['mean_assets']}, median {v['median_assets']}, "
              f"p10 {v['p10_assets']}, max {v['max_assets']}")
    print("liquidity tiers (trailing 90d):")
    for k, v in tiers.items():
        print(f"  {k:8s} n={v['n']:3d} qv_share={v['quote_vol_share']:.1%} "
              f"med_qv={v['median_quote_vol']:.0f} "
              f"med|ret|={v['median_abs_daily_ret']} "
              f"zero_vol={v['median_zero_volume_ratio']}")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
