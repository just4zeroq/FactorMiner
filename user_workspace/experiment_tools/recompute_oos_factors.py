"""Recompute factor values on a strictly held-out window (true out-of-sample).

For each factor id this loads the persisted JSON-AST logic via ``FactorCompiler``,
recomputes the factor on the 20-asset universe over [oos_start, oos_end] using the
local 1d feather market data, and writes a fresh snapshot into a *separate*
``db_root`` (default ``factor_db_oos``). The original ``factor_db`` is left
untouched, so a subsequent backtest run pointed at the OOS root is a genuine
out-of-sample test (no in-sample leakage, including in the signal orientation
which is re-inferred from the OOS snapshot itself).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import List, Sequence

import pandas as pd

# Make the project root importable when run as a standalone script.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.data_feed.naming import data_path
from core.execution.compiler import FactorCompiler
from core.storage.factor_storage import LocalFactorStorage
from core.utils.dynamic_loader import load_user_modules

# Register user-defined custom operators (e.g. ts_volatility_20 used by MyCustomGP
# factors) so the AST evaluator can resolve them during recomputation.
load_user_modules()

INSPECTED = [
    "fac_c25e28c3", "fac_8af952ab", "fac_b66d17ce",
    "fac_099a7eef", "fac_f20a1349", "fac_2017455e",
]

EXCHANGE = "binance"
TIMEFRAME = "1d"
TRADE_TYPE = "futures"
DATA_ROOT = "data"
# Warm-up so rolling operators (windows up to ~60) have enough history before the
# OOS window begins; this look-back is NOT part of the evaluated signal window.
WARMUP_START = "2024-07-01"


def load_asset_prices(asset: str, start: str, end: str) -> pd.DataFrame | None:
    path = data_path(DATA_ROOT, EXCHANGE, asset, TIMEFRAME, TRADE_TYPE)
    if not os.path.exists(path):
        return None
    df = pd.read_feather(path)
    df = df.rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[(df["timestamp"] >= pd.Timestamp(start)) & (df["timestamp"] <= pd.Timestamp(end))]
    return df.reset_index(drop=True)


def recompute(
    factor_ids: Sequence[str],
    oos_start: str,
    oos_end: str,
    db_root: str = "factor_db_oos",
    src_root: str = "factor_db",
    warmup_start: str = WARMUP_START,
) -> None:
    src = LocalFactorStorage(src_root)
    compiler = FactorCompiler(src)
    val_dir = os.path.join(db_root, "values")
    meta_dir = os.path.join(db_root, "metadata")
    os.makedirs(val_dir, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)

    # Asset universe comes from an existing in-sample snapshot of any requested
    # factor (same universe as the run that produced it).
    sample = None
    for fid in factor_ids:
        try:
            sample = src.load_factor_values(fid)
            break
        except Exception:  # noqa: BLE001 - try the next id
            continue
    if sample is None or "asset" not in sample.columns:
        raise SystemExit(
            f"cannot infer asset universe: no values found for {list(factor_ids)} under {src_root}"
        )
    assets = sorted(sample["asset"].unique())
    print(f"asset universe ({len(assets)} assets): {assets[:5]} ...")

    prices: dict[str, pd.DataFrame] = {}
    for a in assets:
        d = load_asset_prices(a, warmup_start, oos_end)
        if d is None or d.empty:
            print(f"  skip {a}: no local data")
            continue
        prices[a] = d
    print(f"loaded prices for {len(prices)} assets (warm-up {warmup_start} -> {oos_end})")

    for fid in factor_ids:
        try:
            compute = compiler.compile_for_live_trading(fid)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] {fid}: compile failed: {exc}")
            continue

        frames: List[pd.DataFrame] = []
        for a in assets:
            d = prices.get(a)
            if d is None:
                continue
            try:
                fv = compute(d.copy())
            except Exception as exc:  # noqa: BLE001
                print(f"  [WARN] {fid} {a}: compute failed: {exc}")
                continue
            if fv is None:
                continue
            if not isinstance(fv, pd.Series):
                fv = pd.Series(fv)
            fv = fv.reset_index(drop=True)
            s = pd.DataFrame({"timestamp": d["timestamp"].values, "factor": fv.values})
            s["asset"] = a
            close = d["close"].reset_index(drop=True)
            s["forward_return"] = close.shift(-1) / close - 1.0
            frames.append(s)

        if not frames:
            print(f"[WARN] {fid}: no values produced")
            continue

        out = pd.concat(frames, ignore_index=True)
        out = out[(out["timestamp"] >= pd.Timestamp(oos_start)) &
                  (out["timestamp"] <= pd.Timestamp(oos_end))]
        out = out.dropna(subset=["factor", "forward_return"])
        out = out.sort_values(["timestamp", "asset"]).reset_index(drop=True)
        out.to_parquet(os.path.join(val_dir, f"{fid}.parquet"), index=False)

        meta_src = os.path.join(src_root, "metadata", f"{fid}.json")
        if os.path.exists(meta_src):
            shutil.copy(meta_src, os.path.join(meta_dir, f"{fid}.json"))

        ic = out["factor"].corr(out["forward_return"], method="spearman")
        print(f"[OK] {fid}: {len(out)} rows, {out['asset'].nunique()} assets, "
              f"OOS RankIC={ic:.3f}" if not pd.isna(ic) else f"[OK] {fid}: {len(out)} rows")

    print("recompute done.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oos-start", default="2025-01-01")
    ap.add_argument("--oos-end", default="2025-06-30")
    ap.add_argument("--db-root", default="factor_db_oos")
    ap.add_argument("--src-root", default="factor_db", help="Source db root holding the mined factors")
    ap.add_argument("--warmup-start", default=WARMUP_START, help="Look-back start for rolling windows (not evaluated)")
    ap.add_argument("--factors", default=",".join(INSPECTED))
    args = ap.parse_args()
    factor_ids = [f.strip() for f in args.factors.split(",") if f.strip()]
    recompute(
        factor_ids,
        args.oos_start,
        args.oos_end,
        args.db_root,
        src_root=args.src_root,
        warmup_start=args.warmup_start,
    )


if __name__ == "__main__":
    main()
