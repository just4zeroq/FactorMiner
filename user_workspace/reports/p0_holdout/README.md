# P0 真 OOS 复验报告（2025-07 → 2026-11）

> 目的：回答 Phase 1 验收里唯一没有正面证据的问题 —— **通过统计闸门的因子，在从未看过的区间上是否还站得住？**
> 结论先行：**统计显著性站住了（OOS |t| 4.1–5.5，无方向翻转），但经济价值没有站住（通过闸门的组合含成本 -6.4%）。** 闸门是有效的"假阳性过滤器"，但不是"收益排序器"。

---

## 1. 实验设计（跑之前固定，未按结果调整）

| 项 | 设定 |
|---|---|
| 数据 | Binance USDT 永续，20 标的，1d，2023-01→2026-11 |
| 挖掘集（in-sample） | 2023-04-01 → 2024-12-31（641 天，ARB 上市后共同区间） |
| 验证集（挖掘期可见） | 2025-01-01 → 2025-06-30（181 天，用于 OOS fitness 与 `oos_strength` 闸门） |
| **严格 holdout（一次性）** | **2025-07-01 → 2026-11-13（500 天，此前任何实验均未使用）** |
| 搜索预算 | GP population 60 × 20 代，实际评估 **787** 个候选（去重后） |
| 算子 | 33 个（四则、滚动统计、时序回归/相关、截面、变换），未用 sin/cos 等噪声算子 |
| 闸门 | NW-HAC t≥2、BH-FDR q≤0.05、DSR≥0.90、OOS 同向、OOS \|RankIC\|≥0.02、成本后为正 |
| 多重检验分母 | `trace.num_trials + 本轮评估数` = **787**（DSR 用此重算） |
| 结果变量 | 严格 OOS 回测：20 标的、5 日调仓、多空各 5、佣金 4bp + 滑点 5bp + 资金费 1bp/日 |

- 配置：`user_workspace/configs/configOOSHoldout.json`
- 产物：`factor_db_p0`（样本内）、`factor_db_p0_oos`（holdout 重算值）、`user_workspace/reports/p0_holdout/`
- 一次性账本：`user_workspace/.holdout_ledger_p0.json`（已记录 2025-07-01..2026-11-13，不可再次揭示）

## 2. 闸门判定（样本内 + 验证集）

| 因子 | 样本内 RankIC | 验证集 \|RankIC\| | HAC t | IR | DSR | q 值 | 方向 | 判定 |
|---|---|---|---|---|---|---|---|---|
| fac_eb287869 | -0.0783 | 0.0902 | 5.88 | 0.239 | **0.987** | 4.0e-09 | -1 | **INSPECTED** |
| fac_0d870d2b | -0.0694 | 0.0828 | 6.62 | 0.263 | **0.997** | 3.7e-11 | -1 | **INSPECTED** |
| fac_0edecf9e | -0.0659 | 0.0895 | 4.61 | 0.190 | 0.849 | 4.1e-06 | -1 | DISCOVERED_UNVERIFIED（DSR 差 0.05） |
| fac_17c6b458 | -0.0368 | 0.0349 | 4.11 | 0.149 | 0.512 | 3.9e-05 | -1 | DISCOVERED_UNVERIFIED（线性组合） |

> 4 个候选的样本内 IC 全为负、验证集同向，方向由样本内学出（`orientation=-1`）。闸门据此定向后再算 DSR / 成本，否则所有负向因子都会被符号误杀 —— 这是本次修掉的真实缺陷之一。

## 3. 严格 OOS：IC 是否存活

| 因子 | holdout mean RankIC | IR（定向后） | HAC t | p | 状态 |
|---|---|---|---|---|---|
| fac_eb287869 | -0.0574 | 0.183 | -4.32 | 1.6e-05 | 通过 |
| fac_0d870d2b | -0.0504 | 0.165 | -4.17 | 3.0e-05 | 通过 |
| fac_0edecf9e | -0.0880 | 0.221 | -5.45 | 5.2e-08 | 被拒 |
| **通过组等权组合** | **+0.0577** | 0.184 | **+4.11** | — | 2 因子 |

**IC 层面结论**：3 个可重算的候选全部在 holdout 上保持了同向、显著的 RankIC（\|t\| 4.1–5.5），**没有一个方向翻转**。对比旧 6 因子（2025H1 严格 OOS：\|RankIC\| 从 0.0568 衰减到 0.0280，2/6 方向翻转，组合 -11.18%），这是明确的改善。

## 4. 严格 OOS：组合是否赚钱（含成本）

