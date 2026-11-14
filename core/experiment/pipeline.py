"""P5: the one-command, eight-stage factor-mining experiment.

    pre-register -> data quality -> mine -> gates -> walk-forward
    -> combine -> OOS recompute -> backtest -> report/archive

Design constraints (from ``platform_maturity_gap.md``):

* every run gets a reproducible identity (config/code/operator/data/seed) and
  an isolated output directory, so it can never pollute the shared factor_db;
* the strict holdout window is never touched -- the validation split is
  recomputed into a separate OOS value store and the signal orientation is
  learned from the in-sample store only;
* every stage is recorded in the run registry; ``--resume`` skips finished
  stages (stage-level, not epoch-level checkpointing);
* failures are written to ``status.json`` with the stage name, not swallowed.
"""

from __future__ import annotations

import copy
import glob
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.experiment import budget, costs, manifest as mf
from core.experiment import registry
from core.experiment import universe as uni

logger = logging.getLogger(__name__)

STAGES = (
    "data_checked",
    "mining",
    "gated",
    "validated",
    "combined",
    "oos_prepared",
    "backtested",
    "completed",
)


def _windows(config: Dict[str, Any]) -> Dict[str, Optional[str]]:
    periods = ((config.get("data_feeds") or {}).get("test_period") or [])
    if not periods:
        return {"start": None, "end": None}
    first = periods[0]
    return {"start": str(first[0]), "end": str(first[-1])}


def _stage_mining(work: Dict[str, Any], client, log: Callable[[str], None]):
    from core.miner.director import FactorMinerDirector
    from core.startup_validation import validate_mining_startup
    from core.utils.dynamic_loader import load_user_modules

    report = load_user_modules(work.get("experiment", {}).get("user_dir", "user_workspace"))
    validate_mining_startup(work, report)
    director = FactorMinerDirector(work, client)
    candidates = director.run(max_iterations=int(work.get("max_iterations", 5)))
    factor_ids = list(director.get_latest_factor_ids())
    verdicts = []
    for fid, cand in zip(factor_ids, candidates):
        m = dict(getattr(cand, "metrics", {}) or {})
        verdicts.append({
            "factor_id": fid,
            "status": m.get("lifecycle_status"),
            "fitness_score": m.get("fitness_score"),
            "hac_t": m.get("hac_t"),
            "p_adj": m.get("p_adj"),
            "dsr": m.get("dsr"),
            "empirical_p": m.get("empirical_p"),
            "gate_checks": m.get("gate_checks"),
            "cost_adj_return": m.get("cost_adj_return"),
            "rank_ic_oos": m.get("RankIC_oos"),
            "n_trials": m.get("n_trials"),
        })
    log(f"mining: {len(candidates)} candidates, {len(factor_ids)} factors stored")
    return {
        "n_candidates": len(candidates),
        "factor_ids": factor_ids,
        "verdicts": verdicts,
        "n_promoted": sum(1 for v in verdicts if v["status"] == "INSPECTED"),
        "val_read_count": int(getattr(director, "val_read_count", 0)),
        "hypothesis_budget": dict(getattr(director, "hypothesis_budget", {}) or {}),
        "budget_snapshot": dict(getattr(director, "budget_snapshot", {}) or {}),
        "director": director,
        "candidates": candidates,
    }


def _stage_combine(db_root: str) -> Dict[str, Any]:
    combos: List[Dict[str, Any]] = []
    for f in sorted(glob.glob(os.path.join(db_root, "metadata", "*.json"))):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        members = d.get("member_factor_ids") or (d.get("logic_reference") or {}).get("member_factor_ids")
        if members or "combination" in str(d.get("miner_type", "")).lower():
            combos.append({
                "factor_id": d.get("factor_id"),
                "miner_type": d.get("miner_type"),
                "member_factor_ids": members,
                "weights": (d.get("logic_reference") or {}).get("weights"),
            })
    return {"n_combination_factors": len(combos), "combinations": combos}


