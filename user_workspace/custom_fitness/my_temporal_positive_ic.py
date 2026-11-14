"""Signed RankIC fitness for the MyTemporalNN learning template."""

from core.miner.registry import EvaluatorRegistry


@EvaluatorRegistry.register_fitness_hook("my_temporal_positive_ic")
def temporal_positive_ic_fitness(factor_values, returns, base_metrics):
    rank_ic = float(base_metrics.get("RankIC", 0.0))
    # 兼容单资产 (Series) 与截面 (DataFrame) 两种因子输出。
    # dropna().count() 在 DataFrame 上返回 Series，int(Series) 会抛 TypeError。
    if hasattr(factor_values, "notna"):
        valid_count = int(factor_values.notna().to_numpy().sum())
        total_count = int(factor_values.size)
        coverage = valid_count / total_count if total_count else 0.0
    else:
        coverage = 1.0
    penalty = (coverage / 0.20) ** 2 if coverage < 0.20 else 1.0
    return rank_ic * 100.0 * penalty
