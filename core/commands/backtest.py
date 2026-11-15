"""CLI command: backtest a set of stored FactorMiner factors.

Usage (run from the project root):
  python -m core.cli backtest \\
      --factors fac_c25e28c3,fac_8af952ab,fac_b66d17ce,fac_099a7eef,fac_f20a1349,fac_2017455e \\
      --start 2024-01-01 --end 2024-12-31 \\
      --commission 0.0004 --slippage 0.0005 --funding 0.0001 --rebalance 5 \\
      --output user_workspace/reports/backtest_run

Strict out-of-sample (direction frozen from 2024, evaluated on 2025H1):
  python -m core.cli backtest --db-root factor_db_oos \\
      --orientation-db-root factor_db --start 2025-01-01 --end 2025-06-30 \\
      --output user_workspace/reports/backtest_oos_strict

Cost/trading parameters can also be supplied via a JSON file passed to --config
(keys: commission_rate, slippage_rate, funding_rate, rebalance_every, n_long,
n_short, initial_cash). CLI flags override the defaults; --config overrides the
defaults but is itself overridden by explicit CLI flags.
"""

import argparse
import json
import logging
import os

import pandas as pd

from core.backtest.backtest_engine import run_backtest
from core.evaluation import holdout_guard

# Default is resolved at runtime from factor_db (any factor with a stored value
# snapshot). The old hard-coded six ids were downgraded to DISCOVERED_UNVERIFIED
# by the library re-audit, so pinning them here would silently run a dead set.
DEFAULT_FACTORS: list[str] = []

SUMMARY_KEYS = [
    "scope",
    "signal_db_root",
    "orientation_db_root",
    "strict_oos",
    "n_factors",
    "n_assets",
    "weighting",
    "effective_weights",
    "expanded_combinations",
    "initial_cash",
    "final_value",
    "total_return_pct",
    "max_drawdown_pct",
    "sharpe",
    "cagr",
    "volatility",
    "num_trades",
    "num_fills",
    "total_commission",
    "total_slippage",
    "total_funding",
    "total_costs",
    "avg_turnover",
    "avg_open_positions",
    "rebalance_every_bars",
    "commission_rate",
    "slippage_rate",
    "daily_funding_rate",
]


def _discover_factor_ids(db_root: str) -> list[str]:
    """All factor ids that currently have a stored value snapshot."""
    from core.storage.factor_storage import LocalFactorStorage

    storage = LocalFactorStorage(db_root)
    ids: list[str] = []
    for meta in storage.list_metadata():
        if storage.load_factor_values(meta.factor_id) is not None:
            ids.append(meta.factor_id)
    return sorted(ids)


def _parse_weights(raw: str | None, factor_ids: list[str]) -> dict[str, float] | None:
    """Parse ``--weights`` as ``id=w,id=w`` or a positional ``w,w,w`` list.

    K-07: the old parser silently accepted unknown ids (a typo became an
    unweighted factor), mixed formats (uncaught ValueError) and NaN/inf/all-zero
    weights. All of those produce a backtest that looks valid but is not.
    """
    if not raw:
        return None
    import math

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("--weights is empty")
    named = ["=" in p for p in parts]
    if any(named) and not all(named):
        raise ValueError(
            "--weights mixes 'id=w' and positional values; use one format only"
        )
    if all(named):
        pairs = [p.split("=", 1) for p in parts]
        try:
            weights = {k.strip(): float(v) for k, v in pairs}
        except ValueError as exc:
            raise ValueError(f"--weights contains a non-numeric value: {exc}") from exc
    else:
        try:
            values = [float(p) for p in parts]
        except ValueError as exc:
            raise ValueError(f"--weights contains a non-numeric value: {exc}") from exc
        if len(values) != len(factor_ids):
            raise ValueError(
                f"--weights has {len(values)} positional values but "
                f"{len(factor_ids)} factors were given"
            )
        weights = dict(zip(factor_ids, values))

    unknown = sorted(set(weights) - set(factor_ids))
    if unknown:
        raise ValueError(
            f"--weights references unknown factor id(s): {', '.join(unknown[:5])}"
        )
    for fid, value in weights.items():
        if not math.isfinite(value):
            raise ValueError(f"--weights for '{fid}' is not finite: {value}")
    if weights and all(abs(v) <= 1e-12 for v in weights.values()):
        raise ValueError("--weights are all zero; the ensemble would be undefined")
    return weights


