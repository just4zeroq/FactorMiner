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


def _oos_warm_panel(mine, test, warmup_bars: int):
    """Prepend the tail of ``mine`` to ``test`` for warmup-only computation.

    P1-M7: a 20-bar rolling factor evaluated on a cold validation window loses
    its first 20 bars. We compute on ``[mine.tail(warmup) ; test]`` and then
    slice back to ``test.index`` so no in-sample bar leaks into the OOS output.
    Best-effort: any failure falls back to the unwarmed ``test`` panel.
    """
    if warmup_bars <= 0 or mine is None or test is None:
        return test
    try:
        import pandas as _pd

        n = int(warmup_bars)
        if isinstance(test, _pd.DataFrame) and isinstance(mine, _pd.DataFrame):
            if test.empty or mine.empty:
                return test
            warm = _pd.concat([mine.tail(n), test])
            return warm[~warm.index.duplicated(keep="last")].sort_index()
        # cross_asset mode returns a dict of feature panels, not one DataFrame.
        if isinstance(test, dict) and isinstance(mine, dict):
            out = {}
            for key, frame in test.items():
                base = mine.get(key)
                if (
                    isinstance(frame, _pd.DataFrame)
                    and isinstance(base, _pd.DataFrame)
                    and not frame.empty
                    and not base.empty
                ):
                    warm = _pd.concat([base.tail(n), frame])
                    out[key] = warm[~warm.index.duplicated(keep="last")].sort_index()
                else:
                    out[key] = frame
            return out
    except Exception as exc:  # pragma: no cover - best-effort warmup
        logger.debug("OOS warmup unavailable: %s", exc)
    return test


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
    # P1-M7: rolling operators (ts_mean/ts_std/...) need history before the
    # validation window, otherwise their first `lookback` bars are NaN and the
    # OOS sample is silently truncated. Warm the computation panel, then slice
    # back to the true test window so no in-sample bar leaks into OOS metrics.
    def _index_of(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                idx = getattr(v, "index", None)
                if idx is not None:
                    return idx
            return None
        return getattr(obj, "index", None)

    test_index = _index_of(data)
    warmup_bars = int(
        ((work.get("data_feeds", {}) or {}).get("test_warmup_bars", 0) or 0)
    )
    warmup_data = _oos_warm_panel(
        evaluate_client.get_data() if warmup_bars > 0 else None,
        data,
        warmup_bars,
    )

    written = []
    leaked: List[str] = []
    # X-01: without a verifiable test index, warmup rows cannot be removed. The
    # old ``except: pass`` wrote the whole warmed panel (including in-sample
    # bars) into the OOS store while claiming "no in-sample bar leaks".
    if warmup_bars > 0 and test_index is None:
        raise RuntimeError(
            "test_warmup_bars > 0 but the validation panel has no index; "
            "cannot slice warmup rows out of the OOS snapshot."
        )
    for fid, cand in zip(factor_ids, candidates):
        try:
            values = cand.compute(warmup_data)
            if test_index is not None and hasattr(values, "loc"):
                try:
                    values = values.loc[values.index.intersection(test_index)]
                except Exception as exc:
                    leaked.append(fid)
                    raise RuntimeError(
                        f"OOS warmup slice failed for {fid}: {exc}"
                    ) from exc
                stray = values.index.difference(test_index)
                if len(stray):
                    leaked.append(fid)
                    raise RuntimeError(
                        f"OOS snapshot for {fid} still holds {len(stray)} "
                        "in-sample warmup rows"
                    )
            frame = LocalFactorStorage._snapshot_frame(values, returns)
            if frame.empty:
                continue
            frame.to_parquet(Path(oos_root) / "values" / f"{fid}.parquet", index=False)
            written.append(fid)
        except Exception as exc:
            logger.warning("OOS recompute failed for %s: %s", fid, exc)
    if leaked:
        raise RuntimeError(
            "Refusing to publish OOS snapshots: warmup/in-sample rows could not "
            f"be excluded for {len(leaked)} factor(s): {', '.join(leaked[:8])}"
        )
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
    # X-02: user operators must be registered on BOTH paths. The resume branch
    # used to skip this, so ``operator_set_hash`` would be computed from the
    # builtin set alone and either falsely flag or falsely pass verification.
    try:
        from core.utils.dynamic_loader import load_user_modules
        load_user_modules(
            original.get("experiment", {}).get("user_dir", "user_workspace")
        )
    except Exception as exc:  # pragma: no cover - defensive
        log(f"user module preload skipped: {exc}")

    config_hash = mf.hash_json(original)
    status: Dict[str, Any] = {}
    resume_verification: Optional[Dict[str, Any]] = None
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
            # X-02: a resumed run reuses cached factor values. If the config,
            # code revision, operator set or data snapshot changed, those values
            # belong to a different experiment and must NOT be reused.
            resume_verification = mf.verify_manifest(manifest, original)
            if not resume_verification["reproducible"]:
                detail = ", ".join(
                    list(resume_verification["mismatches"])
                    + [f"missing:{k}" for k in resume_verification["missing"]]
                ) or "unknown"
                raise RuntimeError(
                    f"Run {rid} is not reproducible under the current code/data "
                    f"({detail}). Refusing to resume and reuse cached factor "
                    "values. Start a fresh run instead (drop --resume)."
                )
            log(f"resuming run {rid} at stage {status.get('stage')} "
                f"(manifest verified: {config_hash[:12]})")
    if not status:
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
    # X-02 / G-05: re-apply the declared seed on the resume path too (a fresh
    # process starts with fresh global RNG state), and record that it actually
    # happened rather than merely being declared in config.
    applied_seed = mf.seed_everything(manifest.get("seed"))
    manifest["seed_applied"] = applied_seed is not None
    work["experiment"]["resume_verified"] = bool(resume_verification)
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
    if resume_verification:
        state["manifest_verification"] = resume_verification
        registry.save_manifest(rid, manifest, root=root)

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
    # X-03: remember the one-time guard results from a previous attempt before
    # refreshing the in-memory state, so ``--resume`` does not re-burn a window
    saved_holdout_guard = (state.get("holdout") or {}).get("guard_results")
    saved_universe_guard = (state.get("universe") or {}).get("guard")
    state["holdout"] = holdout_state
    cost_identity = costs.resolve_cost_params(work)
    state["cost_identity"] = cost_identity
    if not cost_identity.get("consistent"):
        # X-04: ``consistent=False`` used to be recorded and then ignored, so a
        # factor could pass the gate on one cost basis and lose money in the
        # backtest on another. Keep the run alive but make it unmissable.
        log(
            "COST MISMATCH: gate_cost_rate={gate} but 2*(commission+slippage)="
            "{implied} (delta={delta}). Gate and backtest are judging different "
            "economics; fix fitness.gate_cost_rate or backtest.* before trusting "
            "any promotion verdict.".format(**cost_identity)
        )

    # ---- universe (asset-dimension) holdout --------------------------------
    universe_state = split.summary()
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

    # X-03 / K-05: one-time reveals must happen AFTER data validation (a run
    # that fails on bad data must not burn its window), must be recorded, and
    # must be skipped on resume. Both guards used to run unconditionally before
    # any stage, so a resume hit ``UniverseReuseError`` with status.json still
    # showing ``created``.
    if saved_holdout_guard is not None and resume:
        holdout_state["guard_results"] = saved_holdout_guard
        log("[skip] holdout guard (one-time reveal already recorded)")
    else:
        try:
            guard_results = holdout_guard.guard_config(work, source=f"pipeline:{rid}")
        except Exception as exc:
            registry.update_status(rid, "data_checked", status="failed",
                                   error=f"holdout guard: {exc}", root=root)
            raise
        holdout_state["guard_results"] = _jsonable(guard_results)
        registry.write_artifact(rid, "state.json", _jsonable(state), root=root)

    if split.enabled:
        if saved_universe_guard is not None and resume:
            universe_state["guard"] = saved_universe_guard
            log("[skip] universe holdout guard (already recorded)")
        else:
            try:
                universe_state["guard"] = uni.guard(split, source=f"pipeline:{rid}")
            except Exception as exc:
                registry.update_status(rid, "data_checked", status="failed",
                                       error=f"universe holdout guard: {exc}",
                                       root=root)
                raise
            log(f"universe holdout: train={len(split.train_pairs)} "
                f"reserved={len(split.holdout_pairs)} clean={split.clean}")
            registry.write_artifact(rid, "state.json", _jsonable(state), root=root)

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
        # X-07: ``n_folds=0`` is not a validation. Fail the stage loudly
        # instead of writing a green ``validated`` record into state.json.
        outcome = run_walk_forward(db_root, mining.get("factor_ids", []), work)
        if int(outcome.get("n_folds", 0) or 0) <= 0:
            raise RuntimeError(
                "Walk-forward produced 0 folds (not enough bars for the "
                "configured purge/embargo/min_train); refusing to mark the "
                "experiment as validated. Widen the mining window or reduce "
                "min_train_bars / embargo_bars."
            )
        return outcome

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
        # P1-M3: align the backtest's rebalance cadence with the promotion gate
        # (which judges a 1-bar book by default). Backtest used to fall back to
        # its internal 5-bar default while the gate assumed 1 bar, so the gate
        # penalised high-turnover factors that the backtest then executed at a
        # 5x cheaper cadence — an inconsistent economic yardstick.
        gate_cfg = (work.get("evaluation", {}) or {}).get("gates", {}) or {}
        rebalance_every = int(
            work.get("rebalance_every")
            or gate_cfg.get("gate_rebalance_bars")
            or 1
        )
        out = run_backtest(
            ids,
            db_root=oos["oos_root"],
            orientation_db_root=oos["orientation_db_root"],
            start=win["start"],
            end=win["end"],
            commission_rate=cp["commission_rate"],
            slippage_rate=cp["slippage_rate"],
            rebalance_every=rebalance_every,
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
