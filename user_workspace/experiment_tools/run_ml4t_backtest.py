"""Bridge FactorMiner factors into ml4t-backtest for a cross-sectional
long/short portfolio backtest.

NOTE ON SAMPLE SCOPE
--------------------
FactorMiner stores factor *values* snapshots only for the mine period
(2024). There is no precomputed test-period (2025H1) factor surface yet, so
this script backtests on the period the factor values actually cover
(in-sample). A true out-of-sample run requires recomputing the six factors on
2025H1 data first (separate task). The interval is derived automatically from
the factor snapshot timestamps.

What it does
------------
1. Loads the six recommended factor value snapshots (timestamp, asset, factor).
2. Builds a cross-sectional ensemble signal: per (timestamp, factor) rank the
   assets, average the ranks across the six factors, then invert (the six
   factors are all negative-IC, so lower factor value => long).
3. Loads 20 binance 1d futures OHLCV feathers, mapping filenames to the same
   asset ids used by the factor snapshots (BTC/USDT:USDT).
4. Builds a daily funding table (Binance-style perpetual funding, conservative
   fixed 0.01%/day) -- ml4t applies it natively via the Engine funding_df.
5. Runs a cross-sectional long/short strategy (top/bottom N assets) with
   percentage commission + slippage, next-bar fills, and a configurable
   rebalance cadence (default weekly to keep turnover/cost realistic).
6. Exports the full result plus a metrics summary CSV.
"""

from __future__ import annotations

import glob
import os

import pandas as pd
import polars as pl
from ml4t.backtest import BacktestConfig, DataFeed, Engine, Strategy
from ml4t.backtest.config import CommissionType, ExecutionMode, ExecutionPrice, SlippageType

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FACTOR_IDS = [
    "fac_c25e28c3",
    "fac_8af952ab",
    "fac_b66d17ce",
    "fac_099a7eef",
    "fac_f20a1349",
    "fac_2017455e",
]
INITIAL_CASH = 1_000_000.0
N_LONG = 5
N_SHORT = 5
REBALANCE_EVERY = int(os.environ.get("REB", "5"))  # bars between rebalances
DAILY_FUNDING_RATE = float(os.environ.get("FUND", "0.0001"))  # 0.01%/day perpetual funding
COMMISSION_RATE = float(os.environ.get("COMM", "0.0004"))     # 0.04% taker
SLIPPAGE_RATE = float(os.environ.get("SLIP", "0.0005"))       # 0.05%


def _feather_asset(basename: str) -> str:
    core = basename.split("-1d-futures.feather")[0]  # BTC_USDT_USDT
    assert core.endswith("_USDT"), core
    sym = core[: -len("_USDT")]  # BTC_USDT
    return sym.replace("_", "/") + ":USDT"  # BTC/USDT:USDT


def build_prices() -> pd.DataFrame:
    pattern = os.path.join(PROJECT_ROOT, "data", "binance", "futures", "*-1d-futures.feather")
    parts = []
    for path in sorted(glob.glob(pattern)):
        df = pd.read_feather(path)
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df["asset"] = _feather_asset(os.path.basename(path))
        df = df.rename(columns={"date": "timestamp"})
        parts.append(df)
    prices = pd.concat(parts, ignore_index=True)
    prices["timestamp"] = pd.to_datetime(prices["timestamp"])
    return prices


def build_signals() -> tuple[pd.DataFrame, tuple[str, str]]:
    frames = []
    for fid in FACTOR_IDS:
        path = os.path.join(PROJECT_ROOT, "factor_db", "values", f"{fid}.parquet")
        df = pd.read_parquet(path)[["timestamp", "asset", "factor"]].copy()
        df["factor_id"] = fid
        frames.append(df)
    long = pd.concat(frames, ignore_index=True)
    long["rank"] = long.groupby(["timestamp", "factor_id"])["factor"].rank(method="first")
    mean_rank = (
        long.groupby(["timestamp", "asset"])["rank"].mean().reset_index(name="mean_rank")
    )
    mean_rank["prediction"] = -mean_rank["mean_rank"]  # negative-IC inversion
    sig = mean_rank[["timestamp", "asset", "prediction"]].copy()
    lo = str(sig["timestamp"].min().date())
    hi = str(sig["timestamp"].max().date())
    return sig, (lo, hi)


def build_funding(prices: pd.DataFrame, rate: float = 0.0001) -> pd.DataFrame:
    grid = pd.MultiIndex.from_product(
        [prices["timestamp"].drop_duplicates(), prices["asset"].drop_duplicates()],
        names=["timestamp", "asset"],
    ).to_frame(index=False)
    grid["rate"] = rate
    return grid


