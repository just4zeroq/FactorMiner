import concurrent.futures
import math
import traceback
import logging
from typing import List, Dict, Any, Optional

import numpy as np
import pandas as pd

from core.miner.expressions import FactorExpression
from core.miner.entities import CandidateEvaluation, EvaluationFeedback
from core.miner.registry import EvaluatorRegistry
from core.evaluation.code_sandbox import (
    FactorOutputError,
    RestrictedSandbox,
    SandboxExecutionError,
    SandboxTimeoutError,
    SecurityError,
)
from core.evaluation.significance import (
    ic_series,
    tstat_ic,
    deflated_sharpe_ratio,
    pvalue_from_t,
)
from core.evaluation.annualisation import resolve_annualisation
from core.config.defaults import DEFAULT_COST_RATE
from core.evaluation.portfolio_eval import (
    DEFAULT_LS_CFG,
    long_short_returns,
    timing_returns,
)

logger = logging.getLogger(__name__)

# Default fitness hyper-parameters. Every knob is overridable under
# config["fitness"] so behavior stays backward compatible when test_period is
# absent (out_of_sample auto-disables, shrinkage ~ 1 for large samples, etc.).
DEFAULT_FITNESS_CFG = {
    "out_of_sample": None,        # None => auto-enable only if a test split exists
    # A1: the mining loop must NEVER see the held-out split. OOS evaluation is
    # only legitimate on a frozen shortlist at the end of a run (evaluate_on
    # with explicit test_data). Set True only for that final, one-shot call.
    "allow_oos_in_search": False,
    "oos_exponent": 0.5,          # weight on OOS IC vs in-sample IC (geometric blend)
    "shrinkage_k": 30.0,          # observation-count shrinkage prior
    "consistency_penalty": 0.3,   # multiplier when OOS sign disagrees with in-sample
    "turnover_penalty": 0.0,      # normalized-turnover penalty weight (0 = off)
    "turnover_cap": 0.5,          # turnover ratio above this starts to be penalized
    "coverage_floor": 0.20,       # coverage below this is squared-penalized
    # Round-trip cost charged per unit of portfolio turnover by the
    # return-denominated gate (Phase P0.5). 18 bp ≈ 4 bp commission +
    # 5 bp slippage per side. Sourced from core.config.defaults so it can never
    # drift from the backtest / walk-forward cost (review A2 / F2).
    "gate_cost_rate": DEFAULT_COST_RATE,
    "gate_ls_quantile": 0.2,      # top/bottom fraction of the long-short book
    "gate_rebalance_bars": 1,     # book is re-formed every N bars (trading horizon)
    # P0-H1: when a factor has positive RankIC but its quantile long/short book
    # loses money (net of cost), the search target is misaligned with the
    # promotion gate. Multiply fitness by this floor so the director stops
    # favouring un-tradeable factors.
    "tradeable_floor": 0.1,
}