| 组合 | 区间 | 总收益 | Sharpe | 最大回撤 | 总成本 | 换手 |
|---|---|---|---|---|---|---|
| **通过闸门的 2 因子**（含成本） | 2025-07→2026-11 | **-6.40%** | -0.07 | 23.8% | 8.62% | 0.212 |
| 通过闸门的 2 因子（零成本） | 同上 | +2.22% | +0.16 | 22.0% | 0 | 0.212 |
| 被拒的 fac_0edecf9e（含成本） | 同上 | **+23.35%** | +0.57 | 20.4% | 6.31% | 0.122 |
| 被拒的 fac_0edecf9e（零成本） | 同上 | +29.73% | +0.68 | 20.4% | 0 | 0.122 |
| 旧 6 因子（历史对照，含成本） | 2025H1 | -11.18% | -1.14 | 15.8% | 3.43% | — |

**经济层面结论**：
1. 通过闸门的组合**毛收益仅 +2.2%**（年化约 1.3%），远不足以覆盖 8.6% 的交易成本，净亏 6.4%。
2. 被拒的因子反而净赚 23.4% —— 它的样本内 IR 只低 0.05，但 holdout 的 IC 幅度更大（0.088 vs 0.05）、换手更低（0.122 vs 0.212）。
3. 分位诊断解释了原因：通过组信号的十分位收益**不单调**，多空尾部价差只有 +0.006%/日（年化 ~1.5%），RankIC 的显著性主要来自排序一致性而非尾部收益幅度。**RankIC 显著 ≠ 可交易**。

## 5. 本次修掉的三个真实缺陷（都在闸门自身）

1. **DSR 的 `n_trials` 恒为 1** → DSR 退化成普通 Sharpe 检验。改为 `trace.num_trials + 本轮评估数`，并把 `dsr/n_trials` 写回指标。
2. **DSR 基准量纲错误**：`expected_max_sr(n_trials)` 用单位标准差，而 `ir` 是逐期 IC 比率（~0.2），两者不同量纲；改为按 `1/√n_obs` 缩放（Bailey–López de Prado）。
3. **负 IC 被符号误杀**：`ir`/`cost_adj_return` 用带符号均值，导致所有负向因子 DSR=0、成本后为负。改为先按样本内方向定向再计算，并记录 `orientation`。
4. 附带：`evaluation.gates` 配置此前根本没传进 `promote_gates`（阈值只能改代码）；CLI 不带 `--iterations` 时传 `None` 给 `miner.mine()` 直接崩。均已修。

## 6. 诚实的限制

- **样本极小**：一次运行、4 个最终候选、1 个随机种子。"被拒因子更好"完全可能是运气，不能据此说闸门有害，也不能说它有效排序。
- **闸门的成本口径仍是 IC 而非收益**：`cost_adj_return = mean_ic - cost_rate*turnover`，量纲上 IC 与成本不可直接相减；本次 `gate_cost_rate=0`，该闸门实际只检查 IC>0。真正的成本判断来自回测。
- **holdout 已消耗**：2025-07→2026-11 已被本次一次性揭示。任何后续闸门改动都不能再用它调参，只能等新数据（2026-12 之后）或 paper trading。
- **线性组合无法 OOS 重算**：`FactorCompiler` 不支持 `linear_combination` 逻辑类型，fac_17c6b458 被跳过。这是工具缺口，不是因子失败。
- 数据修复：BTC 日线文件混入 31 根 08:00 重复 K 线（2025-07-20→08-19），已备份并清理（`.bak-20251114`），否则会污染 holdout 截面。

## 7. 下一步（按优先级）

1. **收益口径的成本闸门**：用因子分位多空收益（减去换手成本）替代 IC 做 `cost_adj_return`，并在新的、未被看过的区间上预注册验证。
2. **幅度感知的筛选**：DSR/IR 奖励稳定性，会偏向"稳定但很小"的信号；组合层应同时约束 `mean |IC|` 或尾部价差。
3. **支持 linear combination 的 OOS 重算**，否则 Phase 2 的组合因子无法进入严格 OOS。
4. **单标的时序闸门**（既有 P1）：sequential 模式 verdict 恒中性，RL/NN/单币 GP 仍无法晋升。

---

**复现命令**

```bash
python -m core.cli mine --miner GP --config user_workspace/configs/configOOSHoldout.json
python user_workspace/experiment_tools/regate_p0.py --db-root factor_db_p0 --apply
python user_workspace/experiment_tools/recompute_oos_factors.py \
  --src-root factor_db_p0 --db-root factor_db_p0_oos \
  --factors fac_eb287869,fac_0d870d2b,fac_0edecf9e \
  --oos-start 2025-07-01 --oos-end 2026-11-13 --warmup-start 2024-07-01
python -m core.cli backtest --db-root factor_db_p0_oos --orientation-db-root factor_db_p0 \
  --factors fac_eb287869,fac_0d870d2b --start 2025-07-01 --end 2026-11-13 \
  --output user_workspace/reports/p0_holdout/approved_costs
```