def parse_args():
    parser = argparse.ArgumentParser(description="FactorMiner backtest (ml4t-backtest adapter)")
    parser.add_argument(
        "--factors",
        type=str,
        default="",
        help="Comma-separated factor IDs from factor_db (default: every factor with a stored snapshot)",
    )
    parser.add_argument(
        "--weights",
        type=str,
        default=None,
        help="Ensemble weights: 'id=w,id=w' or positional 'w,w,w' aligned with --factors. "
             "Stored linear-combination factors keep their fitted weights automatically.",
    )
    parser.add_argument(
        "--no-expand-combinations",
        action="store_true",
        help="Treat a stored linear-combination factor as a single opaque signal instead of "
             "expanding it into its members",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional JSON with cost params (commission_rate, slippage_rate, funding_rate, rebalance_every, n_long, n_short, initial_cash)",
    )
    parser.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD (default: factor snapshot min)")
    parser.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD (default: factor snapshot max)")
    parser.add_argument("--exchange", type=str, default="binance")
    parser.add_argument("--timeframe", type=str, default="1d")
    parser.add_argument("--trade-type", type=str, default="futures")
    parser.add_argument("--db-root", type=str, default="factor_db",
                        help="Root holding the factor VALUE snapshots to evaluate")
    parser.add_argument(
        "--orientation-db-root", type=str, default=None,
        help="Root holding IN-SAMPLE snapshots used only to learn signal direction. "
             "For strict OOS: --db-root <held-out> --orientation-db-root <in-sample>. "
             "Omit for in-sample runs (direction learned from --db-root).",
    )
    parser.add_argument("--data-root", type=str, default="data")
    # K-04: None means "not passed", so an explicit CLI flag can beat --config
    # while an untouched flag falls back to config and then to the built-in
    # default. Non-None defaults made that distinction impossible.
    parser.add_argument("--commission", type=float, default=None, help="Taker commission (fraction of trade value)")
    parser.add_argument("--slippage", type=float, default=None, help="Slippage (fraction of price)")
    parser.add_argument("--funding", type=float, default=None, help="Daily perpetual funding rate (fraction)")
    parser.add_argument("--constant-funding", action="store_true", help="Force the constant funding grid instead of the real crypto_streams history (real funding is used by default)")
    parser.add_argument("--rebalance", type=int, default=None, help="Bars between rebalances")
    parser.add_argument("--n-long", type=int, default=None, help="Number of long positions")
    parser.add_argument("--n-short", type=int, default=None, help="Number of short positions")
    parser.add_argument("--initial-cash", type=float, default=None)
    parser.add_argument(
        "--output",
        type=str,
        default=os.path.join("user_workspace", "reports", "backtest_run"),
        help="Directory to write the result parquet + summary CSV",
    )
    parser.add_argument(
        "--allow-holdout-reuse",
        action="store_true",
        help="Permit evaluating an already-revealed holdout window (recompute a "
             "published result only). An audit line is always written to "
             "user_workspace/reports/holdout_overrides.jsonl.",
    )
    parser.add_argument(
        "--holdout-mode",
        type=str,
        default=None,
        choices=list(holdout_guard.VALID_MODES),
        help="Override the holdout guard mode (default: strict)",
    )
    return parser.parse_args()