class ParallelEvaluator:
    """
    V4 多范式并行执行网关
    负责安全地执行 FactorExpression，并计算 Fitness，返回统一的 EvaluationFeedback

    P0 upgrade: fitness now blends in-sample (mine_period) and out-of-sample
    (test_period) RankIC with sample-size shrinkage and a directional-consistency
    penalty. This directly attacks the "IC looks great but the factor loses money"
    over-fitting trap that the previous in-sample-only fitness hid.
    """

    def __init__(self, data_client: Any, config: Dict):
        self.data_client = data_client
        self.config = config
        # Cumulative number of candidates handed to this evaluator. Used as the
        # multiple-testing denominator for DSR (deflated Sharpe) so the bar rises
        # with every idea actually tried, not just with the ones finally saved.
        self.total_evaluated = 0
        # Raw p-values of every successful candidate handed to this evaluator,
        # used for cumulative BH-FDR. Cleared by the director once recorded.
        self.run_p_values: List[float] = []
        configured_workers = self.config.get("evaluation", {}).get("max_workers", 8)
        if isinstance(configured_workers, bool):
            raise ValueError("evaluation.max_workers must be a positive integer")
        try:
            self.max_workers = int(configured_workers)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "evaluation.max_workers must be a positive integer"
            ) from exc
        if not 1 <= self.max_workers <= 64:
            raise ValueError(
                "evaluation.max_workers must be an integer between 1 and 64"
            )
        # Per-candidate wall-clock budget. A pathological factor (infinite loop in
        # the sandbox, a pandas op that explodes on a sparse panel) used to hang
        # the whole round. Set to 0 to wait forever (legacy behaviour).
        configured_timeout = self.config.get("evaluation", {}).get("timeout_seconds", 120)
        try:
            self.timeout_per_candidate = float(configured_timeout or 0)
        except (TypeError, ValueError):
            self.timeout_per_candidate = 120.0
        if self.timeout_per_candidate < 0:
            self.timeout_per_candidate = 0.0
        # Number of candidates that blew the budget this run (surfaced in reports).
        self.timed_out = 0
        self.gate_rebalance_bars = self._resolve_gate_rebalance_bars()
        fitness_cfg = self.config.get("fitness", {}) or {}
        self.allow_oos_in_search = bool(fitness_cfg.get("allow_oos_in_search", False))

    def _resolve_gate_rebalance_bars(self) -> int:
        """Trading frequency used by the return-denominated gate.

        The gate must judge the factor at the horizon it was mined for: a
        1-day target means the book is re-formed every day and pays daily
        turnover cost. An explicit ``fitness.gate_rebalance_bars`` always wins;
        otherwise we mirror the configured prediction horizon.
        """
        explicit = self.config.get("fitness", {}).get("gate_rebalance_bars")
        if explicit is not None:
            try:
                return max(1, int(explicit))
            except (TypeError, ValueError):
                pass
        spec_getter = getattr(self.data_client, "get_target_spec", None)
        if callable(spec_getter):
            try:
                horizon = int(getattr(spec_getter(), "horizon_bars", 1) or 1)
                return max(1, horizon)
            except Exception:  # pragma: no cover - defensive
                pass
        return 1

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #
    def evaluate(self, candidates: List[FactorExpression]) -> EvaluationFeedback:
        """Evaluate candidates on the mining (in-sample) split ONLY.

        A1: the previous version called ``get_test_data()`` / ``get_test_returns()``
        on EVERY generation and folded ``abs(RankIC_oos)`` into fitness, so the
        held-out split was repeatedly used for search. The strict-holdout ledger
        guards a different window and cannot stop this. OOS is now evaluated
        exactly once, on a frozen shortlist, via :meth:`evaluate_final` (or
        :meth:`evaluate_on` with explicit test data).
        """
        if not self.allow_oos_in_search:
            return self.evaluate_on(
                candidates,
                self.data_client.get_data(),
                self.data_client.get_returns(),
            )

        test_data = None
        test_returns = None
        tgetter = getattr(self.data_client, "get_test_data", None)
        rgetter = getattr(self.data_client, "get_test_returns", None)
        if callable(tgetter):
            try:
                test_data = tgetter()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("get_test_data() failed: %s", exc)
                test_data = None
        if callable(rgetter):
            try:
                test_returns = rgetter()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("get_test_returns() failed: %s", exc)
                test_returns = None
        return self.evaluate_on(
            candidates,
            self.data_client.get_data(),
            self.data_client.get_returns(),
            test_data=test_data,
            test_returns=test_returns,
        )

    def evaluate_final(self, candidates: List[FactorExpression]) -> EvaluationFeedback:
        """One-shot OOS evaluation of a frozen shortlist (never used for search).

        This is the **public** one-shot OOS entry point (library / CLI callers).
        The director's in-pipeline equivalent, ``_attach_final_oos_metrics``,
        calls the very same :meth:`_compute_oos_metrics` primitive on the same
        split, so the two produce identical, *signed* ``RankIC_oos`` values.
        They are intentionally two surfaces over one implementation rather than
        two independent OOS code paths (see ``test_layer2_leaks``
        ``test_two_oneshot_oos_paths_agree_on_signed_rankic``).

        Reads ``get_test_data()`` / ``get_test_returns()`` exactly once per call.
        """
        test_data = self.data_client.get_test_data()
        test_returns = self.data_client.get_test_returns()
        return self.evaluate_on(
            candidates,
            self.data_client.get_data(),
            self.data_client.get_returns(),
            test_data=test_data,
            test_returns=test_returns,
        )

    def evaluate_on(
        self,
        candidates: List[FactorExpression],
        data: Any,
        returns: Any,
        test_data: Any = None,
        test_returns: Any = None,
    ) -> EvaluationFeedback:
        """Evaluate candidates on an explicit split without mutating the data client.

        `test_data` / `test_returns` (optional) enable out-of-sample fitness.
        """
        feedback = EvaluationFeedback()
        if not candidates:
            return feedback
        self.total_evaluated += len(candidates)

        # 提取用户配置的自定义打分钩子
        fitness_hook_name = self.config.get("fitness", {}).get("hook")
        fitness_func = None
        if fitness_hook_name:
            fitness_func = EvaluatorRegistry._registry.get(fitness_hook_name)

        def safe_compute(expr: FactorExpression) -> CandidateEvaluation:
            try:
                factor_values = expr.compute(data)

                # 如果是深度学习网络直接输出的 tensor (带有梯度图)
                if hasattr(factor_values, 'requires_grad') and factor_values.requires_grad:
                    return CandidateEvaluation(
                        candidate=expr,
                        status="success",
                        raw_outputs=factor_values,
                    )

                # 标量评价
                self._validate_metric_inputs(factor_values, returns)
                # 强制计算基础指标 (in-sample)
                in_metrics = self._calculate_built_in_metrics(
                    factor_values, returns, self.gate_rebalance_bars
                )

                # 尝试计算样本外指标
                oos_metrics = self._compute_oos_metrics(expr, test_data, test_returns)

                # 用户自定义打分或降级默认打分
                if fitness_func:
                    custom_score = fitness_func(factor_values, returns, in_metrics)
                    if isinstance(custom_score, dict):
                        metrics = {**in_metrics, **custom_score}
                    else:
                        metrics = {**in_metrics, "fitness_score": float(custom_score)}
                    # 仍附加 OOS 诊断字段（不覆盖用户的 fitness_score）
                    if oos_metrics is not None:
                        metrics.setdefault("RankIC_oos", float(oos_metrics.get("RankIC", 0.0)))
                        metrics.setdefault("IC_oos", float(oos_metrics.get("IC", 0.0)))
                        metrics.setdefault("oos_consistent", True)
                else:
                    metrics = self._build_fitness(
                        in_metrics, oos_metrics, factor_values, returns
                    )

                # Phase 1: statistical verdict (HAC t / p / DSR / cost-adj) so the
                # director can gate promotion on real evidence, not just fitness.
                if factor_values is not None and returns is not None:
                    verdict = self._build_verdict(factor_values, returns, metrics)
                    metrics.update(verdict)

                expr.metrics = metrics
                return CandidateEvaluation(
                    candidate=expr,
                    status="success",
                    metrics=metrics,
                )
            except Exception as e:
                # 记录详细报错供 LLM 反思或排查
                return CandidateEvaluation(
                    candidate=expr,
                    status="error",
                    error_type=type(e).__name__,
                    error_message=str(e),
                    traceback=traceback.format_exc(),
                )

        worker_count = min(self.max_workers, len(candidates))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="factor-evaluator",
        )
        futures = {executor.submit(safe_compute, cand): cand for cand in candidates}
        results_by_id: Dict[int, CandidateEvaluation] = {}
        budget = self.timeout_per_candidate * len(candidates) if self.timeout_per_candidate else None
        try:
            for future in concurrent.futures.as_completed(futures, timeout=budget):
                candidate = futures[future]
                try:
                    results_by_id[id(candidate)] = future.result()
                except Exception as exc:  # cancelled or executor failure
                    results_by_id[id(candidate)] = CandidateEvaluation(
                        candidate=candidate,
                        status="error",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
        except (TimeoutError, concurrent.futures.TimeoutError):
            logger.warning(
                "Evaluation budget of %.1fs exhausted after %d/%d candidates; "
                "the rest are recorded as timeouts and the round continues.",
                budget, len(results_by_id), len(candidates),
            )
        finally:
            # Never block the miner on a stuck worker. Running threads keep
            # burning until they finish, but the round moves on.
            executor.shutdown(wait=False, cancel_futures=True)

        results: List[CandidateEvaluation] = []
        for candidate in candidates:
            result = results_by_id.get(id(candidate))
            if result is None:
                self.timed_out += 1
                result = CandidateEvaluation(
                    candidate=candidate,
                    status="timeout",
                    error_type="EvaluationTimeout",
                    error_message=(
                        f"No result within the {self.timeout_per_candidate:.0f}s "
                        f"per-candidate budget"
                    ),
                )
            results.append(result)

        feedback.results.extend(results)
        # Phase P1 (cumulative FDR): keep the raw p-value of every candidate we
        # actually looked at. Correcting only the handful that reach the short
        # list is close to no correction at all.
        for res in results:
            p = (res.metrics or {}).get("p_value")
            if res.status == "success" and p is not None:
                try:
                    pv = float(p)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(pv):
                    self.run_p_values.append(pv)
        return feedback

    # ------------------------------------------------------------------ #
    # Metric helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _calculate_built_in_metrics(
        factor_values: Any, returns: Any, rebalance_bars: int = 1
    ) -> Dict:
        """计算基础评估指标: IC, RankIC, Turnover

        E-08: the tradeable book must use the SAME rebalance cadence and the
        SAME orientation statistic as the promotion gate / verdict. Previously
        fitness used a 1-bar book and the sign of pooled Spearman RankIC,
        while the gate used the configured horizon and the mean of the IC time
        series (two different statistics that can disagree in sign).
        """
        metrics = {"IC": 0.0, "RankIC": 0.0, "Turnover": 0.0}

        if factor_values is None or returns is None:
            return metrics

        if isinstance(factor_values, pd.DataFrame) and isinstance(returns, pd.DataFrame):
            # Cross-Asset Mode
            if not factor_values.isna().all().all() and not returns.isna().all().all():
                ic = factor_values.corrwith(returns, axis=1, method='pearson').mean()
                rank_ic = factor_values.corrwith(returns, axis=1, method='spearman').mean()
                if pd.notna(ic):
                    metrics["IC"] = ic
                if pd.notna(rank_ic):
                    metrics["RankIC"] = rank_ic
                # P0-H1: score the quantile long/short net return with the same
                # book construction the promotion gate uses, so the search target
                # (fitness) is aligned with the gate target (tradeable return),
                # not just raw |RankIC|. Orientation follows the sign of RankIC.
                try:
                    from core.evaluation.portfolio_eval import long_short_returns
                    from core.config.defaults import DEFAULT_COST_RATE
                    ic_ts = ic_series(factor_values, returns)
                    ic_mean = float(np.nanmean(ic_ts)) if ic_ts.size else 0.0
                    ls_orient = -1.0 if ic_mean < 0 else 1.0
                    ls = long_short_returns(
                        factor_values, returns,
                        orientation=ls_orient,
                        cost_rate=DEFAULT_COST_RATE,
                        rebalance_bars=max(1, int(rebalance_bars)),
                    )
                    metrics["ls_cost_adj_return"] = float(ls.get("cost_adj_return", 0.0))
                    metrics["ls_t"] = float(ls.get("ls_t", 0.0))
                    metrics["ls_gross"] = float(ls.get("mean_return", 0.0))
                except Exception:
                    metrics["ls_cost_adj_return"] = 0.0
                    metrics["ls_t"] = 0.0
                    metrics["ls_gross"] = 0.0
            turnover = factor_values.diff().abs().mean().mean()
            if pd.notna(turnover):
                metrics["Turnover"] = turnover
        elif isinstance(factor_values, pd.Series) and isinstance(returns, pd.Series):
            # Sequential Single Mode
            if not factor_values.isna().all() and not returns.isna().all():
                ic = factor_values.corr(returns, method='pearson', min_periods=30)
                rank_ic = factor_values.corr(returns, method='spearman', min_periods=30)
                if pd.notna(ic):
                    metrics["IC"] = ic
                if pd.notna(rank_ic):
                    metrics["RankIC"] = rank_ic
            turnover = factor_values.diff().abs().mean()
            if pd.notna(turnover):
                metrics["Turnover"] = turnover

        return metrics

    def _compute_oos_metrics(self, expr, test_data, test_returns) -> Optional[Dict]:
        """Compute out-of-sample metrics if a usable test split is available."""
        if test_data is None or test_returns is None:
            return None
        try:
            if isinstance(test_data, pd.DataFrame) and test_data.empty:
                return None
            if isinstance(test_data, dict) and (not test_data or all(
                getattr(v, "empty", False) for v in test_data.values()
            )):
                return None
            oos_values = expr.compute(test_data)
            self._validate_metric_inputs(oos_values, test_returns)
            return self._calculate_built_in_metrics(
                oos_values, test_returns, self.gate_rebalance_bars
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("OOS metric computation skipped: %s", exc)
            return None

    @staticmethod
    def _validate_metric_inputs(factor_values: Any, returns: Any) -> None:
        """Reject shape/index coercion before metrics can silently realign data."""
        if isinstance(returns, pd.Series):
            if not isinstance(factor_values, pd.Series):
                raise FactorOutputError(
                    "Factor output must be pandas.Series when returns is pandas.Series"
                )
            if not factor_values.index.equals(returns.index):
                raise FactorOutputError("Factor and returns indexes must match exactly")
            objects = [factor_values, returns]
        elif isinstance(returns, pd.DataFrame):
            if not isinstance(factor_values, pd.DataFrame):
                raise FactorOutputError(
                    "Factor output must be pandas.DataFrame when returns is pandas.DataFrame"
                )
            if (
                not factor_values.index.equals(returns.index)
                or not factor_values.columns.equals(returns.columns)
            ):
                raise FactorOutputError(
                    "Factor and returns indexes and columns must match exactly"
                )
            objects = [factor_values, returns]
        else:
            raise FactorOutputError(
                f"Returns must be pandas.Series or pandas.DataFrame, received "
                f"{type(returns).__name__}"
            )

        for value in objects:
            if value.index.has_duplicates:
                raise FactorOutputError("Metric inputs must not contain duplicate indexes")
            dtypes = [value.dtype] if isinstance(value, pd.Series) else list(value.dtypes)
            if any(not pd.api.types.is_numeric_dtype(dtype) for dtype in dtypes):
                raise FactorOutputError("Metric inputs must contain numeric values only")
            numeric = value.to_numpy(dtype=float, na_value=float("nan"))
            if bool((~np.isnan(numeric) & ~np.isfinite(numeric)).any()):
                raise FactorOutputError("Metric inputs must not contain infinite values")
        if factor_values.notna().to_numpy().sum() == 0:
            raise FactorOutputError("Factor output must contain at least one finite value")

        # A5: constant / exploding factors must never reach the library. Three
        # all-zero factors were stored and rank(method="first") turned them
        # into alphabetical noise; 31/84 snapshots had std > 1e6 because
        # _safe_divide replaced a zero denominator with 1e-9.
        finite = factor_values.to_numpy(dtype=float, na_value=float("nan"))
        finite = finite[np.isfinite(finite)]
        if finite.size < 2:
            raise FactorOutputError(
                "Factor output needs at least two finite values to be evaluated"
            )
        if np.unique(finite).size < 2:
            raise FactorOutputError(
                "Factor output is constant: no cross-sectional dispersion"
            )
        if float(np.std(finite)) > 1e9 or float(np.max(np.abs(finite))) > 1e12:
            raise FactorOutputError(
                "Factor output magnitude is exploding (zero-denominator artefact?)"
            )

    # ------------------------------------------------------------------ #
    # Fitness blend (the P0 upgrade)
    # ------------------------------------------------------------------ #
    def _build_fitness(
        self,
        in_metrics: Dict,
        oos_metrics: Optional[Dict],
        factor_values: Any,
        returns: Any,
    ) -> Dict:
        cfg = {**DEFAULT_FITNESS_CFG, **self.config.get("fitness", {})}

        # Coverage of the in-sample factor (guards against all-NaN tricks).
        coverage = self._coverage(factor_values)
        cov_floor = cfg["coverage_floor"]
        penalty = (coverage / cov_floor) ** 2 if coverage < cov_floor else 1.0

        # Effective sample size -> shrinkage (James-Stein style). For real-sized
        # datasets (thousands of bars) this is ~1.0 and only bites tiny samples.
        n_obs = self._effective_obs(factor_values, returns)
        k = cfg["shrinkage_k"]
        shrink = float(np.sqrt(max(n_obs - k, 0) / max(n_obs, 1))) if n_obs > 0 else 0.0

        rankic_in = abs(float(in_metrics.get("RankIC", 0.0)))
        ic_in = float(in_metrics.get("IC", 0.0))

        oos_present = oos_metrics is not None
        oos_enabled = cfg["out_of_sample"]
        if oos_enabled is None:
            oos_enabled = oos_present  # auto-enable only when a test split exists
        use_oos = bool(oos_enabled and oos_present)

        if use_oos:
            rankic_oos = abs(float(oos_metrics.get("RankIC", 0.0)))
            rankic_oos_signed = float(oos_metrics.get("RankIC", 0.0))
            ic_oos = float(oos_metrics.get("IC", 0.0))
            in_sign = 1 if in_metrics.get("RankIC", 0) >= 0 else -1
            oos_sign = 1 if oos_metrics.get("RankIC", 0) >= 0 else -1
            consistent = (in_sign == oos_sign) and (abs(in_metrics.get("RankIC", 0)) > 1e-9)
            cons_mult = 1.0 if consistent else float(cfg["consistency_penalty"])
        else:
            rankic_oos = rankic_in
            rankic_oos_signed = float(in_metrics.get("RankIC", 0.0))
            ic_oos = ic_in
            consistent = True
            cons_mult = 1.0

        lam = float(cfg["oos_exponent"])
        lam = min(max(lam, 0.0), 1.0)
        # Geometric blend of in-sample and OOS RankIC. If OOS RankIC collapses to
        # ~0, the whole fitness collapses -> over-fit factors are no longer rewarded.
        if rankic_in <= 0:
            blended = 0.0
        else:
            blended = (rankic_in ** (1.0 - lam)) * (rankic_oos ** lam)

        # Shrinkage only kicks in when we are actually doing out-of-sample
        # evaluation, so the pure in-sample path is numerically unchanged.
        if not use_oos:
            shrink = 1.0

        # Normalized-turnover penalty (off by default; scale-free).
        tp = 1.0
        gamma = float(cfg["turnover_penalty"])
        if gamma > 0 and rankic_in > 0:
            try:
                level = float(abs(factor_values).mean().mean()) if isinstance(
                    factor_values, pd.DataFrame
                ) else float(abs(factor_values).mean())
                tn_norm = float(in_metrics.get("Turnover", 0.0)) / (level + 1e-9)
                cap = float(cfg["turnover_cap"])
                tp = 1.0 / (1.0 + gamma * max(0.0, tn_norm - cap))
            except Exception:
                tp = 1.0

        # P0-H1: penalise factors whose IC predicts the right direction but
        # whose quantile long/short book loses money. Such a factor is not
        # tradeable, so the search target must not reward it — otherwise the
        # gate (which judges the tradeable return) keeps rejecting winners that
        # fitness kept promoting.
        ls_corr = float(in_metrics.get("ls_cost_adj_return", 0.0))
        tradeable_mult = 1.0
        if rankic_in > 0 and ls_corr <= 0:
            tradeable_mult = float(cfg.get("tradeable_floor", 0.1))

        fitness_score = 100.0 * blended * tradeable_mult * cons_mult * shrink * tp * penalty

        metrics = {
            **in_metrics,
            # OOS-04: report the *signed* OOS RankIC (matching the director's
            # final-OOS path). The promotion gate already takes abs() where it
            # needs magnitude, so this keeps the sign available for diagnosis
            # and makes the two one-shot OOS surfaces agree numerically.
            "RankIC_oos": rankic_oos_signed,
            "IC_oos": ic_oos,
            "oos_consistent": bool(consistent),
            "oos_enabled": bool(oos_enabled and oos_present),
            "shrinkage": shrink,
            "coverage": coverage,
            "turnover_penalty": tp,
            "fitness_score": float(fitness_score),
        }
        return metrics

    # ------------------------------------------------------------------ #
    # Phase 1 statistical verdict (per-factor, cross-sectional panels)
    # ------------------------------------------------------------------ #
    def _build_verdict(
        self, factor_values: Any, returns: Any, metrics: Dict
    ) -> Dict:
        """Compute the per-factor statistical verdict used by the promotion gate.

        Uses the *in-sample* cross-sectional IC time series (needs a (T, N)
        panel, i.e. cross-asset mode). For sequential single-asset mode the
        cross-section does not exist, so the verdict is left neutral (the
        factor simply cannot be statistically validated by this gate yet —
        it must go through temporal assessment instead, see the survey's
        trade-learn note on single-asset evaluation).

        Returns: hac_t, p_value, dsr, ir, n_obs, cost_adj_return, orientation.

        All inference is done on the *oriented* factor (direction learned from
        the in-sample IC sign), so a consistently negative-IC factor is judged
        as the long-short strategy it actually is.
        """
        cfg = {**DEFAULT_FITNESS_CFG, **self.config.get("fitness", {})}
        verdict = {
            "hac_t": 0.0,
            "p_value": 1.0,
            "dsr": 0.0,
            "ir": 0.0,
            "n_obs": 0,
            "cost_adj_return": 0.0,
        }
        try:
            if isinstance(factor_values, pd.Series) and isinstance(
                returns, pd.Series
            ):
                return self._build_temporal_verdict(factor_values, returns, cfg)
            if not (
                isinstance(factor_values, pd.DataFrame)
                and isinstance(returns, pd.DataFrame)
            ):
                logger.debug("verdict neutral: not a cross-asset panel")
                return verdict
            f = factor_values.to_numpy(dtype=float)
            r = returns.to_numpy(dtype=float)
            ic_ts = ic_series(f, r)
            ic_ts = ic_ts[np.isfinite(ic_ts)]
            if ic_ts.size < 4:
                return verdict
            hac_t = tstat_ic(f, r)
            # E-03: finite-sample HAC t has fat tails; use the Student-t
            # reference law with df = (IC observations - 1), not N(0,1).
            p_value = pvalue_from_t(hac_t, df=int(ic_ts.size) - 1)
            mean_ic = float(ic_ts.mean())
            # Orientation is part of the model and is learned in-sample (the
            # backtest does the same via infer_orientation). A factor with a
            # consistently negative IC is a perfectly good *long-short* signal;
            # judging DSR / cost-adjusted return on the raw sign would reject
            # every such factor for a purely cosmetic reason.
            orientation = 1.0 if mean_ic >= 0 else -1.0
            mean_ic = abs(mean_ic)
            hac_t = abs(float(hac_t))
            std_ic = float(ic_ts.std())
            ir = mean_ic / std_ic if std_ic > 0 else 0.0
            n_obs = int(ic_ts.size)
            # Phase P1 (magnitude-aware): DSR must deflate the Sharpe ratio of
            # the *strategy*, not the information ratio of the IC series.
            # IC-IR is scale-free, so it rewards "stable but tiny" factors and
            # is blind to how much money the book actually makes. The strategy
            # Sharpe is the quantity Bailey & Lopez de Prado actually deflate,
            # and it is increasing in the size of the edge.

            # Phase P0.5 — return-denominated tradability.
            # `mean IC - cost` mixed an information coefficient with a trading
            # cost, so it could never answer "does this book make money?".
            # We now build the actual quantile long-short book (gross 1.0,
            # 50/50 dollar-neutral) on the oriented factor and charge the cost
            # of its turnover, so `cost_adj_return` is a per-period net return
            # that is directly comparable to a backtest.
            # P1-6: ``cfg`` is the DEFAULT_FITNESS_CFG-merged dict and that
            # default declares ``gate_rebalance_bars: 1``, so the old
            # ``cfg.get(...) or self.gate_rebalance_bars`` short-circuited to 1
            # for every config that sets ``target.horizon_bars`` instead of an
            # explicit fitness key — silently disabling the h>1 overlap
            # discount. ``self.gate_rebalance_bars`` already resolves
            # (explicit fitness key > target horizon) at construction time.
            horizon = max(1, int(self.gate_rebalance_bars))
            ls = long_short_returns(
                factor_values,
                returns,
                quantile=float(
                    cfg.get("gate_ls_quantile", DEFAULT_LS_CFG["quantile"])
                ),
                orientation=float(orientation),
                cost_rate=float(cfg.get("gate_cost_rate", 0.0)),
                annualisation=resolve_annualisation(
                    self.config, index=factor_values.index
                ),
                rebalance_bars=horizon,
            )
            str_sharpe = (
                float(ls["cost_adj_return"]) / float(ls["std_return"])
                if float(ls["std_return"]) > 0
                else 0.0
            )
            # M-08: with a multi-bar prediction horizon (h>1) the forward
            # returns overlap by (h-1) bars, so consecutive observations are NOT
            # independent: the true independent sample is ~T/h, not T. Feeding
            # T into the DSR deflation overstates evidence by ~sqrt(h) and lets
            # an h=5 factor clear a bar it should not. For h=1 this is a no-op.
            dsr_obs = max(2, int(math.ceil(float(ls["n_obs"]) / float(horizon))))
            dsr = deflated_sharpe_ratio(
                sharpe=str_sharpe, n_obs=dsr_obs, n_trials=1,
                # P1-M9: pass the empirical net-return moments so DSR is not
                # optimistic on fat-tailed / negatively-skewed books.
                skewness=float(ls.get("net_skew", 0.0)),
                kurtosis=float(ls.get("net_kurtosis", 3.0)),
            )
            verdict.update({
                "hac_t": float(hac_t),
                "p_value": float(p_value),
                "dsr": float(dsr),
                # P1-b: provenance. This DSR is deflated for exactly one trial;
                # the promotion gate re-deflates it with the cumulative trial
                # count (see apply_promotion_gates). Keeping the denominator
                # explicit stops an audit from reading 1.0 here as evidence.
                "dsr_n_trials": 1,
                "ir": float(ir),
                "n_obs": n_obs,
                # Strategy Sharpe used by DSR (per period, net of cost) and the
                # observation count it was estimated on.
                "sr": float(str_sharpe),
                "sr_n_obs": int(dsr_obs),
                "cost_adj_return": float(ls["cost_adj_return"]),
                "orientation": float(orientation),
                # Return-space diagnostics (net of turnover cost).
                "ls_return": float(ls["mean_return"]),
                "ls_vol": float(ls["std_return"]),
                "ls_sharpe": float(ls["sharpe"]),
                "ls_hit": float(ls["hit_rate"]),
                "ls_turnover": float(ls["turnover"]),
                "ls_cost": float(ls["cost"]),
                "ls_t": float(ls["ls_t"]),
                # P1-M9: carry the net-return moments so the gate's DSR deflation
                # (apply_promotion_gates) uses the same correction as this verdict.
                "net_skew": float(ls.get("net_skew", 0.0)),
                "net_kurtosis": float(ls.get("net_kurtosis", 3.0)),
            })
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("verdict skipped: %s", exc)
        return verdict

    def _build_temporal_verdict(
        self, factor_values: pd.Series, returns: pd.Series, cfg: Dict
    ) -> Dict:
        """Verdict for single-asset (sequential) factors.

        There is no cross-section to rank, so a rank IC is not directly
        tradable. What a single-asset factor actually trades is a *timing*
        rule, so the verdict is built from that rule's return stream:
        long when the factor is in its own trailing top quantile, short in the
        bottom one, with turnover charged the same way as the panel book.

        Until this existed, every sequential factor (RL / NN / single-coin GP)
        received a neutral verdict (t=0, p=1) and therefore could never be
        promoted, no matter how good it was.
        """
        verdict = {
            "hac_t": 0.0,
            "p_value": 1.0,
            "dsr": 0.0,
            "ir": 0.0,
            "n_obs": 0,
            "cost_adj_return": 0.0,
            "mode": "temporal",
        }
        aligned = pd.DataFrame(
            {
                "f": factor_values.astype(float),
                "r": returns.reindex(factor_values.index).astype(float),
            }
        ).dropna()
        if aligned.shape[0] < 30:
            return verdict

        corr = aligned["f"].corr(aligned["r"], method="spearman")
        if pd.isna(corr):
            return verdict
        orientation = 1.0 if corr >= 0 else -1.0

        # P1-6: same short-circuit trap as the cross-sectional gate — read the
        # horizon resolved at construction, not the merged default of 1.
        horizon = max(1, int(self.gate_rebalance_bars))
        tm = timing_returns(
            factor_values,
            returns,
            lookback=int(
                cfg.get(
                    "gate_temporal_lookback", DEFAULT_LS_CFG["temporal_lookback"]
                )
            ),
            quantile=float(
                cfg.get("gate_ls_quantile", DEFAULT_LS_CFG["quantile"])
            ),
            orientation=orientation,
            cost_rate=float(cfg.get("gate_cost_rate", 0.0)),
            annualisation=resolve_annualisation(
                self.config, index=factor_values.index
            ),
            rebalance_bars=horizon,
        )
        if tm["n_obs"] < 10:
            return verdict

        hac_t = abs(float(tm["ls_t"]))
        sr = (
            float(tm["cost_adj_return"]) / float(tm["std_return"])
            if float(tm["std_return"]) > 0
            else 0.0
        )
        # M-08: honour the h>1 overlap discount for the temporal path too.
        dsr_obs_t = max(2, int(math.ceil(float(tm["n_obs"]) / float(horizon))))
        verdict.update({
            "hac_t": hac_t,
            # E-03: Student-t tail with the traded book's observation count.
            "p_value": pvalue_from_t(hac_t, df=int(tm["n_obs"]) - 1),
            "dsr_n_trials": 1,
            "dsr": deflated_sharpe_ratio(
                sharpe=sr, n_obs=dsr_obs_t, n_trials=1,
                # P1-M9: empirical net-return moments (see long_short_returns).
                skewness=float(tm.get("net_skew", 0.0)),
                kurtosis=float(tm.get("net_kurtosis", 3.0)),
            ),
            # No IC information ratio exists in the time series; the strategy
            # Sharpe is the risk-adjusted ratio that DSR deflates.
            "ir": float(sr),
            "n_obs": int(tm["n_obs"]),
            "sr": float(sr),
            "sr_n_obs": int(dsr_obs_t),
            "cost_adj_return": float(tm["cost_adj_return"]),
            "orientation": float(orientation),
            "ls_return": float(tm["mean_return"]),
            "ls_vol": float(tm["std_return"]),
            "ls_sharpe": float(tm["sharpe"]),
            "ls_hit": float(tm["hit_rate"]),
            "ls_turnover": float(tm["turnover"]),
            "ls_cost": float(tm["cost"]),
            "ls_t": float(tm["ls_t"]),
            "ls_exposure": float(tm["exposure"]),
            # P1-M9: net-return moments for the gate's DSR deflation.
            "net_skew": float(tm.get("net_skew", 0.0)),
            "net_kurtosis": float(tm.get("net_kurtosis", 3.0)),
        })
        return verdict

    @staticmethod
    def _coverage(factor_values: Any) -> float:
        """Fraction of finite cells in the factor panel.

        Counts valid cells individually (a missing return for one asset must
        not void the whole timestamp). The previous `dropna()`-based version
        dropped any row containing a single NaN, which on sparse crypto panels
        (scattered listing gaps) collapsed a 0.85-coverage panel to ~0.04 and
        silently zeroed the fitness of valid factors.
        """
        try:
            arr = None
            if hasattr(factor_values, "to_numpy"):
                arr = np.asarray(factor_values.to_numpy(), float)
            elif isinstance(factor_values, (np.ndarray, list)):
                arr = np.asarray(factor_values, float)
            if arr is not None and arr.size > 0:
                total = arr.size
                valid = int(np.isfinite(arr).sum())
                return valid / total if total > 0 else 1.0
        except Exception:
            pass
        return 1.0

    @staticmethod
    def _effective_obs(factor_values: Any, returns: Any = None) -> int:
        """Independent time observations (T), not T*N finite cells.

        HAC, DSR and ``ic_series`` all work on the T-length IC time series, so
        shrinkage must share that sample size. Using the cell count made
        ``shrink ~= 1`` forever in cross-asset mode and gave three statistics
        three different effective sample sizes.
        """
        obj = factor_values
        if isinstance(obj, dict):
            obj = next((v for v in obj.values() if hasattr(v, "index")), None)
        idx = getattr(obj, "index", None)
        if idx is not None:
            return int(len(idx))
        try:
            return int(np.asarray(obj).shape[0])
        except Exception:
            return 0

    @staticmethod
    def _valid_pair_count(factor_values: Any, returns: Any) -> int:
        try:
            if isinstance(factor_values, pd.DataFrame):
                return int((factor_values.notna() & returns.notna()).sum().sum())
            return int((factor_values.notna() & returns.notna()).sum())
        except Exception:
            return 0
