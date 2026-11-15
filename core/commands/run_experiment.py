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
    # K-08: no default here. A non-None default silently overwrote the config's
    # ``paradigm`` / ``user_dir`` even when the user never passed the flag, so a
    # config declaring LLM/MyCustomNN was launched as GP with a mismatched
    # operator_set_hash in the manifest.
    p.add_argument("--miner", default=None, help="Override the config's paradigm / miner class name")
    p.add_argument("--iterations", type=int, default=None, help="Override max_iterations")
    p.add_argument("--seed", type=int, default=None, help="Fix global RNG seed for reproducibility")
    p.add_argument("--resume", action="store_true", help="Skip stages already completed for this config")
    p.add_argument("--run-id", default=None, help="Explicit run id (default: generated)")
    p.add_argument("--user-dir", default=None, help="Override the config's user workspace directory")
    p.add_argument("--output-root", default=None, help="Override the runs root (mainly for tests)")
    p.add_argument("--data-root", default=None,
                   help="Override data_feeds.data_root (the directory the panel is read from)")
    p.add_argument("--preflight", action="store_true",
                   help="Run the doctor checks first and abort on any FATAL finding")
    p.add_argument("--holdout-mode", default=None,
                   choices=["strict", "audit", "off"],
                   help="Override evaluation.gates.holdout_mode for this run")
    p.add_argument("--allow-holdout-reuse", action="store_true",
                   help="Permit recomputing an already-revealed window (audited)")
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

    if getattr(args, "miner", None) is not None:
        config["paradigm"] = args.miner
    elif not config.get("paradigm"):
        config["paradigm"] = "GP"
    if args.iterations is not None:
        config["max_iterations"] = int(args.iterations)
    exp = config.setdefault("experiment", {})
    if getattr(args, "user_dir", None) is not None:
        exp["user_dir"] = args.user_dir
    else:
        exp.setdefault("user_dir", "user_workspace")
    if args.seed is not None:
        exp["seed"] = int(args.seed)

    # One resolved data root for the preflight, the miner and the refresh step.
    # Leaving them to disagree made ``experiment --data-root`` validate a
    # directory the panel loader never opened.
    from core.data_feed.naming import resolve_data_root

    feeds = config.setdefault("data_feeds", {})
    data_root = resolve_data_root(
        getattr(args, "data_root", None) or feeds.get("data_root")
    )
    feeds["data_root"] = str(data_root)

    if getattr(args, "holdout_mode", None) or getattr(args, "allow_holdout_reuse", False):
        gates = ((config.setdefault("evaluation", {})).setdefault("gates", {}))
        if getattr(args, "holdout_mode", None):
            gates["holdout_mode"] = args.holdout_mode
        if getattr(args, "allow_holdout_reuse", False):
            gates["allow_holdout_reuse"] = True

    if getattr(args, "preflight", False):
        from core.commands import doctor as doctor_cmd

        rows = doctor_cmd.run_checks(
            args.config,
            data_root=str(data_root),
            user_dir=exp.get("user_dir", "user_workspace"),
            miner=config.get("paradigm"),
        )
        doctor_cmd._print_table(rows)
        if any(r["status"] == doctor_cmd.FATAL for r in rows):
            print("\npreflight failed: fix the FATAL checks above.", flush=True)
            raise SystemExit(1)

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