def _stage_oos_recompute(work, client, director, candidates, factor_ids, oos_root,
                         client_oos=None):
    """Recompute factor values on the validation window into an isolated store.

    ``client_oos`` is the *reserved-universe* data client. When a universe
    holdout is configured, factor values are recomputed on symbols that never
    entered mining; orientation still comes from the in-sample store, so no
    information flows back from the validation assets.
    """
    from core.storage.factor_storage import LocalFactorStorage

    out = LocalFactorStorage(oos_root)
    in_root = work["storage"]["db_root"]
    src_meta, dst_meta = Path(in_root) / "metadata", Path(oos_root) / "metadata"
    if src_meta.exists():
        for f in src_meta.glob("*.json"):
            shutil.copy2(f, dst_meta / f.name)
    evaluate_client = client_oos or client
    data = evaluate_client.get_test_data()
    returns = evaluate_client.get_test_returns()
    written = []
    for fid, cand in zip(factor_ids, candidates):
        try:
            values = cand.compute(data)
            frame = LocalFactorStorage._snapshot_frame(values, returns)
            if frame.empty:
                continue
            frame.to_parquet(Path(oos_root) / "values" / f"{fid}.parquet", index=False)
            written.append(fid)
        except Exception as exc:
            logger.warning("OOS recompute failed for %s: %s", fid, exc)
    return {
        "oos_root": oos_root,
        "written": written,
        "n_written": len(written),
        "orientation_db_root": in_root,
        "universe_role": "holdout" if client_oos is not None else "shared",
    }


def _report_text(rid: str, manifest: Dict[str, Any], state: Dict[str, Any]) -> str:
    m = state.get("mining", {})
    wf = state.get("validated", {})
    bt = state.get("backtested", {})
    summary = bt.get("summary", {}) if isinstance(bt, dict) else {}
    lines = [
        f"# FactorMiner experiment report — `{rid}`",
        "",
        f"- Created: {manifest.get('created_at')}",
        f"- Paradigm: `{manifest.get('paradigm')}` · timeframe `{manifest.get('timeframe')}`",
        f"- Config hash: `{manifest.get('config_hash')}` · git `{manifest.get('git_sha')}`",
        f"- Operator set: `{manifest.get('operator_set_hash')}` · data `{manifest.get('data_snapshot', {}).get('hash')}`",
        f"- Seed: `{manifest.get('seed')}` (LLM runs remain non-deterministic)",
        f"- Mine: {manifest.get('windows', {}).get('mine')} · validation: {manifest.get('windows', {}).get('test')}",
        "",
        "## Pipeline",
        "",
        "| Stage | Result |",
        "|---|---|",
        f"| Data quality | `{state.get('data_checked', {}).get('ok')}` — {len(state.get('data_checked', {}).get('warnings', []))} warning(s) |",
        f"| Mining | {m.get('n_candidates', 0)} candidates → {len(m.get('factor_ids', []))} stored factors |",
        f"| Promotion gates | {m.get('n_promoted', 0)} promoted; val reads = {m.get('val_read_count', 0)} |",
        f"| Walk-forward | {wf.get('n_folds', 0)} folds, refit per fold = `{wf.get('refit_per_fold')}`, mean RankIC {wf.get('mean_rank_ic')}, all folds positive = `{wf.get('all_folds_positive')}` |",
        f"| Combination | {state.get('combined', {}).get('n_combination_factors', 0)} combination factor(s) |",
        f"| OOS recompute | {state.get('oos_prepared', {}).get('n_written', 0)} factor value snapshot(s) on validation window |",
        f"| Backtest | scope {summary.get('scope')}, total return {summary.get('total_return_pct')}%, Sharpe {summary.get('sharpe')}, funding `{summary.get('funding_source')}` |",
        "",
        "## Statistical discipline",
        "",
        f"- Cumulative trial denominator at start: `{manifest.get('n_trials_at_start')}`",
        f"- Hypothesis budget: `{m.get('hypothesis_budget', {}).get('cumulative_tests')}` cumulative tests; DSR bar `{m.get('hypothesis_budget', {}).get('bar_per_period_sharpe')}` per period",
        f"- Cost identity: `{state.get('cost_identity', {}).get('identity')}` → consistent = `{state.get('cost_identity', {}).get('consistent')}`",
        f"- Holdout guard: `{state.get('holdout', {}).get('mode')}`, revealed windows = {state.get('holdout', {}).get('n_revealed')}",
        f"- Empirical target-permutation null: computed for {sum(1 for v in m.get('verdicts', []) if v.get('empirical_p') is not None)}/{len(m.get('verdicts', []))} candidates",
    ]
    uni_state = state.get("universe") or {}
    if uni_state.get("enabled"):
        lines.append(
            f"- Universe holdout: `{uni_state.get('source')}`, "
            f"train {uni_state.get('n_train')} / reserved {uni_state.get('n_holdout')} assets, "
            f"clean = `{uni_state.get('clean')}`, "
            f"OOS recompute role = `{state.get('oos_prepared', {}).get('universe_role')}`, "
            f"reused = `{uni_state.get('guard', {}).get('reused')}`"
        )
    lines += [
        "",
        "## Limitations (honest by design)",
        "",
        "- This run evaluates the **validation** window only; the one-time strict holdout was not touched.",
        "- A green pipeline means the process is reproducible and auditable, NOT that a profitable factor was found.",
        "- Positive out-of-sample evidence requires the rolling pre-registered "
        "windows in `user_workspace/experiment_window.json` (30-bar windows from "
        "the day after the last revealed holdout, 5-bar embargo, each revealed once).",
        "- `--resume` is stage-level: it skips completed stages, it does not restore an epoch-level GP population.",
    ]
    if uni_state.get("enabled") and not uni_state.get("clean"):
        lines.append(
            "- **Universe holdout is NOT clean**: the reserved symbols are a subset of "
            "the mining universe and their histories were already read. This run "
            "verifies the plumbing, not independent generalisation. Download "
            "disjoint fresh symbols for a real universe holdout."
        )
    lines.append("")
    return "\n".join(lines)