class CrossSectionalFactorStrategy(Strategy):
    def __init__(self, signal_col: str = "prediction", min_assets: int = 12, rebalance_every: int = 5):
        self.signal_col = signal_col
        self.min_assets = min_assets
        self.rebalance_every = rebalance_every
        self.held: set[str] = set()
        self._bar = 0

    def on_data(self, timestamp, data, context, broker) -> None:
        self._bar += 1
        if self._bar > 1 and (self._bar - 1) % self.rebalance_every != 0:
            return
        rows = []
        for asset, bar in data.items():
            sigs = bar.get("signals") or {}
            sig = sigs.get(self.signal_col)
            price = bar.get("price", bar.get("close"))
            if sig is None or price is None or not isinstance(price, (int, float)) or price <= 0:
                continue
            rows.append((asset, float(sig), float(price)))
        if len(rows) < self.min_assets:
            return
        rows.sort(key=lambda r: r[1], reverse=True)
        longs = [r[0] for r in rows[:N_LONG]]
        shorts = [r[0] for r in rows[-N_SHORT:]]
        target = set(longs) | set(shorts)
        equity = broker.get_account_value()
        per = equity / (N_LONG + N_SHORT)
        price_map = {a: p for a, _s, p in rows}
        desired: dict[str, float] = {}
        for a in longs:
            desired[a] = per / price_map[a]
        for a in shorts:
            desired[a] = -per / price_map[a]
        for asset in set(desired) | self.held:
            pos = broker.get_position(asset)
            held_qty = pos.quantity if pos is not None else 0.0
            tgt = desired.get(asset, 0.0)
            if abs(tgt - held_qty) > 1e-9:
                broker.submit_order(asset, tgt - held_qty)
        self.held = target


def main() -> None:
    signals, (lo, hi) = build_signals()
    prices = build_prices()
    prices = prices[(prices["timestamp"] >= pd.Timestamp(lo)) & (prices["timestamp"] <= pd.Timestamp(hi))]
    funding = build_funding(prices, DAILY_FUNDING_RATE)

    print(f"Signal window: {lo} .. {hi} (IN-SAMPLE / mine period)")
    print(f"Prices rows: {len(prices)}  assets: {prices['asset'].nunique()}")
    print(f"Funding rows: {len(funding)}  rebalance_every: {REBALANCE_EVERY} bars")

    prices_pl = pl.from_pandas(prices)
    signals_pl = pl.from_pandas(signals)
    funding_pl = pl.from_pandas(funding)

    config = BacktestConfig(
        initial_cash=INITIAL_CASH,
        allow_short_selling=True,
        allow_leverage=True,
        execution_mode=ExecutionMode.NEXT_BAR,
        execution_price=ExecutionPrice.CLOSE,
        next_bar_execution_price=ExecutionPrice.CLOSE,
        commission_type=CommissionType.PERCENTAGE,
        commission_rate=COMMISSION_RATE,
        commission_minimum=0.0,
        slippage_type=SlippageType.PERCENTAGE,
        slippage_rate=SLIPPAGE_RATE,
        partial_fills_allowed=True,
        skip_cash_validation=True,
    )
    strategy = CrossSectionalFactorStrategy(rebalance_every=REBALANCE_EVERY)
    feed = DataFeed(prices_df=prices_pl, signals_df=signals_pl)
    engine = Engine(feed, strategy, config, funding_df=funding_pl)
    result = engine.run()

    m = result.metrics
    print("\n=== RESULT METRICS ===")
    for key in (
        "initial_cash", "final_value", "total_return_pct", "max_drawdown_pct",
        "sharpe", "cagr", "volatility", "num_trades", "num_fills",
        "total_commission", "total_slippage", "total_funding", "total_costs",
        "avg_turnover", "max_turnover", "avg_open_positions", "win_rate",
    ):
        print(f"  {key}: {m.get(key)}")

    out_dir = os.path.join(PROJECT_ROOT, "user_workspace", "reports", "backtest_run_insample_2024")
    result.to_parquet(out_dir)
    portfolio = result.to_portfolio_state_dataframe().to_pandas()
    fills = result.to_fills_dataframe().to_pandas()
    portfolio.to_csv(os.path.join(out_dir, "portfolio_state.csv"), index=False)
    fills.to_csv(os.path.join(out_dir, "fills.csv"), index=False)

    summary = {
        "scope": f"in-sample {lo}..{hi}",
        "n_factors": len(FACTOR_IDS),
        "n_assets": prices["asset"].nunique(),
        "initial_cash": m.get("initial_cash"),
        "final_value": m.get("final_value"),
        "total_return_pct": m.get("total_return_pct"),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "sharpe": m.get("sharpe"),
        "cagr": m.get("cagr"),
        "volatility": m.get("volatility"),
        "num_trades": m.get("num_trades"),
        "num_fills": m.get("num_fills"),
        "total_commission": m.get("total_commission"),
        "total_slippage": m.get("total_slippage"),
        "total_funding": m.get("total_funding"),
        "total_costs": m.get("total_costs"),
        "avg_turnover": m.get("avg_turnover"),
        "avg_open_positions": m.get("avg_open_positions"),
        "rebalance_every_bars": REBALANCE_EVERY,
        "commission_rate": COMMISSION_RATE,
        "slippage_rate": SLIPPAGE_RATE,
        "daily_funding_rate": DAILY_FUNDING_RATE,
    }
    pd.DataFrame([summary]).to_csv(os.path.join(out_dir, "summary.csv"), index=False)
    print(f"\nExported result + summary to {out_dir}")


if __name__ == "__main__":
    main()
