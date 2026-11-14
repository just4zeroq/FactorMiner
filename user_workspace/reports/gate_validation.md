# 闸门验证报告 · 旧因子复核（Part 1）

## 方法
- 6 个曾在 `INSPECTED` 状态、但严格 OOS（2025H1）全亏的 GP 因子。
- 从 `factor_db/values` 与 `factor_db_oos/values` 读取样本内/样本外面板的因子值与前瞻收益，
  用与 `ParallelEvaluator._build_verdict` 完全一致的算法重算 `hac_t / p_value / dsr / cost_adj_return`，
  并按**样本内学出的方向**先定向（负 IC 因子等价于反向多空，不再被符号误杀），
  再用样本内/样本外 RankIC 符号一致性判定 `oos_consistent`。
- DSR 的 `n_trials` 取 **787**（历史运行未记录真实试验数，用 P0 的 787 次搜索预算作保守代理）。
- `cost_adj_return` 为**收益口径**（P0.5）：定向后的分位多空组合（gross 1.0、美元中性、top/bottom 20%）在 18 bp 往返成本、5 日调仓下的每期净收益。
- 调用真实的 `apply_promotion_gates`（含 BH-FDR 多重检验校正）给出 `lifecycle_status`。

## 结果
**6/6 个因子被闸门拦截（DISCOVERED_UNVERIFIED）。**

| 因子 | 样本内RankIC | OOS RankIC | HAC t | DSR | 成本后收益 | OOS一致 | OOS强度 | 闸门结论 |
|---|---|---|---|---|---|---|---|---|
| fac_c25e28c3 | 0.0673 | 0.0576 | 3.34 | 0.413 | -0.0002 | True | True | DISCOVERED_UNVERIFIED |
| fac_8af952ab | 0.0647 | 0.0555 | 3.40 | 0.443 | -0.0004 | True | True | DISCOVERED_UNVERIFIED |
| fac_b66d17ce | 0.0633 | 0.0545 | 3.09 | 0.349 | -0.0001 | True | True | DISCOVERED_UNVERIFIED |
| fac_099a7eef | 0.0489 | 0.0254 | 3.71 | 0.403 | 0.0000 | True | True | DISCOVERED_UNVERIFIED |
| fac_f20a1349 | 0.0403 | 0.0573 | 3.29 | 0.395 | -0.0005 | True | True | DISCOVERED_UNVERIFIED |
| fac_2017455e | 0.0420 | 0.0013 | 3.23 | 0.248 | 0.0002 | True | False | DISCOVERED_UNVERIFIED |

## 逐因子拦截原因
- **fac_c25e28c3**：DSR<0.90（无真 alpha）；成本后收益<=0（无 alpha） → DISCOVERED_UNVERIFIED
- **fac_8af952ab**：DSR<0.90（无真 alpha）；成本后收益<=0（无 alpha） → DISCOVERED_UNVERIFIED
- **fac_b66d17ce**：DSR<0.90（无真 alpha）；成本后收益<=0（无 alpha） → DISCOVERED_UNVERIFIED
- **fac_099a7eef**：DSR<0.90（无真 alpha） → DISCOVERED_UNVERIFIED
- **fac_f20a1349**：DSR<0.90（无真 alpha）；成本后收益<=0（无 alpha） → DISCOVERED_UNVERIFIED
- **fac_2017455e**：DSR<0.90（无真 alpha）；OOS |RankIC| 跌破阈值（样本外失效） → DISCOVERED_UNVERIFIED

## 结论与重要更正
在真实搜索预算（n_trials=787）下，6 个因子全部被 **DSR<0.90** 拦在 `DISCOVERED_UNVERIFIED`。
它们的 HAC t / BH-FDR / OOS 方向 / OOS 强度都能过，卡住的是‘这么多轮搜索后还剩下多少可信度’。

**更正**：早期版本的复核脚本在算 DSR / 成本后收益时保留了 IC 的原始符号，
导致所有负 IC 因子被‘符号误杀’，于是得到‘6/6 拦截’的结论。
修正定向后，若用宽松分母（n_trials=1），6 个里有 5 个能过闸门 —— 说明之前那次‘全拦’
并非闸门真的识别了失效因子，而是符号 bug 的副作用。

**分母敏感性**：n_trials=84（当前库规模）仍 6/6 拦截（DSR 0.50–0.70）；
只有把分母降到个位数，部分因子才会过线。真正起作用的是 DSR 的搜索预算。

**更重要的限制**：即使闸门在 IC 层面判定显著，2025H1 严格 OOS 回测仍然亏损 11.18%。
P0 复验（`user_workspace/reports/p0_holdout/README.md`）复现了同一现象：
OOS RankIC 显著（t≈4.2）但含成本组合 -6.4%。因此‘IC 显著’不能替代‘组合可交易’，
下一步必须把成本闸门改成收益口径（见 optimization_plan 0c 的 P0.5）。
