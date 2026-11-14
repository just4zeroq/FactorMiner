"""CLI entry for the end-to-end experiment pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Run a complete, reproducible FactorMiner experiment"
    )
    p.add_argument("--config", required=True, help="Path to the JSON config")
    p.add_argument("--miner", default="GP", help="Paradigm / miner class name")
    p.add_argument("--iterations", type=int, default=None, help="Override max_iterations")
    p.add_argument("--seed", type=int, default=None, help="Fix global RNG seed for reproducibility")
    p.add_argument("--resume", action="store_true", help="Skip stages already completed for this config")
    p.add_argument("--run-id", default=None, help="Explicit run id (default: generated)")
    p.add_argument("--user-dir", default="user_workspace", help="User workspace with custom modules")
    p.add_argument("--output-root", default=None, help="Override the runs root (mainly for tests)")
    return p.parse_args(argv)


def run_experiment_cmd(args):
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    try:
        with open(args.config, "r", encoding="utf-8-sig") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read config: {exc}", file=sys.stderr)
        sys.exit(1)

    config["paradigm"] = args.miner
    if args.iterations is not None:
        config["max_iterations"] = int(args.iterations)
    exp = config.setdefault("experiment", {})
    exp["user_dir"] = args.user_dir
    if args.seed is not None:
        exp["seed"] = int(args.seed)

    from pathlib import Path
    from core.experiment.pipeline import run_experiment

    result = run_experiment(
        config,
        config_path=args.config,
        run_id=args.run_id,
        resume=bool(args.resume),
        root=Path(args.output_root) if args.output_root else None,
        progress=lambda msg: print(msg, flush=True),
    )
    state = result["state"]
    mining = state.get("mining", {})
    wf = state.get("validated", {})
    bt = state.get("backtested", {}).get("summary", {})
    print("\n=== FACTORMINER EXPERIMENT ===")
    print(f"  run_id       : {result['run_id']}")
    print(f"  run_dir      : {result['run_dir']}")
    print(f"  candidates   : {mining.get('n_candidates')} -> {len(mining.get('factor_ids', []))} stored")
    print(f"  promoted     : {mining.get('n_promoted')}")
    print(f"  val reads    : {mining.get('val_read_count')}")
    print(f"  wf folds     : {wf.get('n_folds')} (mean RankIC {wf.get('mean_rank_ic')}, refit/fold {wf.get('refit_per_fold')})")
    print(f"  backtest     : scope={bt.get('scope')} return={bt.get('total_return_pct')}% sharpe={bt.get('sharpe')}")
    print(f"  funding      : {bt.get('funding_source')}")
    print(f"  report       : {result['run_dir']}/report.md")
    return result


if __name__ == "__main__":
    run_experiment_cmd(parse_args())
