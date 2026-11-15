import argparse
import sys
import os
import logging
import asyncio

def setup_parser():
    parser = argparse.ArgumentParser(description="FactorMiner V4 Global CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Miner Subcommand
    miner_parser = subparsers.add_parser("mine", help="Run the FactorMiner engine")
    miner_parser.add_argument("--miner", type=str, required=True, help="Name of the miner class/paradigm (e.g., GP, LLM)")
    miner_parser.add_argument("--config", type=str, required=True, help="Path to the JSON configuration file")
    miner_parser.add_argument("--iterations", type=int, default=None, help="Optional: Number of iterations (overrides config)")
    miner_parser.add_argument("--user-dir", type=str, default="user_workspace", help="Path to the user workspace directory")
    
    # End-to-end experiment (P5): pre-register -> data -> mine -> gates ->
    # walk-forward -> combine -> OOS recompute -> backtest -> report.
    exp_parser = subparsers.add_parser("experiment", help="Run a complete reproducible factor-mining experiment")
    exp_parser.add_argument("--config", type=str, required=True, help="Path to the JSON configuration file")
    exp_parser.add_argument("--miner", type=str, default=None, help="Override the config's paradigm / miner class name")
    exp_parser.add_argument("--iterations", type=int, default=None, help="Override max_iterations")
    exp_parser.add_argument("--seed", type=int, default=None, help="Fix global RNG seed for reproducibility")
    exp_parser.add_argument("--resume", action="store_true", help="Skip stages already completed for this config")
    exp_parser.add_argument("--run-id", type=str, default=None, help="Explicit run id")
    exp_parser.add_argument("--user-dir", type=str, default=None, help="Override the config's user workspace directory")
    exp_parser.add_argument("--output-root", type=str, default=None, help="Override the runs root (mainly for tests)")
    exp_parser.add_argument("--holdout-mode", type=str, default=None,
                            choices=["strict", "audit", "off"],
                            help="Override evaluation.gates.holdout_mode")
    exp_parser.add_argument("--allow-holdout-reuse", action="store_true",
                            help="Permit recomputing an already-revealed window (audited)")

    # Download Subcommand
    dl_parser = subparsers.add_parser("download", help="Download historical market data")
    dl_parser.add_argument("--exchange", type=str, default="binance", help="Exchange name (e.g., binance)")
    dl_parser.add_argument("--symbols", type=str, required=True, help="Comma-separated symbols (e.g., BTC/USDT,ETH/USDT)")
    dl_parser.add_argument("--timeframes", type=str, default="1d", help="Comma-separated timeframes (e.g., 1m,1h,1d)")
    dl_parser.add_argument("--type", type=str, default="spot", choices=["spot", "futures"], help="Market type (spot or futures)")
    dl_parser.add_argument("--start", type=str, required=True, help="Start date (YYYY-MM-DD)")
    dl_parser.add_argument("--end", type=str, required=True, help="End date (YYYY-MM-DD)")
    dl_parser.add_argument("--mode", type=str, default="full", choices=["full", "incremental", "overwrite"], help="Download mode (full, incremental, or overwrite)")
    
    # Inspect Subcommand
    inspect_parser = subparsers.add_parser("inspect", help="Inspect and audit factor across assets, periods, and metrics")
    inspect_parser.add_argument("--factor", type=str, default=None, help="Factor ID stored in factor_db")
    inspect_parser.add_argument("--ast", type=str, default=None, help="Raw AST string or dictionary literal")
    inspect_parser.add_argument("--code", type=str, default=None, help="Python factor code string")
    inspect_parser.add_argument("--config", type=str, default=None, help="Optional: Path to base JSON configuration file")
    inspect_parser.add_argument("--pairs", type=str, default=None, help="Comma-separated symbols (e.g., BTC/USDT:USDT,ETH/USDT:USDT)")
    inspect_parser.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD)")
    inspect_parser.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD)")
    inspect_parser.add_argument("--timeframe", type=str, default=None, help="Timeframe (e.g. 5m, 1h, 1d)")
    inspect_parser.add_argument("--user-dir", type=str, default="user_workspace", help="Path to user workspace directory")
    inspect_parser.add_argument("--allow-holdout-reuse", action="store_true", help="Permit inspecting an already-revealed holdout window (audited).")
    inspect_parser.add_argument("--holdout-mode", type=str, default=None, choices=["strict", "audit", "off"], help="Override the holdout guard mode (default: strict)")

    # Backtest Subcommand
    bt_parser = subparsers.add_parser(
        "backtest",
        help="Backtest stored factors as a cross-sectional long/short portfolio (ml4t-backtest)",
    )
    bt_parser.add_argument(
        "--factors",
        type=str,
        default="",
        help="Comma-separated factor IDs from factor_db "
             "(default: discover every factor with a stored value snapshot)",
    )
    bt_parser.add_argument("--config", type=str, default=None, help="Optional JSON with cost params (commission_rate, slippage_rate, funding_rate, rebalance_every, n_long, n_short, initial_cash)")
    bt_parser.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD (default: factor snapshot min)")
    bt_parser.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD (default: factor snapshot max)")
    bt_parser.add_argument("--exchange", type=str, default="binance")
    bt_parser.add_argument("--timeframe", type=str, default="1d")
    bt_parser.add_argument("--trade-type", type=str, default="futures")
    bt_parser.add_argument("--db-root", type=str, default="factor_db", help="Root holding the factor VALUE snapshots to evaluate")
    bt_parser.add_argument("--orientation-db-root", type=str, default=None, help="Root holding IN-SAMPLE snapshots used only to learn signal direction; for strict OOS point --db-root at held-out values and this at the in-sample library")
    bt_parser.add_argument("--data-root", type=str, default="data")
    bt_parser.add_argument("--commission", type=float, default=None, help="Taker commission (fraction of trade value)")
    bt_parser.add_argument("--slippage", type=float, default=None, help="Slippage (fraction of price)")
    bt_parser.add_argument("--funding", type=float, default=None, help="Daily perpetual funding rate (fraction)")
    bt_parser.add_argument("--constant-funding", action="store_true", help="Force the constant funding grid instead of the real crypto_streams history (real funding is used by default)")
    bt_parser.add_argument("--rebalance", type=int, default=None, help="Bars between rebalances")
    bt_parser.add_argument("--n-long", type=int, default=None, help="Number of long positions")
    bt_parser.add_argument("--n-short", type=int, default=None, help="Number of short positions")
    bt_parser.add_argument("--initial-cash", type=float, default=None)
    bt_parser.add_argument("--output", type=str, default=os.path.join("user_workspace", "reports", "backtest_run"), help="Directory to write result parquet + summary CSV")
    bt_parser.add_argument("--weights", type=str, default=None, help="Ensemble weights: 'id=w,id=w' or positional 'w,w,w'")
    bt_parser.add_argument("--no-expand-combinations", action="store_true", help="Treat a stored linear-combination factor as one opaque signal")
    bt_parser.add_argument("--allow-holdout-reuse", action="store_true", help="Permit evaluating an already-revealed holdout window (audited).")
    bt_parser.add_argument("--holdout-mode", type=str, default=None, choices=["strict", "audit", "off"], help="Override the holdout guard mode (default: strict)")

    return parser

