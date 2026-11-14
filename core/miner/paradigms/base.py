from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import logging
import hashlib
import numpy as np
import pandas as pd

from core.miner.expressions import FactorExpression
from core.miner.expressions import fit_linear_combination, _safe_corr, LinearCombinedFactor
from core.miner.pool import AlphaPool
from core.miner.entities import EvaluationFeedback, MinerState

logger = logging.getLogger(__name__)

def factor_signature(values: Any) -> Optional[np.ndarray]:
    """Flatten a factor panel into a comparable signature.

    Cross-sectional panels are ranked per timestamp first, so the signature
    measures *ordering* rather than scale — two factors that rank assets
    identically are redundant even if one is 100x the other. Returns ``None``
    when the factor has no usable values.
    """
    if values is None:
        return None
    try:
        if isinstance(values, pd.DataFrame):
            if values.empty:
                return None
            ranked = values.rank(axis=1)
            return ranked.to_numpy(dtype=float, na_value=np.nan).reshape(-1)
        if isinstance(values, pd.Series):
            if values.empty:
                return None
            return values.to_numpy(dtype=float).reshape(-1)
        array = np.asarray(values, dtype=float)
        return array.reshape(-1) if array.size else None
    except Exception:
        return None


def signature_correlation(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """Pearson correlation over the jointly-finite part of two signatures."""
    if a is None or b is None or a.shape != b.shape:
        return None
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 50:
        return None
    aa = a[mask]
    bb = b[mask]
    aa = aa - aa.mean()
    bb = bb - bb.mean()
    denom = float(np.sqrt((aa**2).sum() * (bb**2).sum()))
    if denom <= 0:
        return None
    return float((aa * bb).sum() / denom)


class DiversityFilter:
    """因子正交性与多样性过滤器

    两级去重：

    1. **硬去重**（始终开启）—— 按源码 MD5 拦截完全相同的表达式。
    2. **软去重**（``value_threshold > 0`` 时开启）—— 把候选的因子面值按截面
       rank 展平做相关，``|corr| > value_threshold`` 视为同一个信号（反向只是
       符号翻转，同样冗余）。这一层拦的是"同一信号的不同写法"：源码 MD5 对它们
       完全无效，而它们会白白消耗试验预算、抬高 DSR 的分母。

    软去重只对通过硬去重的候选计算面值，且 archive 有容量上限，避免 O(K²) 失控。
    """

    def __init__(
        self,
        correlation_threshold: float = 0.7,
        initial_hashes: set = None,
        value_threshold: float = 0.0,
        value_archive_limit: int = 60,
    ):
        self.threshold = correlation_threshold
        self.archive_hashes = initial_hashes or set()
        self.value_threshold = float(value_threshold or 0.0)
        self.value_archive_limit = max(1, int(value_archive_limit or 60))
        self._value_signatures: List[np.ndarray] = []
        self._signature_cache: Dict[int, Optional[np.ndarray]] = {}
        self.value_rejected = 0
        self.max_observed_corr = 0.0

    # ---- soft dedup ----------------------------------------------------- #
    def _signature(self, candidate: FactorExpression, data: Any) -> Optional[np.ndarray]:
        key = id(candidate)
        if key in self._signature_cache:
            return self._signature_cache[key]
        signature = None
        try:
            signature = factor_signature(candidate.compute(data))
        except Exception as exc:
            logger.debug("Value dedup skipped for a candidate: %s", exc)
        if len(self._signature_cache) > 4096:
            self._signature_cache.clear()
        self._signature_cache[key] = signature
        return signature

    def _max_abs_corr(self, signature: Optional[np.ndarray]) -> float:
        if signature is None or not self._value_signatures:
            return 0.0
        best = 0.0
        for other in self._value_signatures:
            corr = signature_correlation(signature, other)
            if corr is not None:
                best = max(best, abs(corr))
        return best

    def _remember(self, signature: Optional[np.ndarray]) -> None:
        if signature is None or self.value_threshold <= 0:
            return
        if len(self._value_signatures) >= self.value_archive_limit:
            return
        self._value_signatures.append(signature)

    def filter_redundant(self, candidates: List[FactorExpression], data: Any, preserved_candidates: List[FactorExpression] = None) -> List[FactorExpression]:
        unique_candidates = []
        preserved_set = set(preserved_candidates) if preserved_candidates else set()

        # Elites bypass both layers, but their values still define what counts as
        # redundant for the candidates generated this epoch.
        for elite in preserved_set:
            self._remember(self._signature(elite, data))

        for cand in candidates:
            # 1. 优先保留上一代保留下来的精英父代个体，不过滤
            if cand in preserved_set:
                unique_candidates.append(cand)
                continue
                
            # 2. 语法/语义层面的硬去重（通过 Hash 机制）
            source_str = str(cand.get_source())
            expr_hash = hashlib.md5(source_str.encode()).hexdigest()
            if expr_hash in self.archive_hashes:
                continue

            # 3. 面值软去重：同一信号的不同写法
            if self.value_threshold > 0:
                signature = self._signature(cand, data)
                corr = self._max_abs_corr(signature)
                self.max_observed_corr = max(self.max_observed_corr, corr)
                if corr > self.value_threshold:
                    self.value_rejected += 1
                    logger.debug(
                        "Soft dedup rejected %s (|corr|=%.3f > %.3f)",
                        expr_hash[:8], corr, self.value_threshold,
                    )
                    continue
                self._remember(signature)

            self.archive_hashes.add(expr_hash)
            cand.logic_hash = expr_hash
            unique_candidates.append(cand)
            
        return unique_candidates

    def summary(self) -> Dict[str, Any]:
        return {
            "value_threshold": self.value_threshold,
            "archive_size": len(self._value_signatures),
            "value_rejected": self.value_rejected,
            "max_observed_corr": round(self.max_observed_corr, 4),
        }

class BaseFactorMiner(ABC):
    """
    因子挖掘的通用基类范式
    适用于 GP (遗传), RL (强化学习), LLM (大模型), NN (神经网络)
    """
    
    def __init__(self, data: Any, config: Dict):
        self.data = data           
        self.config = config       
        self.state = MinerState()
        # 预留统一的回测评价器接口（由外部 Director 或自身子类初始化时注入）
        self.evaluator = None 
        # 全局名人堂（Hall of Fame）：记录历史上出现过的全量最高分因子
        self.hall_of_fame: List[Dict[str, Any]] = []

    def _update_hall_of_fame(self, candidate: FactorExpression, score: float, max_hof_size: int = 100):
        """记录并维护全局名人堂"""
        source_str = str(candidate.get_source())
        cand_hash = hashlib.md5(source_str.encode()).hexdigest()
        
        # 避免同一因子重复进入名人堂
        for item in self.hall_of_fame:
            if item["hash"] == cand_hash:
                return
                
        self.hall_of_fame.append({
            "score": score,
            "hash": cand_hash,
            "candidate": candidate
        })
        self.hall_of_fame.sort(key=lambda x: x["score"], reverse=True)
        if len(self.hall_of_fame) > max_hof_size:
            self.hall_of_fame = self.hall_of_fame[:max_hof_size]
        
    @abstractmethod
    def initialize_search_space(self) -> None:
        """
        1. 初始化搜索空间 / 环境
        """
        pass

    @abstractmethod
    def generate_candidates(self) -> List[FactorExpression]:
        """
        2. 生成候选因子 (Propose)
        @return: 严格返回 FactorExpression 对象的列表
        """
        pass

    @abstractmethod
    def evaluate_candidates(self, candidates: List[FactorExpression]) -> EvaluationFeedback:
        """
        3. 评价与打分 (Evaluate / Reward)
        - GP/LLM/RL: 返回传统的指标（IC、夏普等）。
        - NN: 携带计算图 Tensor 原样返回，供 update_model 梯度计算。
        """
        pass

    @abstractmethod
    def update_model(self, candidates: List[FactorExpression], feedback: EvaluationFeedback) -> None:
        """
        4. 反馈与模型更新 (Feedback & Learn)
        - GP: 更新种群 (self.state.population = new_trees)
        - LLM: 更新反思记忆 (self.state.failed_reflections.append(...))
        - NN: 执行反向传播 (loss.backward(); optimizer.step())
        """
        pass

    def _factor_panel(self) -> Any:
        """The panel factors are computed on (the client wraps it behind get_data)."""
        data = self.data
        getter = getattr(data, "get_data", None)
        if callable(getter):
            try:
                return getter()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Value dedup unavailable (panel load failed): %s", exc)
                return None
        return data

    def mine(self, n_iterations: int | None, progress_callback=None) -> List[FactorExpression]:
        """
        5. 标准的主循环引擎 (Main Loop) - 集成去重拦截器
        """
        if n_iterations is None:
            n_iterations = int(self.config.get("max_iterations", 5))
        logger.info(f"[{self.__class__.__name__}] Starting mining loop for {n_iterations} iterations.")
        self.initialize_search_space()
        
        initial_hashes = set()
        if hasattr(self, 'storage_client') and self.storage_client:
            try:
                initial_hashes = self.storage_client.get_all_logic_hashes()
                logger.info(f"Loaded {len(initial_hashes)} global deduplication hashes from storage.")
            except Exception as e:
                logger.warning(f"Failed to load global hashes: {e}")
                
        div_filter = DiversityFilter(
            self.config.get('max_corr', 0.7),
            initial_hashes=initial_hashes,
            # Soft dedup is ON by default: expression-level MD5 cannot see that
            # `rank(close)` and `zscore(close)` are the same signal. Set
            # `dedup_value_threshold` to 0 to disable it.
            value_threshold=float(self.config.get('dedup_value_threshold', 0.99)),
            value_archive_limit=int(self.config.get('dedup_value_archive', 40) or 40),
        )

        
        dedup_panel = self._factor_panel() if float(self.config.get('dedup_value_threshold', 0.99)) > 0 else None

        for epoch in range(n_iterations):
            self.current_epoch = epoch
            # 1. 提案
            raw_candidates = self.generate_candidates()
            
            # 1.5 拦截过滤：不过滤上一代保留的种群精英，只去重全新生成的表达式
            preserved = getattr(self.state, "population", None)
            candidates = div_filter.filter_redundant(
                raw_candidates,
                dedup_panel if dedup_panel is not None else self.data,
                preserved_candidates=preserved,
            )
            if not candidates:
                logger.warning(f"Epoch {epoch}: All candidates filtered out as redundant.")
                if progress_callback:
                    progress_callback(epoch + 1, n_iterations, None)
                continue
            
            # 2. 判卷
            feedback = self.evaluate_candidates(candidates)
            
            # 2.5 搜集本轮高分因子放入名人堂 (Hall of Fame)
            if feedback:
                for cand in candidates:
                    result = feedback.for_candidate(cand)
                    if result and result.succeeded and result.metrics:
                        metrics = result.metrics
                        score = metrics.get("fitness_score", 0.0)
                        cand.metrics = metrics
                        self._update_hall_of_fame(cand, score)
            
            # 3. 学习：update_model 负责更新内部的 self.state 或模型参数
            self.update_model(candidates, feedback)
            
            self._log_epoch(epoch, candidates, feedback.metrics)
            
            if progress_callback:
                best_factor = self._get_best_factors()[0] if self._get_best_factors() else None
                progress_callback(epoch + 1, n_iterations, best_factor)
            
        dedup_report = div_filter.summary()
        if dedup_report["value_rejected"] or dedup_report["value_threshold"] > 0:
            logger.info(
                "Diversity filter summary: threshold=%.3f, archive=%d, "
                "rejected_by_value=%d, max_observed_corr=%.3f",
                dedup_report["value_threshold"],
                dedup_report["archive_size"],
                dedup_report["value_rejected"],
                dedup_report["max_observed_corr"],
            )
        self.dedup_summary = dedup_report

        candidates = self._get_best_factors()
        return self._finalize(candidates)

    def _finalize(self, candidates: List[FactorExpression]) -> List[FactorExpression]:
        """Final-stage assembly.

        Two selectable strategies (config ``finalize_method``):

        * ``"linear"`` (default) — fit an in-sample ridge combination of the
          top-K diverse sub-factors (AlphaGen / Alpha101 style) and prepend it
          as the primary recommended factor.
        * ``"alphapool"`` (Phase 2) — feed the discovered candidates into a
          fixed-capacity :class:`AlphaPool`: redundant factors (mutual-IC >
          threshold) are rejected, and the whole-pool linear weights are
          re-optimized to maximize *combined* IC. The resulting combination is a
          :class:`LinearCombinedFactor` with the pool's fitted weights.

        Both strategies train only on in-sample data, so the OOS RankIC the
        evaluator computes afterward honestly reflects generalization.
        """
        cfg = self.config
        method = cfg.get("finalize_method", "linear")
        if method == "alphapool" and self.evaluator is not None and self.data is not None:
            try:
                data = self.data.get_data()
                returns = self.data.get_returns()
                if data is not None and returns is not None:
                    result = self._finalize_alphapool(candidates, data, returns)
                    if result is not None:
                        return result
            except Exception as exc:
                logger.warning("AlphaPool finalize failed, falling back to linear: %s", exc)
        # Default / fallback: in-sample ridge linear combination.
        return self._finalize_linear(candidates)

    def _finalize_linear(self, candidates: List[FactorExpression]) -> List[FactorExpression]:
        """In-sample ridge combination of the top-K diverse sub-factors."""
        cfg = self.config
        lc = cfg.get("linear_combine", None)
        if lc is None:
            # GP defaults to ON; other paradigms OFF (NN sub-factors aren't pandas).
            lc = (cfg.get("paradigm", "") == "GP")
        if not lc:
            return candidates
        if self.evaluator is None or self.data is None:
            return candidates
        try:
            data = self.data.get_data()
            returns = self.data.get_returns()
        except Exception as exc:
            logger.warning("Linear combination skipped (data unavailable): %s", exc)
            return candidates
        if data is None or returns is None:
            return candidates

        top_k = int(cfg.get("linear_combine_top_k", 3))
        corr_thresh = float(cfg.get("linear_combine_corr_thresh", 0.7))
        lam = float(cfg.get("linear_combine_lambda", 1.0))

        selected = self._select_diverse_combo_candidates(
            candidates, top_k, corr_thresh, data, returns
        )
        if len(selected) < 2:
            return candidates

        try:
            combined = fit_linear_combination(selected, data, returns, reg_lambda=lam)
            fb = self.evaluator.evaluate([combined])
            res = fb.for_candidate(combined)
            if res and res.succeeded and res.metrics:
                combined.metrics = res.metrics
            logger.info(
                "Linear combination produced with %d sub-factors; weights=%s",
                len(selected),
                [round(float(w), 4) for w in combined.weights],
            )
            return [combined] + selected
        except Exception as exc:
            logger.warning("Linear combination finalize failed: %s", exc)
            return candidates

    def _finalize_alphapool(
        self,
        candidates: List[FactorExpression],
        data: Any,
        returns: Any,
    ) -> Optional[List[FactorExpression]]:
        """Phase 2: build a static AlphaPool from the discovered candidates.

        Returns the combined factor prepended to its members, or ``None`` when
        the pool could not assemble at least two non-redundant members (so the
        caller falls back to the linear strategy).
        """
        cfg = self.config
        capacity = int(cfg.get("alphapool_capacity", 10))
        mutual_max = float(cfg.get("alphapool_mutual_ic_max", 0.99))
        shift = int(cfg.get("alphapool_weight_fit_shift", 21))
        reg = float(cfg.get("alphapool_reg_lambda", 5.0))
        top_m = int(cfg.get("alphapool_top_m", 30))

        if isinstance(returns, pd.DataFrame):
            ret_idx, ret_cols = returns.index, returns.columns
            ret_arr = returns.to_numpy(dtype=float, na_value=float("nan"))
        else:
            ret_arr = np.asarray(returns, dtype=float)
            ret_idx = ret_cols = None

        pool = AlphaPool(
            capacity=capacity,
            mutual_ic_max=mutual_max,
            weight_fit_shift=shift,
            reg_lambda=reg,
        )
        pool.set_target(ret_arr, ret_idx, ret_cols)

        expr_by_id: Dict[str, FactorExpression] = {}
        valid = [
            c for c in candidates
            if getattr(c, "metrics", {}).get("fitness_score", -1e9) > -1e8
        ]
        ranked = sorted(
            valid,
            key=lambda c: float(c.metrics.get("fitness_score", -1e9)),
            reverse=True,
        )
        for c in ranked[:top_m]:
            try:
                f = c.compute(data)
            except Exception:
                continue
            if f is None:
                continue
            fid = c.logic_hash or str(id(c))
            expr_by_id[fid] = c
            pool.admit(fid, f, ret_idx, ret_cols)

        if pool.report()["size"] < 2:
            return None

        selected = [expr_by_id[i] for i in pool._ids if i in expr_by_id]
        weights = pool._weights
        if len(selected) != len(weights) or not selected:
            return None

        combined = LinearCombinedFactor(sub_expressions=selected, weights=weights)
        fb = self.evaluator.evaluate([combined])
        res = fb.for_candidate(combined)
        if res and res.succeeded and res.metrics:
            combined.metrics = res.metrics
        logger.info(
            "AlphaPool finalize: %d members, pooled RankIC=%.4f, weights=%s",
            pool.report()["size"], pool.pooled_rankic(),
            [round(float(w), 4) for w in weights],
        )
        return [combined] + selected

    def _select_diverse_combo_candidates(
        self,
        candidates: List[FactorExpression],
        top_k: int,
        corr_thresh: float,
        data: Any,
        returns: Any,
    ) -> List[FactorExpression]:
        """Pick the top-K fittest factors while keeping them mutually uncorrelated."""
        valid = [
            c for c in candidates
            if getattr(c, "metrics", {}).get("fitness_score", -1e9) > -1e8
        ]
        ranked = sorted(
            valid,
            key=lambda c: float(c.metrics.get("fitness_score", -1e9)),
            reverse=True,
        )
        chosen: List[FactorExpression] = []
        chosen_series = []
        for c in ranked:
            if len(chosen) >= top_k:
                break
            try:
                f = c.compute(data)
            except Exception:
                continue
            if f is None:
                continue
            ok = True
            for s in chosen_series:
                corr = _safe_corr(f, s)
                if corr is not None and abs(corr) > corr_thresh:
                    ok = False
                    break
            if ok or not chosen:
                chosen.append(c)
                chosen_series.append(f)
        # Pad with the next-best if diversity filtering left us with too few.
        if len(chosen) < 2:
            for c in ranked:
                if c not in chosen:
                    chosen.append(c)
                    if len(chosen) >= 2:
                        break
        return chosen[: max(2, top_k)]
        
    def _log_epoch(self, epoch: int, candidates: List[FactorExpression], metrics: List[Dict[str, float]]):
        logger.info(f"Epoch {epoch} completed. Processed {len(candidates)} unique candidates.")
        
    def _get_best_factors(self) -> List[FactorExpression]:
        # 优先从全局名人堂中获取全期最高分的因子列表
        if self.hall_of_fame:
            return [item["candidate"] for item in self.hall_of_fame]
        if hasattr(self.state, "population") and self.state.population:
            return self.state.population
        if hasattr(self.state, "replay_buffer") and self.state.replay_buffer:
            return self.state.replay_buffer
        return []
