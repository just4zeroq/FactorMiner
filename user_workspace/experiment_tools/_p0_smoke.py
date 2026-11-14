import sys, json
import pandas as pd, numpy as np
from core.data_feed.real_client import RealDataClient
from core.evaluation.evaluator import ParallelEvaluator
from core.miner.paradigms.gp_miner import GPFactorMiner
from core.miner.paradigms.rl_miner import RLFactorMiner
from core.miner.expressions import LinearCombinedFactor

config = {
    "paradigm": "GP",
    "linear_combine": True,
    "linear_combine_top_k": 3,
    "linear_combine_lambda": 1.0,
    "population_size": 24,
    "data_feeds": {
        "exchange": "binance", "instrument_type": "futures", "timeframe": "1d",
        "pairs": ["BTC/USDT:USDT"],
        "required_streams": ["open", "high", "low", "close", "volume"],
        "mine_period": [["2024-01-01", "2024-06-30"]],
        "test_period": [["2024-07-01", "2024-12-31"]],
        "mining_mode": "sequential_single",
    },
    "search_space": {
        "allowed_operators": [
            "add", "sub", "mul", "div",
            "ts_mean", "ts_std", "ts_zscore", "ts_rank", "ts_delta", "ts_momentum",
            "ts_ema", "ts_ref", "ts_sum", "ts_var", "ts_median", "ts_mad",
            "ts_skew", "ts_kurt", "ts_quantile", "ts_argmax", "ts_argmin",
            "ts_count_up", "ts_realized_vol", "ts_rsi", "cumsum", "signed_power",
            "ts_corr", "ts_regression", "ts_max2", "ts_min2", "greater", "less",
            "abs", "sign", "log", "sqrt", "exp", "inv", "tanh", "sigmoid", "clamp",
            "cs_rank", "cs_zscore", "cs_neutralize",
        ],
        "terminals": ["close", "open", "high", "low", "volume"],
    },
    "gp": {"max_depth": 3, "windows": [5, 10, 20], "powers": [1.0, 2.0], "parsimony": 0.05},
    "rl": {"learning_rate": 0.05, "traj_len": 4, "entropy_coef": 0.01},
    "fitness": {},
    "evaluation": {"max_workers": 4},
}

dc = RealDataClient(config)
md = dc.get_data(); mr = dc.get_returns()
td = dc.get_test_data(); tr = dc.get_test_returns()
print("mine rows:", None if md is None else len(md), "| returns:", None if mr is None else mr.notna().sum())
print("test rows:", None if td is None else len(td), "| test returns:", None if tr is None else tr.notna().sum())
assert md is not None and len(md) > 50 and td is not None and len(td) > 50

ev = ParallelEvaluator(dc, config)

print("\n=== GP mine (3 iters, with linear-combination finalize) ===")
gp = GPFactorMiner(dc, config)
gp.evaluator = ev
gp_cands = gp.mine(3)
gp_combo = [c for c in gp_cands if isinstance(c, LinearCombinedFactor)]
assert gp_combo, "GP should emit a LinearCombinedFactor"
combo = gp_combo[0]
print(f"GP returned {len(gp_cands)} candidates; combined factor weights={[round(w,4) for w in combo.weights]}")
print(f"  combined fitness={combo.metrics.get('fitness_score'):.2f} "
      f"IC={combo.metrics.get('RankIC'):.3f} OOS_IC={combo.metrics.get('RankIC_oos'):.3f} "
      f"oos_consistent={combo.metrics.get('oos_consistent')}")
print("   combo:", combo.to_display_string(90))

print("\n=== RL mine (3 iters) ===")
rl = RLFactorMiner(dc, config)
rl.evaluator = ev
rl.initialize_search_space()
for it in range(3):
    cands = rl.generate_candidates()
    fb = ev.evaluate(cands)
    rl.update_model(cands, fb)
    best = max(cands, key=lambda c: c.metrics.get("fitness_score", -1e9))
    print(f"iter {it}: best_fit={best.metrics.get('fitness_score'):.2f} "
          f"IC={best.metrics.get('IC'):.3f} OOS_IC={best.metrics.get('RankIC_oos'):.3f}")
    print("   e.g.", best.to_display_string(70))

print("\nALL P0 INTEGRATION CHECKS PASSED")