def _exit_code(result) -> int:
    """Normalise the heterogeneous command return values into a shell exit code.

    Commands historically returned ``None``, a dict payload, a bool or an int.
    ``main()`` used to drop all of them, so ``factorminer backtest`` reported
    success even when ``run_backtest_cmd`` returned 1. Anything that is not an
    explicit failure is treated as 0 so legacy commands keep working.
    """
    if result is None:
        return 0
    if isinstance(result, bool):
        return 0 if result else 1
    if isinstance(result, int):
        return int(result)
    if isinstance(result, dict):
        if result.get("success") is False or result.get("ok") is False:
            return 1
        if result.get("error"):
            return 1
        state = result.get("state")
        if isinstance(state, dict):
            status = str(state.get("status", "")).lower()
            if status in {"failed", "error", "cancelled", "interrupted"}:
                return 1
        return 0
    return 0


def main(argv=None) -> int:
    parser = setup_parser()
    args = parser.parse_args(argv)

    if args.command == "mine":
        from core.commands import mine as miner_main
        result = miner_main.run_miner(args)
    elif args.command == "experiment":
        from core.commands.run_experiment import run_experiment_cmd
        result = run_experiment_cmd(args)
    elif args.command == "download":
        from core.commands import download as dl_main
        result = asyncio.run(dl_main.run_downloader(args))
    elif args.command == "inspect":
        from core.commands import inspect as inspect_main
        result = inspect_main.run_inspector(args)
    elif args.command == "backtest":
        from core.commands import backtest as bt_main
        result = bt_main.run_backtest_cmd(args)
    else:
        parser.print_help()
        return 2

    return _exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
