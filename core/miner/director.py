import logging
from typing import Dict, Any, List

from core.miner.entities import FactorMetadata, EvaluationFeedback
from core.storage.factor_storage import get_global_storage
from core.evaluation.evaluator import ParallelEvaluator
from core.evaluation.annualisation import resolve_annualisation
from core.evaluation.significance import apply_promotion_gates
from core.evaluation.experiment_trace import (
    ExperimentTrace,
    FdrLedger,
    HoldoutLedger,
    HoldoutReuseError,
    TrialRecord,
    signature_of,
)
from core.miner.paradigms.base import BaseFactorMiner

logger = logging.getLogger(__name__)

class FactorMinerDirector:
    """
    因子挖掘的全局网关 Facade
    负责组装数据源、配置、评价器，并驱动底层具体流派的 Miner 跑完闭环。
    """
    def __init__(self, config: Dict, data_client: Any):
        from core.startup_validation import normalize_deprecated_miner

        normalize_deprecated_miner(config)
        self.config = config
        self.data_client = data_client
        # Honor a per-run storage root if configured; otherwise the default root.
        _db_root = self.config.get("storage", {}).get("db_root")
        self.storage_client = get_global_storage(_db_root)
        
        # 1. 初始化统一并行评价器
        self.evaluator = ParallelEvaluator(self.data_client, self.config)
        
        self.universe = config.get("universe", "DEFAULT_UNIVERSE")
        self.data_feeds = config.get("data_feeds", {})
        
        # 2. 实例化流派 Miner
        paradigm = config.get("paradigm", "GP")
        self.miner = self._instantiate_miner(paradigm, config, data_client)
        # 将统一的 evaluator 和 storage_client 注入到 miner 中
        self.miner.evaluator = self.evaluator
        self.miner.storage_client = self.storage_client

        # Phase 1: statistical-gate infrastructure (cross-run persistence).
        gate_cfg = self.config.get("evaluation", {}).get("gates", {})
        self.trace = ExperimentTrace(gate_cfg.get("trace_path"))
        self.holdout = HoldoutLedger(gate_cfg.get("holdout_path"))
        # Phase P1: cumulative BH-FDR needs the raw p-value of every hypothesis
        # ever tested, not just the ones that reached this round's short list.
        self.fdr_ledger = FdrLedger(gate_cfg.get("fdr_ledger_path"))

        self.latest_factor_ids: List[str] = []
        # Filled in by _save_excellent_factors; surfaced by /api/gates/budget.
        self.hypothesis_budget: Dict[str, Any] = {}

        # P1 reproducibility: the pipeline injects a built run manifest and an
        # explicit seed. Legacy callers (CLI mine / API launch) stay untouched
        # because both are optional and default to the previous behaviour.
        exp_cfg = self.config.get("experiment", {}) or {}
        self.manifest = exp_cfg.get("manifest")
        if exp_cfg.get("seed") is not None:
            from core.experiment.manifest import seed_everything
            seed_everything(exp_cfg["seed"])

    def _instantiate_miner(self, paradigm: str, config: Dict, data: Any) -> BaseFactorMiner:
        from core.miner.registry import MinerRegistry
        
        # 优先查询用户自定义注册的 Miner
        if paradigm in MinerRegistry._registry:
            miner_cls = MinerRegistry._registry[paradigm]
            return miner_cls(data, config)
            
        # Fallback 到系统原生的四大流派
        if paradigm == "GP":
            from .paradigms.gp_miner import GPFactorMiner
            return GPFactorMiner(data, config)
        elif paradigm == "LLM":
            from .paradigms.llm_miner import LLMFactorMiner
            return LLMFactorMiner(data, config)
        elif paradigm == "RL":
            from .paradigms.rl_miner import RLFactorMiner
            return RLFactorMiner(data, config)
        elif paradigm == "NN":
            raise ValueError(
                "NN miner implementation is not registered. Load user modules "
                "and use the bundled NN/MyCustomNN implementation."
            )
        else:
            raise ValueError(f"Unknown paradigm: {paradigm}. Did you forget to register it?")

    def run(self, max_iterations: int = 10, progress_callback=None) -> List[Any]:
        # P0-b: fail fast, BEFORE the miner reads a single holdout return.
        # Registering a final reveal up front is conservative on purpose: if the
        # run crashes, the window is still burned — better to waste a window
        # than to allow a second peek.
        self._enforce_holdout()
        logger.info(f"Starting FactorMiner loop for paradigm: {self.config.get('paradigm')}")
        
        # 委托给底层的通用范式引擎去跑 (它自带去重和生命周期)
        best_candidates = self.miner.mine(max_iterations, progress_callback=progress_callback)
        
        self._save_excellent_factors(best_candidates)
        self._print_results_table(best_candidates)

        logger.info("FactorMiner loop completed.")
        return best_candidates

    def _build_null_tstats(self, candidates, gate_cfg):
        """Per-candidate target-permutation |t| nulls for the P2 empirical gate.

        Returns ``None`` when the feature is off, so every existing run keeps
        its verdicts. Panels are nan-to-num'd on both sides so the null and the
        observed HAC t are computed under the same convention.
        """
        cfg = gate_cfg or {}
        n_perm = int(cfg.get("null_n_perm", 0) or 0)
        if n_perm <= 0 or cfg.get("empirical_p_max") is None:
            return None
        try:
            import numpy as np
            from core.evaluation.significance import target_permutation_tstats

            def _array(obj):
                if hasattr(obj, "to_numpy"):
                    arr = obj.to_numpy(dtype=float, na_value=np.nan)
                else:
                    arr = np.asarray(obj, dtype=float)
                arr = np.asarray(arr, dtype=float)
                if arr.ndim == 1:
                    arr = arr.reshape(-1, 1)
                return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)

            returns = _array(self.data_client.get_returns())
            data = self.data_client.get_data()
            seed = int((self.config.get("experiment") or {}).get("seed") or 0)
            out = []
            for cand in candidates:
                try:
                    panel = _array(cand.compute(data))
                    if panel.shape != returns.shape:
                        out.append([])
                        continue
                    out.append(
                        target_permutation_tstats(
                            panel, returns, n_perm=n_perm, seed=seed
                        ).tolist()
                    )
                except Exception as exc:  # pragma: no cover - one bad factor
                    logger.debug("Null distribution skipped for a candidate: %s", exc)
                    out.append([])
            return out
        except Exception as exc:
            logger.warning("Target-permutation null unavailable: %s", exc)
            return None

    def _save_excellent_factors(self, candidates):
        import uuid
        from datetime import datetime

        self.latest_factor_ids = []

        # Phase 1: batch promotion gates. BH-FDR across the candidate set is the
        # multiple-testing control that was missing before; only factors whose
        # gates are all green become INSPECTED, the rest stay DISCOVERED_UNVERIFIED.
        metrics_list = [(getattr(c, "metrics", None) or {}) for c in candidates]
        # DSR must be deflated by the *real* number of ideas tried: everything
        # recorded in the cross-run trace plus every candidate this run handed
        # to the evaluator (including the ones that never made the shortlist).
        evaluator = getattr(self.miner, "evaluator", None)
        raw_trials = getattr(evaluator, "total_evaluated", 0)
        run_trials = (
            int(raw_trials)
            if isinstance(raw_trials, (int, float)) and not isinstance(raw_trials, bool)
            else 0
        )
        if run_trials <= 0:
            run_trials = len(candidates)
        # P2: the portable budget snapshot keeps the multiple-testing denominator
        # honest across machines/clones even though the high-volume ledgers are
        # gitignored. It is monotonic, so a stale local ledger cannot reset it.
        from core.experiment import budget as budget_sync

        run_p_values = list(getattr(evaluator, "run_p_values", []) or [])
        base_trials, family, self.budget_snapshot = budget_sync.resolve(
            int(self.trace.num_trials),
            self.fdr_ledger.cumulative(),
            run_p_values,
        )
        n_trials = base_trials + run_trials
        # Every candidate handed to the evaluator reads the OOS/validation split.
        # This is the count AlphaMaster never recorded; surfacing it makes val
        # reuse a governed quantity instead of an invisible one.
        self.val_read_count = int(run_trials)
        gate_cfg = self.config.get("evaluation", {}).get("gates")
        # P2: turn the target-permutation null from a library helper into an
        # actual promotion gate (off unless null_n_perm + empirical_p_max are set).
        null_tstats = self._build_null_tstats(candidates, gate_cfg)
        gates = apply_promotion_gates(
            metrics_list,
            cfg=gate_cfg,
            n_trials=n_trials,
            family_p_values=family or None,
            null_tstats=null_tstats,
        )
        # Persist this run's hypotheses once; the ledger is idempotent per run.
        run_id = getattr(self, "_fdr_run_id", None)
        if not run_id:
            run_id = f"run_{uuid.uuid4().hex[:8]}"
            self._fdr_run_id = run_id
        self.fdr_ledger.append(
            run_p_values,
            run_id=run_id,
            meta={"n_candidates": len(candidates), "n_trials": n_trials},
        )
        try:
            budget_sync.sync(n_trials, run_p_values, increment_runs=True)
        except Exception as exc:  # pragma: no cover - reporting must not break a run
            logger.debug("Hypothesis budget snapshot skipped: %s", exc)
        if evaluator is not None and hasattr(evaluator, "run_p_values"):
            evaluator.run_p_values.clear()

        # Hypothesis budget: DSR's denominator is now a governed quantity. Log it
        # every run so "the gate is impossible" is visible as a budget problem
        # (too many trials) rather than a mystery.
        try:
            import statistics

            obs = [int(m.get("n_obs")) for m in metrics_list if m.get("n_obs")]
            budget_cfg = self.config.get("evaluation", {}).get("gates", {}).get("hypothesis_budget")
            self.hypothesis_budget = self.fdr_ledger.budget_report(
                n_obs=int(statistics.median(obs)) if obs else None,
                budget=budget_cfg,
                annualisation=resolve_annualisation(self.config),
            )
            logger.info(
                "Hypothesis budget: %d cumulative test(s) across %d run(s)%s; "
                "DSR bar = %.3f per-period SR (%.2f annualised)",
                self.hypothesis_budget["cumulative_tests"],
                self.hypothesis_budget["runs"],
                (
                    f" (budget {self.hypothesis_budget['budget']}, "
                    f"{self.hypothesis_budget['utilization']:.0%} used)"
                    if self.hypothesis_budget.get("budget")
                    else ""
                ),
                self.hypothesis_budget.get("bar_per_period_sharpe", float("nan")),
                self.hypothesis_budget.get("bar_annualised_sharpe", float("nan")),
            )
            utilization = self.hypothesis_budget.get("utilization")
            if utilization and utilization >= 0.8:
                logger.warning(
                    "Hypothesis budget is %.0f%% spent (%d tests). Every further "
                    "run raises the DSR bar for all future candidates — widen the "
                    "target horizon or shrink the search space instead.",
                    utilization * 100,
                    self.hypothesis_budget["cumulative_tests"],
                )
        except Exception as exc:  # pragma: no cover - reporting must not break a run
            logger.debug("Hypothesis budget report skipped: %s", exc)

        # Record EVERY attempt into the cross-run ExperimentTrace so num_trials
        # stays honest across runs (the denominator of truth).
        for cand, g in zip(candidates, gates):
            self._record_trial(cand, g)

        # Pre-allocate ids for the whole batch so a linear-combination factor can
        # reference the stored ids of its members. Without this the combination is
        # persisted as anonymous sub-ASTs and the backtest can only re-weight it by
        # guessing, which is how AlphaPool weights previously got flattened to an
        # equal-weight mean.
        preallocated_ids = [f"fac_{uuid.uuid4().hex[:8]}" for _ in candidates]
        object_ids = {id(cand): fid for cand, fid in zip(candidates, preallocated_ids)}

        for cand, g, factor_id in zip(candidates, gates, preallocated_ids):
            self.latest_factor_ids.append(factor_id)

            cand.metrics["p_adj"] = g["p_adj"]
            cand.metrics["dsr"] = g["dsr"]
            cand.metrics["n_trials"] = n_trials
            # P2: make the empirical-null verdict auditable. Without this the
            # gate ran but its result was invisible in factor_db / run reports.
            cand.metrics["empirical_p"] = g.get("empirical_p")
            cand.metrics["gate_pass"] = bool(g["gate"]["pass"])
            cand.metrics["gate_checks"] = {
                k: v for k, v in g["gate"].items() if k != "pass"
            }
            metadata = FactorMetadata(
                factor_id=factor_id,
                miner_type=self.config.get("paradigm"),
                user_id="CharlesJ-ABu",
                metrics=cand.metrics,
                logic_hash=getattr(cand, 'logic_hash', ""),
                generation_config={
                    key: self.config[key]
                    for key in ("max_iterations", "population_size", "top_k_factors", "hidden_dim")
                    if key in self.config
                },
                created_at=datetime.now().isoformat(),
                lifecycle_status=g["status"],
            )

            # 存储分流
            from core.miner.expressions import (
                FactorExpressionAST,
                FactorExpressionCode,
                FactorExpressionTensor,
                FactorExpressionAction,
                LinearCombinedFactor,
            )
            if isinstance(cand, FactorExpressionAST):
                self.storage_client.save_gp_factor(cand.get_source(), metadata)
            elif isinstance(cand, FactorExpressionCode):
                self.storage_client.save_llm_factor(
                    cand.get_source(),
                    cand.get_reflection_history(),
                    metadata,
                    provenance=cand.get_provenance(),
                )
            elif isinstance(cand, FactorExpressionAction):
                self.storage_client.save_rl_factor(cand.get_source(), b"", metadata)
            elif isinstance(cand, LinearCombinedFactor):
                logic = cand.to_dict()
                member_ids = [
                    object_ids.get(id(sub)) for sub in cand.sub_expressions
                ]
                if any(member_ids):
                    logic["member_factor_ids"] = member_ids
                self.storage_client.save_linear_combination_factor(logic, metadata)
            elif isinstance(cand, FactorExpressionTensor):
                src = cand.get_source()
                artifact_reference = {}
                artifact = cand.export_model_artifact()
                if artifact is not None:
                    try:
                        filename = self.storage_client.save_model_artifact(
                            src["model_version"],
                            artifact.payload,
                            artifact.extension,
                        )
                        artifact_reference = artifact.logic_reference(filename)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Failed to persist model artifact for "
                            f"{src['model_version']}: {exc}"
                        ) from exc
                else:
                    # Backward-compatible fallback for legacy custom miners.
                    weights = getattr(getattr(cand, "model_instance", None), "W", None)
                    if weights is not None:
                        try:
                            self.storage_client.save_model_weights(
                                src["model_version"], weights.tobytes()
                            )
                        except Exception as exc:
                            logger.warning(
                                "Failed to persist weights for model %s: %s",
                                src["model_version"], exc,
                            )
                self.storage_client.save_nn_factor_channel(
                    src["model_version"],
                    src["channel"],
                    metadata,
                    artifact_reference,
                )

            self._save_factor_snapshot(factor_id, cand)

    def _record_trial(self, cand, gate) -> None:
        """Persist one evaluated candidate into the cross-run ExperimentTrace."""
        try:
            payload = (
                cand.get_source()
                if callable(getattr(cand, "get_source", None))
                else cand
            )
            m = getattr(cand, "metrics", {}) or {}
            self.trace.record(
                TrialRecord(
                    signature=signature_of(payload),
                    fitness=float(m.get("fitness_score", float("nan"))),
                    rank_ic=float(m.get("RankIC", float("nan"))),
                    rank_ic_oos=float(m.get("RankIC_oos", float("nan"))),
                    significant=bool(gate["gate"]["pass"]),
                    meta={"p_adj": gate["p_adj"]},
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("trial record skipped: %s", exc)

    def _enforce_holdout(self) -> None:
        """P0-b: route every mining run through the single holdout guard.

        Previously this silently returned unless ``strict_holdout`` was
        configured, so re-mining on an already-burned ``test_period`` was
        completely unchecked. Now:

        * explicit ``strict_holdout`` -> one-time ``final`` reveal;
        * otherwise -> every ``data_feeds.test_period`` window is checked as
          ``explore`` (no reveal registered, but a burned window still raises).

        ``ledger_path`` honours an explicitly configured ledger so isolated
        self-check tools keep their own sandbox; the default is the one
        canonical project ledger.
        """
        from core.evaluation import holdout_guard

        common = dict(
            config=self.config,
            ledger_path=self.holdout.path,
            source="director:mine",
        )
        gates = self.config.get("evaluation", {}).get("gates", {}) or {}
        strict = gates.get("strict_holdout")
        if isinstance(strict, dict):
            holdout_guard.guard(
                strict.get("start"), strict.get("end"),
                purpose="final", **common
            )
            return
        periods = self.config.get("data_feeds", {}).get("test_period") or []
        holdout_guard.guard_periods(periods, purpose="explore", **common)

    def _snapshot_lineage(
        self,
        evaluation_split: str = "mine",
        candidate: Any = None,
    ) -> Dict[str, Any]:
        feeds = self.config.get("data_feeds", {})
        periods_key = "test_period" if evaluation_split == "test" else "mine_period"
        target_spec = getattr(candidate, "evaluation_target", None)
        if target_spec is None:
            getter = getattr(self.data_client, "get_target_spec", None)
            target_spec = getter() if callable(getter) else None
        if hasattr(target_spec, "as_dict"):
            target_payload = target_spec.as_dict()
        elif isinstance(target_spec, dict):
            target_payload = target_spec
        else:
            target_payload = None
        definition = getattr(candidate, "forward_return_definition", None)
        if not definition:
            definition_getter = getattr(
                self.data_client,
                "get_forward_return_definition",
                None,
            )
            definition = (
                definition_getter()
                if callable(definition_getter)
                else "close[t+1] / close[t] - 1"
            )
        lineage = {
            "source": self.data_client.__class__.__name__,
            "exchange": feeds.get("exchange"),
            "instrument_type": feeds.get("instrument_type"),
            "timeframe": feeds.get("timeframe"),
            "pairs": feeds.get("pairs", []),
            "mining_mode": feeds.get("mining_mode", "sequential_single"),
            "mine_period": feeds.get("mine_period", []),
            "test_period": feeds.get("test_period", []),
            "evaluation_split": evaluation_split,
            "evaluation_period": feeds.get(periods_key, []),
            "required_streams": feeds.get("required_streams", []),
            "target": target_payload,
            "forward_return_definition": definition,
        }
        # P1: stamp the reproducible identity into every persisted factor so a
        # reaudit can tell whether cached values were produced by this code/data.
        if self.manifest:
            from core.experiment.manifest import manifest_summary
            lineage["run_id"] = self.manifest.get("run_id")
            lineage["run_manifest"] = manifest_summary(self.manifest)
        return lineage

    def _save_factor_snapshot(self, factor_id: str, candidate: Any) -> None:
        """Persist the exact values and labels used for Phase II analysis."""
        try:
            evaluation_split = getattr(candidate, "evaluation_split", "mine")
            if evaluation_split == "test":
                evaluation_data = self.data_client.get_test_data()
                forward_returns = self.data_client.get_test_returns()
            else:
                evaluation_data = self.data_client.get_data()
                forward_returns = self.data_client.get_returns()
            forward_returns = getattr(
                candidate, "evaluation_returns", forward_returns
            )
            factor_values = candidate.compute(evaluation_data)
            lineage = self._snapshot_lineage(evaluation_split, candidate)
            saved = self.storage_client.save_factor_snapshot(
                factor_id,
                factor_values,
                forward_returns,
                lineage,
            )
            if not saved:
                logger.warning("No Phase II snapshot persisted for factor %s", factor_id)
        except Exception as exc:
            logger.warning("Failed to materialize Phase II snapshot for factor %s: %s", factor_id, exc)

    def _print_results_table(self, candidates):
        try:
            from rich.console import Console
            from rich.table import Table
            
            console = Console()
            mining_mode = self.config.get("data_feeds", {}).get("mining_mode", "sequential_single")
            universe = self.config.get("universe", "DEFAULT_UNIVERSE")
            
            table = Table(
                title=f"🎯 Mining Completed | Mode: {mining_mode} | Universe: {universe}",
                show_header=True, 
                header_style="bold magenta"
            )
            table.add_column("Rank", style="dim", width=6)
            table.add_column("Factor ID", width=12)
            
            if mining_mode == "sequential_single":
                table.add_column("Target Pair")
                
            table.add_column("Paradigm")
            table.add_column("Logic / Expression", overflow="fold")
            table.add_column("Fitness Score", justify="right")
            table.add_column("IC", justify="right")
            table.add_column("RankIC", justify="right")
            table.add_column("Complexity")
            
            for idx, (factor_id, cand) in enumerate(zip(self.latest_factor_ids, candidates)):
                metrics = cand.metrics
                fitness = f"{metrics.get('fitness_score', 0):.4f}"
                ic = f"{metrics.get('IC', 0):.4f}"
                rank_ic = f"{metrics.get('RankIC', 0):.4f}"
                paradigm = self.config.get("paradigm")
                logic = cand.to_display_string(max_length=None)
                complexity = cand.get_complexity()
                
                if mining_mode == "sequential_single":
                    target_pair = self.config.get("data_feeds", {}).get("pairs", [universe])[0]
                    table.add_row(str(idx + 1), factor_id, target_pair, paradigm, logic, fitness, ic, rank_ic, complexity)
                else:
                    table.add_row(str(idx + 1), factor_id, paradigm, logic, fitness, ic, rank_ic, complexity)
                    
            console.print(table)
        except ImportError:
            logger.info("Rich library not installed. Skipping fancy table.")

    def get_latest_factor_ids(self) -> List[str]:
        return self.latest_factor_ids
