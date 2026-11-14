"""FactorMiner -> ml4t-backtest adapter.

Loads stored factor value snapshots from ``factor_db/values`` and the matching
local market data (canonical V4 feather files), builds a cross-sectional
long/short signal, and runs an event-driven point-in-time backtest via
``ml4t.backtest``.

Reused system components (instead of hand-rolled path parsing):
  - core.data_feed.naming.data_filename / data_path  -> canonical file resolution
  - core.storage.factor_storage.LocalFactorStorage   -> factor value + metadata access

Design notes
------------
* Signal orientation (which tail of the factor to long) is inferred per factor
  from the stored snapshot using a Spearman rank correlation between the factor
  value and its forward return. This keeps the engine general: it works for any
  factor set, not just the original six negative-IC candidates, and does not
  depend on possibly-stale metadata IC fields.
* A run with no explicit --start/--end is IN-SAMPLE by construction when the
  factor snapshots only cover the mine period. For a genuine out-of-sample run,
  recompute factor values on a held-out window into a separate db_root (see
  user_workspace/experiment_tools/recompute_oos_factors.py) and pass
  --orientation-db-root pointing at the in-sample library. The predictive sign
  is then learned from training data only, avoiding look-ahead leakage.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Mapping, Optional, Sequence

import pandas as pd
import polars as pl
from ml4t.backtest import BacktestConfig, DataFeed, Engine, Strategy
from ml4t.backtest.config import (
    CommissionType,
    ExecutionMode,
    ExecutionPrice,
    SlippageType,
)

from core.data_feed.naming import data_filename, data_path
from core.storage.factor_storage import LocalFactorStorage

logger = logging.getLogger(__name__)

DEFAULT_DB_ROOT = "factor_db"
DEFAULT_DATA_ROOT = "data"
DEFAULT_EXCHANGE = "binance"
DEFAULT_TIMEFRAME = "1d"
DEFAULT_TRADE_TYPE = "futures"


class CrossSectionalFactorStrategy(Strategy):
    """Rebalance a cross-sectional long/short book from a single signal column.

    On every ``rebalance_every`` bar the strategy ranks the assets by the signal
    (highest first), goes long the top ``n_long`` and short the bottom
    ``n_short``, sizing each leg to an equal share of account equity. Negative
    share quantities are submitted for the short leg (ml4t supports shorting via
    a negative quantity when ``allow_short_selling`` is enabled).
    """

    def __init__(
        self,
        signal_col: str = "prediction",
        min_assets: int = 10,
        rebalance_every: int = 5,
        n_long: int = 5,
        n_short: int = 5,
    ) -> None:
        self.signal_col = signal_col
        self.min_assets = min_assets
        self.rebalance_every = rebalance_every
        self.n_long = n_long
        self.n_short = n_short
        self.held: set[str] = set()
        self._bar = 0

    def on_data(self, timestamp, data, context, broker) -> None:  # noqa: D401
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
        longs = [r[0] for r in rows[: self.n_long]]
        shorts = [r[0] for r in rows[-self.n_short :]]
        target = set(longs) | set(shorts)

        equity = broker.get_account_value()
        per = equity / (self.n_long + self.n_short)
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


def infer_orientation(values: pd.DataFrame) -> float:
    """Return +1 if higher factor value predicts higher forward return, else -1.

    Uses a Spearman rank correlation on the stored snapshot so the engine does
    not depend on metadata IC fields (which may be stale or corrupted).
    """
    sub = values.dropna(subset=["factor", "forward_return"])
    if len(sub) < 10:
        logger.warning("Too few rows to infer orientation; defaulting to -1 (long low factor)")
        return -1.0
    corr = sub["factor"].corr(sub["forward_return"], method="spearman")
    if pd.isna(corr) or corr == 0:
        return -1.0
    return 1.0 if corr > 0 else -1.0


def load_factor_signals(
    factor_ids: Sequence[str],
    db_root: str = DEFAULT_DB_ROOT,
    signal_col: str = "prediction",
    orientation_db_root: Optional[str] = None,
) -> pd.DataFrame:
    """Ensemble factor value snapshots into a per-(timestamp, asset) signal.

    Each factor is ranked cross-sectionally per timestamp; the rank is oriented
    by the factor's predictive sign, then averaged across factors into a single
    ``signal_col`` prediction (higher => stronger long).

    ``orientation_db_root`` decouples *where the signal direction is learned*
    from *where the evaluated factor values come from*. For a genuine
    out-of-sample run, point ``db_root`` at the held-out factor values and
    ``orientation_db_root`` at the in-sample library: the sign is then frozen
    from training data only, so no information from the evaluation window leaks
    into the signal. When omitted, orientation is inferred from ``db_root``
    itself (legacy behaviour, valid for in-sample runs).
    """
    storage = LocalFactorStorage(db_root)
    orient_storage = (
        LocalFactorStorage(orientation_db_root)
        if orientation_db_root and orientation_db_root != db_root
        else storage
    )
    frames: list[pd.DataFrame] = []
    orientations: dict[str, float] = {}
    used_ids: list[str] = []
    for fid in factor_ids:
        df = storage.load_factor_values(fid)
        if df is None or df.empty:
            logger.warning("No factor values for %s; skipping", fid)
            continue
        keep = [c for c in ("timestamp", "asset", "factor", "forward_return") if c in df.columns]
        df = df[keep].copy()
        df["factor_id"] = fid

        # Learn the predictive sign from the designated orientation snapshot.
        # Using the evaluation window here would be look-ahead leakage.
        orient_df = df if orient_storage is storage else orient_storage.load_factor_values(fid)
        if orient_df is None or orient_df.empty:
            logger.warning(
                "No orientation snapshot for %s in %s; falling back to evaluation snapshot",
                fid, orientation_db_root,
            )
            orient_df = df
        orientations[fid] = infer_orientation(orient_df)
        frames.append(df)
        used_ids.append(fid)

    if not frames:
        raise ValueError("No usable factor value snapshots found for the given ids")

    long = pd.concat(frames, ignore_index=True)
    long["rank"] = long.groupby(["timestamp", "factor_id"])["factor"].rank(method="first")
    long["oriented"] = long["factor_id"].map(orientations) * long["rank"]
    mean_oriented = (
        long.groupby(["timestamp", "asset"])["oriented"].mean().reset_index(name=signal_col)
    )
    mean_oriented["timestamp"] = pd.to_datetime(mean_oriented["timestamp"])
    mean_oriented = mean_oriented.sort_values(["timestamp", "asset"]).reset_index(drop=True)
    logger.info(
        "Built ensemble signal from %d factors; orientations=%s",
        len(used_ids),
        {fid: orientations[fid] for fid in used_ids},
    )
    return mean_oriented


def load_prices(
    signal_assets: Iterable[str],
    data_root: str = DEFAULT_DATA_ROOT,
    exchange: str = DEFAULT_EXCHANGE,
    timeframe: str = DEFAULT_TIMEFRAME,
    trade_type: str = DEFAULT_TRADE_TYPE,
) -> pd.DataFrame:
    """Load local V4 feather OHLCV for the assets present in the signal.

    Resolves each file with ``core.data_feed.naming.data_path`` so it matches the
    canonical ``{safe_symbol}-{timeframe}-{trade_type}.feather`` layout. Assets
    without a local file are skipped (and reported) rather than aborting.
    """
    parts: list[pd.DataFrame] = []
    missing: list[str] = []
    for asset in sorted(set(signal_assets)):
        path = data_path(data_root, exchange, asset, timeframe, trade_type)
        if not os.path.exists(path):
            missing.append(asset)
            continue
        df = pd.read_feather(path)
        needed = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
        df = df[needed].copy()
        df["asset"] = asset
        df = df.rename(columns={"date": "timestamp"})
        parts.append(df)
    if missing:
        logger.warning(
            "No local %s data for %d asset(s); skipped: %s",
            trade_type,
            len(missing),
            missing[:8],
        )
    if not parts:
        raise FileNotFoundError(
            f"No local {trade_type} market data files matched the factor assets under {data_root}"
        )
    prices = pd.concat(parts, ignore_index=True)
    prices["timestamp"] = pd.to_datetime(prices["timestamp"])
    return prices.sort_values(["timestamp", "asset"]).reset_index(drop=True)


def build_funding(prices: pd.DataFrame, rate: float = 0.0001) -> pd.DataFrame:
    """Perpetual-funding grid aligned to the traded timestamps/assets."""
    grid = pd.MultiIndex.from_product(
        [prices["timestamp"].drop_duplicates(), prices["asset"].drop_duplicates()],
        names=["timestamp", "asset"],
    ).to_frame(index=False)
    grid["rate"] = rate
    return grid


def build_backtest_config(
    initial_cash: float = 1_000_000.0,
    commission_rate: float = 0.0004,
    slippage_rate: float = 0.0005,
    allow_short_selling: bool = True,
    allow_leverage: bool = True,
) -> BacktestConfig:
    """Event-driven config: next-bar close fills, percentage cost + slippage."""
    return BacktestConfig(
        initial_cash=initial_cash,
        allow_short_selling=allow_short_selling,
        allow_leverage=allow_leverage,
        execution_mode=ExecutionMode.NEXT_BAR,
        execution_price=ExecutionPrice.CLOSE,
        next_bar_execution_price=ExecutionPrice.CLOSE,
        commission_type=CommissionType.PERCENTAGE,
        commission_rate=commission_rate,
        commission_minimum=0.0,
        slippage_type=SlippageType.PERCENTAGE,
        slippage_rate=slippage_rate,
        partial_fills_allowed=True,
        skip_cash_validation=True,
    )


def run_backtest(
    factor_ids: Sequence[str],
    *,
    db_root: str = DEFAULT_DB_ROOT,
    orientation_db_root: Optional[str] = None,
    data_root: str = DEFAULT_DATA_ROOT,
    exchange: str = DEFAULT_EXCHANGE,
    timeframe: str = DEFAULT_TIMEFRAME,
    trade_type: str = DEFAULT_TRADE_TYPE,
    start: Optional[str] = None,
    end: Optional[str] = None,
    initial_cash: float = 1_000_000.0,
    commission_rate: float = 0.0004,
    slippage_rate: float = 0.0005,
    funding_rate: float = 0.0001,
    n_long: int = 5,
    n_short: int = 5,
    rebalance_every: int = 5,
    min_assets: Optional[int] = None,
) -> dict:
    """Run a full cross-sectional factor backtest.

    Returns a dict with ``result`` (ml4t Result), ``metrics``, ``summary``,
    and the source ``prices``/``signals``/``funding`` frames for downstream
    export or analysis.
    """
    signals = load_factor_signals(
        factor_ids, db_root=db_root, orientation_db_root=orientation_db_root
    )
    lo_all = str(signals["timestamp"].min().date())
    hi_all = str(signals["timestamp"].max().date())

    prices = load_prices(
        signals["asset"].unique(),
        data_root=data_root,
        exchange=exchange,
        timeframe=timeframe,
        trade_type=trade_type,
    )

    s_lo = pd.Timestamp(start) if start else pd.Timestamp(lo_all)
    s_hi = pd.Timestamp(end) if end else pd.Timestamp(hi_all)
    prices = prices[(prices["timestamp"] >= s_lo) & (prices["timestamp"] <= s_hi)]
    signals = signals[(signals["timestamp"] >= s_lo) & (signals["timestamp"] <= s_hi)]

    if signals.empty or prices.empty:
        raise ValueError(
            f"No overlapping data for window {s_lo.date()}..{s_hi.date()} "
            f"(signals: {len(signals)}, prices: {len(prices)})"
        )

    funding = build_funding(prices, funding_rate)
    if min_assets is None:
        min_assets = n_long + n_short

    config = build_backtest_config(
        initial_cash=initial_cash,
        commission_rate=commission_rate,
        slippage_rate=slippage_rate,
    )
    strategy = CrossSectionalFactorStrategy(
        signal_col="prediction",
        min_assets=min_assets,
        rebalance_every=rebalance_every,
        n_long=n_long,
        n_short=n_short,
    )
    feed = DataFeed(prices_df=pl.from_pandas(prices), signals_df=pl.from_pandas(signals))
    engine = Engine(feed, strategy, config, funding_df=pl.from_pandas(funding))
    result = engine.run()
    m = result.metrics

    summary = {
        "scope": f"{s_lo.date()}..{s_hi.date()}",
        "signal_db_root": db_root,
        "orientation_db_root": orientation_db_root or db_root,
        "strict_oos": bool(orientation_db_root and orientation_db_root != db_root),
        "n_factors": len(factor_ids),
        "n_assets": int(prices["asset"].nunique()),
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
        "rebalance_every_bars": rebalance_every,
        "commission_rate": commission_rate,
        "slippage_rate": slippage_rate,
        "daily_funding_rate": funding_rate,
    }
    return {
        "result": result,
        "metrics": m,
        "summary": summary,
        "prices": prices,
        "signals": signals,
        "funding": funding,
    }