def run_experiment(
    config: Dict[str, Any],
    *,
    config_path: Optional[str] = None,
    run_id: Optional[str] = None,
    resume: bool = False,
    root: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Execute the full experiment. Returns the run result dict."""
    log = progress or (lambda msg: logger.info(msg))
    original = copy.deepcopy(config)
    # Reserve the validation universe BEFORE any stage sees a data client.
    # ``work`` is the train-only config; ``original`` keeps the pre-registered
    # split declaration for the manifest / config_hash.
    split = uni.resolve_split(original)
    work = uni.train_config(original, split) if split.enabled else copy.deepcopy(config)
    work.setdefault("experiment", {})
    work.setdefault("storage", {})
    work["experiment"]["user_dir"] = original.get("experiment", {}).get("user_dir", "user_workspace")

    # ---- pre-registration --------------------------------------------------
    config_hash = mf.hash_json(original)
    status: Dict[str, Any] = {}
    if resume:
        found = registry.find_resumable(config_hash, root=root)
        if found:
            status = registry.read_status(found, root=root)
            manifest_path = (
                Path(root) / found / "manifest.json" if root
                else registry.run_dir(found) / "manifest.json"
            )
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            rid = found
            log(f"resuming run {rid} at stage {status.get('stage')}")
    if not status:
        # Register user operators BEFORE fingerprinting the operator set.
        # Otherwise build_manifest() sees only the 59 builtins while the run
        # actually searches with builtin + user operators, and the final
        # reproducibility check would (correctly) flag a mismatch.
        try:
            from core.utils.dynamic_loader import load_user_modules
            load_user_modules(
                original.get("experiment", {}).get("user_dir", "user_workspace")
            )
        except Exception as exc:  # pragma: no cover - defensive
            log(f"user module preload skipped: {exc}")
        base, _, _ = budget.resolve(0, [], [])
        manifest = mf.build_manifest(
            original,
            run_id=run_id,
            config_path=config_path,
            n_trials_at_start=base,
        )
        rid = manifest["run_id"]
        path = registry.create_run(manifest, original, config_path=config_path, root=root)
        status = registry.read_status(rid, root=root)
        log(f"pre-registered run {rid} ({path})")
    else:
        path = registry.run_dir(rid)
        if root:
            path = Path(root) / rid

    work["experiment"]["manifest"] = manifest
    if manifest.get("seed") is not None:
        work["experiment"]["seed"] = manifest["seed"]
    db_root = str(path / "factor_db")
    work["storage"]["db_root"] = db_root
    completed = set(status.get("completed_stages", []))
    state: Dict[str, Any] = {}
    state_file = path / "state.json"
    if resume and state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    def stage(name: str, fn: Callable[[], Any]) -> Any:
        if resume and name in completed and name in state:
            log(f"[skip] {name} (already completed)")
            return state[name]
        log(f"[stage] {name}")
        try:
            result = fn()
        except Exception as exc:
            registry.update_status(rid, name, status="failed", error=str(exc), root=root)
            raise
        state[name] = result
        registry.update_status(rid, name, root=root)
        registry.write_artifact(rid, "state.json", _jsonable(state), root=root)
        return result

    # ---- holdout discipline ------------------------------------------------
    from core.evaluation import holdout_guard

    holdout_state = {
        "mode": holdout_guard.resolve_mode(work),
        "n_revealed": 0,
    }
    try:
        from core.evaluation.experiment_trace import HoldoutLedger
        holdout_state["n_revealed"] = len(HoldoutLedger(holdout_guard.default_ledger_path()).snapshot())
    except Exception:
        pass
    holdout_guard.guard_config(work, source=f"pipeline:{rid}")
    state["holdout"] = holdout_state
    state["cost_identity"] = costs.resolve_cost_params(work)

    # ---- universe (asset-dimension) holdout --------------------------------
    universe_state = split.summary()
    if split.enabled:
        universe_state["guard"] = uni.guard(split, source=f"pipeline:{rid}")
        log(f"universe holdout: train={len(split.train_pairs)} "
            f"reserved={len(split.holdout_pairs)} clean={split.clean}")
    state["universe"] = universe_state

    # ---- data clients ------------------------------------------------------
    from core.data_feed.real_client import RealDataClient

    client: Optional[RealDataClient] = None
    client_oos: Optional[RealDataClient] = None

    def do_data():
        nonlocal client, client_oos
        from core.data_feed.panel_validator import validate_panel
        client = RealDataClient(work)
        data, returns = client.get_data(), client.get_returns()
        report = validate_panel(
            data, returns,
            timeframe=str(work.get("data_feeds", {}).get("timeframe", "1d")),
            quality_cfg=work.get("data_quality"),
        )
        out = report.as_dict()
        if split.enabled:
            client_oos = RealDataClient(uni.holdout_config(original, split))
            try:
                oos_report = validate_panel(
                    client_oos.get_test_data(), client_oos.get_test_returns(),
                    timeframe=str(work.get("data_feeds", {}).get("timeframe", "1d")),
                    quality_cfg=work.get("data_quality"),
                ).as_dict()
                out["holdout_universe"] = {
                    "ok": oos_report.get("ok"),
                    "warnings": oos_report.get("warnings", []),
                    "n_assets": len(split.holdout_pairs),
                }
            except Exception as exc:  # a missing reserved symbol must be loud
                logger.warning("Holdout universe data check failed: %s", exc)
                out["holdout_universe"] = {
                    "ok": False, "error": str(exc),
                    "n_assets": len(split.holdout_pairs),
                }
        return out

    stage("data_checked", do_data)

    def do_mining():
        raw = _stage_mining(work, client, log)
        # Keep runtime objects out of the persisted state.
        state["_runtime"] = {
            "director": raw.pop("director", None),
            "candidates": raw.pop("candidates", None),
        }
        return raw

    mining = stage("mining", do_mining)
    state["gated"] = stage("gated", lambda: {
        "n_promoted": mining.get("n_promoted", 0),
        "val_read_count": mining.get("val_read_count", 0),
        "verdicts": mining.get("verdicts", []),
        "hypothesis_budget": mining.get("hypothesis_budget", {}),
    })

    def do_wf():
        from core.experiment.walk_forward import run_walk_forward
        return run_walk_forward(db_root, mining.get("factor_ids", []), work)

    stage("validated", do_wf)
    stage("combined", lambda: _stage_combine(db_root))

    runtime = state.get("_runtime") or {}
    director, candidates = runtime.get("director"), runtime.get("candidates")

    def do_oos():
        if director is None or candidates is None:
            return {"n_written": 0, "note": "resume skipped mining; OOS recompute unavailable"}
        return _stage_oos_recompute(
            work, client, director, candidates,
            mining.get("factor_ids", []), str(path / "factor_db_oos"),
            client_oos=client_oos,
        )

    oos = stage("oos_prepared", do_oos)

    def do_backtest():
        from core.backtest.backtest_engine import run_backtest
        ids = oos.get("written") or []
        if not ids:
            return {"skipped": True, "reason": "no OOS factor values"}
        win = _windows(work)
        cp = state["cost_identity"]
        out = run_backtest(
            ids,
            db_root=oos["oos_root"],
            orientation_db_root=oos["orientation_db_root"],
            start=win["start"],
            end=win["end"],
            commission_rate=cp["commission_rate"],
            slippage_rate=cp["slippage_rate"],
            use_real_funding=True,
        )
        return {"summary": out["summary"]}

    stage("backtested", do_backtest)

    def do_report():
        report = _report_text(rid, manifest, state)
        registry.write_artifact(rid, "report.md", report, root=root)
        registry.write_artifact(rid, "metrics.json", _jsonable(state), root=root)
        registry.write_artifact(rid, "walk_forward.json", _jsonable(state.get("validated", {})), root=root)
        mf_manifest = dict(manifest)
        mf_manifest["n_trials_at_end"] = budget.status().get("n_trials", 0)
        mf_manifest["reproducibility"] = mf.verify_manifest(manifest, original)
        registry.write_artifact(rid, "manifest_final.json", mf_manifest, root=root)
        return {"report": "report.md", "metrics": "metrics.json"}

    stage("completed", do_report)
    registry.update_status(rid, "completed", root=root)
    log(f"experiment complete: {path}")
    return {
        "run_id": rid,
        "run_dir": str(path),
        "status": "completed",
        "stages": list(STAGES),
        "state": _jsonable({k: v for k, v in state.items() if k != "_runtime"}),
    }


def _jsonable(obj: Any) -> Any:
    """Drop live objects from the persisted state."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if not k.startswith("_runtime")}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)