def run_backtest_cmd(args):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("FactorMinerBacktest")

    overrides: dict = {}
    if args.config:
        if not os.path.exists(args.config):
            logger.error("Cost config not found: %s", args.config)
            return 1
        try:
            with open(args.config, encoding="utf-8-sig") as f:
                overrides = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Cannot read cost config %s: %s", args.config, exc)
            return 1
        if not isinstance(overrides, dict):
            logger.error("Cost config %s must be a JSON object", args.config)
            return 1
        logger.info("Loaded cost defaults from %s: %s", args.config, list(overrides))

    # K-04: explicit CLI > --config > built-in default. The old
    # ``overrides.get(key, args.x)`` let a stale config silently beat an
    # explicit ``--commission``/``--rebalance`` flag.
    def pick(cli_value, cfg_key: str, fallback):
        if cli_value is not None:
            return cli_value
        return overrides.get(cfg_key, fallback)

    # ``getattr`` keeps programmatic callers / older Namespace shapes working.
    def arg(name):
        return getattr(args, name, None)

    initial_cash = pick(arg("initial_cash"), "initial_cash", 1_000_000.0)
    commission_rate = pick(arg("commission"), "commission_rate", 0.0004)
    slippage_rate = pick(arg("slippage"), "slippage_rate", 0.0005)
    funding_rate = pick(arg("funding"), "funding_rate", 0.0001)
    n_long = pick(arg("n_long"), "n_long", 5)
    n_short = pick(arg("n_short"), "n_short", 5)
    rebalance_every = pick(arg("rebalance"), "rebalance_every", 5)

    raw_factors = getattr(args, "factors", "") or ""
    factor_ids = [f.strip() for f in raw_factors.split(",") if f.strip()]
    if not factor_ids:
        factor_ids = _discover_factor_ids(args.db_root)
        if factor_ids:
            logger.info("No --factors given; using %d stored factor(s)", len(factor_ids))
    if not factor_ids:
        logger.error("No factor ids provided and none found under %s", args.db_root)
        return 1

    try:
        weights = _parse_weights(args.weights, factor_ids)
    except ValueError as exc:
        logger.error("Invalid --weights: %s", exc)
        return 2
    if bool(args.start) != bool(args.end):
        logger.error("--start and --end must be supplied together")
        return 2

    # P0-b: ANY entry point that reads holdout returns must pass the single
    # ledger guard *before* computing anything. --orientation-db-root marks a
    # strict-OOS run, i.e. a one-time final reveal.
    holdout_guard.guard(
        start=args.start,
        end=args.end,
        purpose="final" if args.orientation_db_root else "explore",
        config=overrides,
        mode=getattr(args, "holdout_mode", None),
        allow_reuse=getattr(args, "allow_holdout_reuse", False),
        source="cli:backtest",
        note=f"factors={len(factor_ids)} db_root={args.db_root}",
    )

    out = run_backtest(
        factor_ids,
        db_root=args.db_root,
        orientation_db_root=args.orientation_db_root,
        data_root=args.data_root,
        exchange=args.exchange,
        timeframe=args.timeframe,
        trade_type=args.trade_type,
        start=args.start,
        end=args.end,
        initial_cash=initial_cash,
        commission_rate=commission_rate,
        slippage_rate=slippage_rate,
        funding_rate=funding_rate,
        n_long=n_long,
        n_short=n_short,
        rebalance_every=rebalance_every,
        weights=weights,
        use_real_funding=not bool(getattr(args, "constant_funding", False)),
        resolve_combinations=not bool(getattr(args, "no_expand_combinations", False)),
    )
    summary = out["summary"]
    result = out["result"]

    print("\n=== FACTORMINER BACKTEST RESULT ===")
    for k in SUMMARY_KEYS:
        print(f"  {k}: {summary.get(k)}")

    os.makedirs(args.output, exist_ok=True)
    result.to_parquet(args.output)
    out["prices"].to_csv(os.path.join(args.output, "prices.csv"), index=False)
    out["signals"].to_csv(os.path.join(args.output, "signals.csv"), index=False)
    out["funding"].to_csv(os.path.join(args.output, "funding.csv"), index=False)
    if "equity" in out and out["equity"] is not None and not out["equity"].empty:
        out["equity"].to_csv(os.path.join(args.output, "equity.csv"), index=False)
    pd.DataFrame([summary]).to_csv(os.path.join(args.output, "summary.csv"), index=False)
    print(f"\nExported result + summary to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_backtest_cmd(parse_args()))
