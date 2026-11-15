"""CLI entry for the end-to-end experiment pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys


def _add_common(p: argparse.ArgumentParser) -> None:
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
    # F7: dotted-path overrides, applied before the config hash is computed.
    p.add_argument(
        "--set", dest="overrides", action="append", default=None, metavar="PATH=VALUE",
        help="Override a config value, e.g. --set gp.max_depth=5 --set fitness.turnover_max=0.15",
    )
    # F8: cost estimate without starting a run.
    p.add_argument("--dry-run", action="store_true",
                   help="Print the candidate/throughput/DSR estimate and exit")
    # F6 / L2.2: multi-hypothesis sweep + sharding.
    p.add_argument("--sweep", default=None, help="sweep.json declaring the grid to run")
    p.add_argument("--shard", default=None, metavar="I/N",
                   help="Run only shard I of N (1-based) of the expanded sweep")
    # E4: stop conditions.
    p.add_argument("--stop-after-candidates", type=int, default=None,
                   help="Stop mining once this many candidates were evaluated")
    p.add_argument("--max-wallclock", type=int, default=None, metavar="SECONDS",
                   help="Stop mining after this many seconds and finish the run normally")
    p.add_argument("--stop-after-promoted", type=int, default=None,
                   help="In a sweep, stop scheduling further cells once this many factors were promoted")
    p.add_argument("--max-mem-mb", type=float, default=None,
                   help="Per-candidate memory ceiling; an over-budget candidate is abandoned")
    # E3: which factors walk-forward validates.
    p.add_argument("--wf-scope", default=None, choices=["promoted", "all"],
                   help="Walk-forward only the promoted factors (default) or all candidates")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Run a complete, reproducible FactorMiner experiment"
    )
    _add_common(p)
    return p.parse_args(argv)


def _load_config(args) -> dict:
    try:
        with open(args.config, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read config: {exc}", file=sys.stderr)
        raise SystemExit(1)


def _apply_cli(config: dict, args) -> dict:
    from core.config.overrides import OverrideError, apply_overrides, format_applied

    overrides = list(getattr(args, "overrides", None) or [])
    if getattr(args, "stop_after_candidates", None):
        overrides.append(
            f"experiment.stop.max_candidates={int(args.stop_after_candidates)}"
        )
    if getattr(args, "max_wallclock", None):
        overrides.append(
            f"experiment.stop.max_wallclock={int(args.max_wallclock)}"
        )
    if getattr(args, "max_mem_mb", None):
        overrides.append(f"evaluation.max_mem_mb={float(args.max_mem_mb)}")
    if overrides:
        try:
            config, applied = apply_overrides(config, overrides)
        except OverrideError as exc:
            print(f"experiment: {exc}", file=sys.stderr)
            raise SystemExit(2)
        if applied:
            print("config overrides:")
            print(format_applied(applied))

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
    if getattr(args, "wf_scope", None):
        exp.setdefault("walk_forward", {})["scope"] = args.wf_scope

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
    return config


def _current_n_trials() -> int:
    try:
        from core.experiment import budget

        return int(budget.status().get("n_trials") or 0)
    except Exception:  # pragma: no cover - estimate only
        return 0


def run_experiment_cmd(args):
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = _apply_cli(_load_config(args), args)

    if getattr(args, "dry_run", False):
        from core.experiment.estimate import estimate, format_estimate

        n_trials = _current_n_trials()
        sweep_path = getattr(args, "sweep", None)
        if sweep_path:
            from core.experiment.sweep import (
                SweepError, describe, expand, load_sweep, select_shard,
            )

            try:
                cells = select_shard(
                    expand(config, load_sweep(sweep_path)),
                    getattr(args, "shard", None),
                )
            except SweepError as exc:
                print(f"experiment --sweep: {exc}", file=sys.stderr)
                raise SystemExit(2)
            if not cells:
                print("experiment --sweep: this shard is empty", file=sys.stderr)
                raise SystemExit(2)
            print(describe(cells))
            total = 0.0
            payloads = []
            for cell in cells:
                payload = estimate(cell["config"], n_trials=n_trials)
                payloads.append({"cell": cell["label"], "family": cell["family"], **payload})
                total += float(payload["minutes_est"])
                print()
                print(f"--- cell {cell['label']} (family={cell['family']}) ---")
                print(format_estimate(payload, args.config))
            print(f"\nsweep total: {len(cells)} cell(s) x ~{total/len(cells):.1f} min "
                  f"= ~{total:.1f} min of mining")
            if getattr(args, "json", False):
                print(json.dumps(payloads, indent=2, ensure_ascii=False, default=str))
            return {"dry_run": True, "sweep": True, "cells": payloads}

        payload = estimate(config, n_trials=n_trials)
        print(format_estimate(payload, args.config))
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return {"dry_run": True, "estimate": payload}

    if getattr(args, "preflight", False):
        from core.commands import doctor as doctor_cmd

        rows = doctor_cmd.run_checks(
            args.config,
            data_root=str((config.get("data_feeds") or {}).get("data_root")),
            user_dir=(config.get("experiment") or {}).get("user_dir", "user_workspace"),
            miner=config.get("paradigm"),
        )
        doctor_cmd._print_table(rows)
        if any(r["status"] == doctor_cmd.FATAL for r in rows):
            print("\npreflight failed: fix the FATAL checks above.", flush=True)
            raise SystemExit(1)

    return _run_cells(config, args)


def _run_cells(config: dict, args) -> dict:
    from pathlib import Path

    from core.experiment.pipeline import run_experiment

    sweep_path = getattr(args, "sweep", None)
    if not sweep_path:
        return _run_one(config, args, run_experiment, Path)

    from core.experiment.sweep import SweepError, describe, expand, load_sweep, select_shard

    try:
        sweep = load_sweep(sweep_path)
        cells = expand(config, sweep)
        cells = select_shard(cells, getattr(args, "shard", None))
    except SweepError as exc:
        print(f"experiment --sweep: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not cells:
        print("experiment --sweep: this shard is empty", file=sys.stderr)
        raise SystemExit(2)
    print(describe(cells), flush=True)

    results = []
    target_promotions = getattr(args, "stop_after_promoted", None)
    promoted_so_far = 0
    for cell in cells:
        if target_promotions and promoted_so_far >= int(target_promotions):
            print(
                f"\nstop-after-promoted={target_promotions} reached "
                f"({promoted_so_far} promotion(s)); skipping the remaining "
                f"{len(cells) - len(results)} cell(s).",
                flush=True,
            )
            break
        print(f"\n=== SWEEP CELL {cell['label']} (family={cell['family']}) ===", flush=True)
        try:
            one = _run_one(cell["config"], args, run_experiment, Path, label=cell["label"])
            results.append(one)
            promoted_so_far += int(
                ((one or {}).get("state") or {}).get("mining", {}).get("n_promoted") or 0
            )
        except SystemExit as exc:
            results.append({"label": cell["label"], "error": f"exit {exc.code}"})
    promoted = sum(
        int(((r or {}).get("state") or {}).get("mining", {}).get("n_promoted") or 0)
        for r in results
    )
    print(f"\n=== SWEEP SUMMARY: {len(results)} cell(s), {promoted} promotion(s) ===")
    for r in results:
        print(f"  {r.get('label')}: {r.get('run_id') or r.get('error')}")
    return {"sweep": True, "cells": results, "n_promoted": promoted}


def _run_one(config, args, run_experiment, Path, label=None) -> dict:
    result = run_experiment(
        config,
        config_path=args.config,
        # Honour --run-id for a single run; a sweep must not collide, so its
        # cells always get generated ids.
        run_id=None if label else getattr(args, "run_id", None),
        resume=bool(args.resume),
        root=Path(args.output_root) if args.output_root else None,
        progress=lambda msg: print(msg, flush=True),
    )
    state = result["state"]
    mining = state.get("mining", {})
    wf = state.get("validated", {})
    bt = state.get("backtested", {}).get("summary", {})
    funnel = mining.get("funnel") or {}
    print("\n=== FACTORMINER EXPERIMENT ===")
    if label:
        print(f"  cell         : {label}")
    print(f"  run_id       : {result['run_id']}")
    print(f"  run_dir      : {result['run_dir']}")
    print(f"  candidates   : {mining.get('n_candidates')} -> {len(mining.get('factor_ids', []))} stored")
    if funnel:
        print(
            f"  stage-a      : {funnel.get('n_survivors')}/{funnel.get('n_input')} "
            f"reached the null ({funnel.get('reasons') or 'none rejected'})"
        )
    print(f"  promoted     : {mining.get('n_promoted')}")
    print(f"  val reads    : {mining.get('val_read_count')}")
    print(f"  wf folds     : {wf.get('n_folds')} (mean RankIC {wf.get('mean_rank_ic')}, refit/fold {wf.get('refit_per_fold')})")
    print(f"  wf scope     : {wf.get('scope')} ({wf.get('n_factors')} factor(s))")
    print(f"  backtest     : scope={bt.get('scope')} return={bt.get('total_return_pct')}% sharpe={bt.get('sharpe')}")
    print(f"  funding      : {bt.get('funding_source')}")
    print(f"  report       : {result['run_dir']}/report.md")
    if label:
        result = dict(result)
        result["label"] = label
    return result


if __name__ == "__main__":
    run_experiment_cmd(parse_args())
