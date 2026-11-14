import logging
import math
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from core.evaluation.targets import ForwardReturnTarget
from core.evaluation.annualisation import DEFAULT_ANNUALISATION

logger = logging.getLogger(__name__)


class InspectorMetricEngine:
    """
    因子审查指标引擎：计算全维度的因子统计量，包含 IC/RankIC、IR、t-stat、Lag 衰减及分组多空收益。
    """

    @staticmethod
    def calculate_comprehensive_metrics(
        factor_values: Any,
        returns: Any,
        close_prices: Any = None,
        lags: List[int] = [1, 2, 3, 5, 10],
        n_quantiles: int = 5,
        price_data: Any = None,
        target_spec: ForwardReturnTarget | Dict[str, Any] | None = None,
        annualisation: float | None = None,
    ) -> Dict[str, Any]:
        """
        计算全套审查指标字典。

        ``annualisation`` only scales the reported IR; pass a value derived
        from the run's timeframe for 7x24 crypto bars (365 / 2190 / 8760).
        """
        ann = float(annualisation) if annualisation and annualisation > 0 else DEFAULT_ANNUALISATION
        result = {
            "pearson": {},
            "spearman": {},
            "decay": {},
            "quantiles": {},
            "quantile_sections": 0,
            "turnover": 0.0,
            "coverage": 0.0,
            "total_bars": 0,
            "valid_bars": 0,
        }

        if factor_values is None or returns is None:
            return result

        # 兼容性整理
        if isinstance(factor_values, pd.DataFrame) and isinstance(returns, pd.DataFrame):
            return InspectorMetricEngine._evaluate_cross_asset(
                factor_values, returns, lags, n_quantiles, ann
            )
        
        if not isinstance(factor_values, pd.Series):
            factor_values = pd.Series(factor_values)
        if not isinstance(returns, pd.Series):
            returns = pd.Series(returns)

        # 1. 基础有效性与覆盖率
        total_bars = len(factor_values)
        valid_mask = factor_values.notna() & returns.notna()
        valid_bars = int(valid_mask.sum())
        coverage = valid_bars / total_bars if total_bars > 0 else 0.0

        result["total_bars"] = total_bars
        result["valid_bars"] = valid_bars
        result["coverage"] = coverage

        if valid_bars < 10:
            logger.warning("Too few valid bars (%d) for inspection.", valid_bars)
            return result

        clean_f = factor_values[valid_mask]
        clean_r = returns[valid_mask]

        # 2. 逐 bar / 滚动 相关性 (Rolling / Overall)
        # 为评估 IC 稳定性，计算 20-bar 滚动相关性序列
        rolling_pearson = clean_f.rolling(20, min_periods=10).corr(clean_r).dropna()
        rank_f = clean_f.rank()
        rank_r = clean_r.rank()
        rolling_spearman = rank_f.rolling(20, min_periods=10).corr(rank_r).dropna()

        # 如果总体计算：
        p_ic = clean_f.corr(clean_r, method="pearson")
        s_ic = clean_f.corr(clean_r, method="spearman")

        def calc_stats(series: pd.Series, overall_val: float):
            if series.empty or len(series) < 2:
                mean_v = overall_val if pd.notna(overall_val) else 0.0
                std_v = 0.0
            else:
                mean_v = float(series.mean())
                std_v = float(series.std())

            n = len(series) if not series.empty else 1
            ir = (mean_v / std_v * math.sqrt(ann)) if std_v > 1e-9 else 0.0
            t_stat = (mean_v / (std_v / math.sqrt(n))) if std_v > 1e-9 and n > 1 else 0.0
            pos_ratio = (series > 0).mean() if not series.empty else (1.0 if overall_val > 0 else 0.0)

            return {
                "overall": float(overall_val) if pd.notna(overall_val) else 0.0,
                "mean": mean_v,
                "std": std_v,
                "ir": ir,
                "t_stat": t_stat,
                "pos_ratio": float(pos_ratio),
            }

        result["pearson"] = calc_stats(rolling_pearson, p_ic)
        result["spearman"] = calc_stats(rolling_spearman, s_ic)

        # 3. 因子衰减分析 (Lag IC Decay)
        # 优先保持 entry/exit/return_type 不变，仅改变 horizon。
        if isinstance(price_data, pd.DataFrame) and target_spec is not None:
            normalized_target = (
                target_spec
                if isinstance(target_spec, ForwardReturnTarget)
                else ForwardReturnTarget.from_config(target_spec)
            )
            for lag in lags:
                lag_ret = normalized_target.with_horizon(lag).build(price_data)
                mask_k = factor_values.notna() & lag_ret.notna()
                if mask_k.sum() >= 10:
                    ric_k = factor_values[mask_k].corr(
                        lag_ret[mask_k],
                        method="spearman",
                    )
                    result["decay"][f"Lag_{lag}"] = (
                        float(ric_k) if pd.notna(ric_k) else 0.0
                    )
                else:
                    result["decay"][f"Lag_{lag}"] = 0.0
        elif close_prices is not None and isinstance(close_prices, pd.Series):
            for lag in lags:
                lag_ret = close_prices.pct_change(lag).shift(-lag)
                mask_k = factor_values.notna() & lag_ret.notna()
                if mask_k.sum() >= 10:
                    ric_k = factor_values[mask_k].corr(lag_ret[mask_k], method="spearman")
                    result["decay"][f"Lag_{lag}"] = float(ric_k) if pd.notna(ric_k) else 0.0
                else:
                    result["decay"][f"Lag_{lag}"] = 0.0
        else:
            # 降级备用：以 returns.shift(-(lag-1)) 替代
            for lag in lags:
                lag_r = returns.shift(-(lag - 1))
                mask_k = factor_values.notna() & lag_r.notna()
                if mask_k.sum() >= 10:
                    ric_k = factor_values[mask_k].corr(lag_r[mask_k], method="spearman")
                    result["decay"][f"Lag_{lag}"] = float(ric_k) if pd.notna(ric_k) else 0.0
                else:
                    result["decay"][f"Lag_{lag}"] = 0.0

        # 4. 5-Quantile 分组收益分析
        try:
            quantiles = pd.qcut(clean_f, q=n_quantiles, labels=False, duplicates="drop")
            q_df = pd.DataFrame({"quantile": quantiles, "return": clean_r})
            q_returns = q_df.groupby("quantile")["return"].mean().to_dict()
            
            q_dict = {}
            for q_idx in range(n_quantiles):
                q_dict[f"Q{q_idx+1}"] = float(q_returns.get(q_idx, 0.0))
            
            # 结算 Long-Short 多空价差 (Q5 - Q1)
            q_first = q_returns.get(0, 0.0)
            q_last = q_returns.get(n_quantiles - 1, 0.0)
            q_dict["Long_Short"] = float(q_last - q_first)

            result["quantiles"] = q_dict
        except Exception as e:
            logger.debug("Quantile calculation failed: %s", e)
            result["quantiles"] = {f"Q{i+1}": 0.0 for i in range(n_quantiles)}
            result["quantiles"]["Long_Short"] = 0.0

        # 5. 因子换手率 (Turnover)
        diff_abs = clean_f.diff().abs().mean()
        denom = clean_f.abs().mean() + 1e-9
        result["turnover"] = float(diff_abs / denom) if pd.notna(diff_abs) else 0.0

        return result

    @staticmethod
    def _cross_sectional_quantile_returns(
        factor_values: pd.DataFrame,
        returns: pd.DataFrame,
        n_quantiles: int,
    ) -> Tuple[Dict[str, float], int]:
        """
        截面分位组合收益。

        在每个时间点按因子值把资产排名分成 n_quantiles 组，先求该时点各组
        的前瞻收益均值，再对所有时间点取均值。与时序分支的语义一致（每组的
        平均前瞻收益），区别只是分组发生在横截面上而非把全部 bar 混在一起。

        返回 (分位收益字典, 参与计算的有效截面数)。若没有任何有效截面可用，
        返回全 0 字典和 0，而不是伪造数值。
        """
        n_quantiles = max(2, int(n_quantiles))
        empty = {f"Q{i + 1}": 0.0 for i in range(n_quantiles)}
        empty["Long_Short"] = 0.0

        f, r = factor_values.align(returns, join="inner")
        if f.empty:
            return empty, 0

        # 因子与前瞻收益必须同时有效，避免把缺失收益当 0 计入分组均值
        valid = f.notna() & r.notna()
        f = f.where(valid)
        r = r.where(valid)

        counts = f.notna().sum(axis=1)
        distinct = f.nunique(axis=1, dropna=True)
        # 有效资产数不足分组数时，分位价差会被少数资产放大；因子无区分度时
        # 分组本身没有意义（常数因子），这两种截面都直接跳过
        usable = (counts >= n_quantiles) & (distinct >= n_quantiles)

        bucket_returns: Dict[int, List[float]] = {
            i: [] for i in range(1, n_quantiles + 1)
        }
        used_sections = 0
        for ts in usable[usable].index:
            f_row = f.loc[ts].dropna()
            r_row = r.loc[ts].reindex(f_row.index)
            # rank(method="first") 对并列值做确定性打散，保证每个资产只落一组
            ranks = f_row.rank(method="first")
            buckets = (
                np.ceil(ranks / len(f_row) * n_quantiles)
                .astype(int)
                .clip(lower=1, upper=n_quantiles)
            )
            for q, group_ret in r_row.groupby(buckets).mean().items():
                if pd.notna(group_ret):
                    bucket_returns[int(q)].append(float(group_ret))
            used_sections += 1

        if not used_sections:
            logger.warning(
                "No cross-section has >= %d valid, distinct factor values; "
                "quantile returns are undefined for this factor.",
                n_quantiles,
            )
            return empty, 0

        q_dict = {
            f"Q{i + 1}": (
                float(np.mean(bucket_returns[i + 1]))
                if bucket_returns[i + 1]
                else 0.0
            )
            for i in range(n_quantiles)
        }
        q_dict["Long_Short"] = q_dict[f"Q{n_quantiles}"] - q_dict["Q1"]
        return q_dict, used_sections

    @staticmethod
    def _evaluate_cross_asset(
        factor_values: pd.DataFrame,
        returns: pd.DataFrame,
        lags: List[int],
        n_quantiles: int,
        annualisation: float = DEFAULT_ANNUALISATION,
    ) -> Dict[str, Any]:
        """Cross-Asset 截面评估逻辑"""
        valid_mask = ~factor_values.isna() & ~returns.isna()
        total_bars = factor_values.size
        valid_bars = int(valid_mask.sum().sum())
        coverage = valid_bars / total_bars if total_bars > 0 else 0.0

        # 截面 IC：每个时间点跨资产算 Spearman / Pearson
        p_ics = factor_values.corrwith(returns, axis=1, method="pearson").dropna()
        s_ics = factor_values.corrwith(returns, axis=1, method="spearman").dropna()

        def calc_cs_stats(series: pd.Series):
            if series.empty:
                return {"overall": 0.0, "mean": 0.0, "std": 0.0, "ir": 0.0, "t_stat": 0.0, "pos_ratio": 0.0}
            mean_v = float(series.mean())
            std_v = float(series.std())
            n = len(series)
            ir = (mean_v / std_v * math.sqrt(annualisation)) if std_v > 1e-9 else 0.0
            t_stat = (mean_v / (std_v / math.sqrt(n))) if std_v > 1e-9 and n > 1 else 0.0
            pos_r = float((series > 0).mean())
            return {
                "overall": mean_v,
                "mean": mean_v,
                "std": std_v,
                "ir": ir,
                "t_stat": t_stat,
                "pos_ratio": pos_r,
            }

        turnover = factor_values.diff().abs().mean().mean()

        decay = {}
        for lag in lags:
            lag_r = returns.shift(-(lag - 1))
            cs_decay = factor_values.corrwith(lag_r, axis=1, method="spearman").mean()
            decay[f"Lag_{lag}"] = float(cs_decay) if pd.notna(cs_decay) else 0.0

        quantiles, quantile_sections = (
            InspectorMetricEngine._cross_sectional_quantile_returns(
                factor_values, returns, n_quantiles
            )
        )

        return {
            "total_bars": total_bars,
            "valid_bars": valid_bars,
            "coverage": coverage,
            "pearson": calc_cs_stats(p_ics),
            "spearman": calc_cs_stats(s_ics),
            "decay": decay,
            "quantiles": quantiles,
            "quantile_sections": quantile_sections,
            "turnover": float(turnover) if pd.notna(turnover) else 0.0,
        }
