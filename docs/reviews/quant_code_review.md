# 量化专家视角 · 因子挖掘系统复查（2026-11-14）

> 范围：不重复上一轮「架构/运维」review，聚焦**因子挖掘的统计正确性、执行/成本口径、搜索目标与判定目标的一致性**，以及**代码级 bug**。
> 方法：源码精读（附 `路径:行号`）+ 在最近一次真实运行 `run_20261114T125520Z_2722fb` 的因子快照上做复现。
> 基线：`pytest -q` 447 passed / 1 skipped。

---

## 0. 结论先行

系统主干（OOS 不反灌选优、闸门链路完整、成本单一真相源、时间隔离 purge/embargo）是**成立**的。但本轮发现 **1 个结构性量化缺陷 + 2 个会直接让结论失真的问题**，它们共同解释了"IC 显著、却 0 晋升"的现象：

1. **搜索目标与判定目标不一致**（最核心）：fitness 优化的是 `|RankIC|` 的几何混合，而晋升闸门判定的是**分位多空组合的净收益**。真实运行里 4 个因子 `hac_t≈4.5`（显著）、`RankIC_oos≈0.10`，但 `cost_adj_return<0`（尾部价差为负）。这在数学上是自洽的——**正 RankIC 不蕴含正的分位价差**，所以搜索会系统性地挖出"IC 好看、不可交易"的因子。
2. **验证集污染**：`test_period` 同时被用于 fitness（OOS 混合）和最终 OOS 回测，所谓 "strict_oos" 只表示"方向从训练集学"，不代表评估窗口未被搜索消费。
3. **`n_promoted` 恒为 0**：流水线从 `cand.metrics.get("lifecycle_status")` 读状态，但该字段从未写入 metrics（只写进了 `FactorMetadata`），所以即便闸门全绿，报告也永远显示 0 晋升。

---

## 1. 🔴 高危（会改变结论）

### H1. 搜索目标用 |RankIC|，闸门判定用净多空收益 → 结构性挖出"不可交易因子"
- 证据：
  - `core/evaluation/evaluator.py:444-472`：`blended = rankic_in^(1-λ) * rankic_oos^λ`，`fitness_score = 100*blended*...`，即 fitness 只由 **RankIC 大小**驱动。
  - `core/evaluation/significance.py:311-342` + `core/evaluation/portfolio_eval.py:66-193`：闸门判定的是 `cost_adj_return = mean_return - turnover*cost`（**分位多空组合的净收益**）。
  - 真实运行 `run_20261114T125520Z_2722fb` 的 4 个因子：`hac_t` 4.20–4.65、`RankIC_oos` 0.096–0.108，但 `cost_adj_return` 全部为负（-0.0004 ~ -0.0008/bar，复现见下）。
- 复现（真实快照 `fac_4d219b4c`）：

  ```
  rebalance=1: turnover=0.0109/bar  gross=-0.00038  net=-0.00040  ls_t=-0.59
  ```
  即 **gross 本身就是负的**，不是成本吃掉的。正 RankIC 来自"中段秩相关"，而顶部/底部两端反转 → 分位多空亏钱。
- 影响：这不是 bug，而是**目标函数错配**。只要搜索最大化 RankIC、判定看尾部价差，就会持续产出"统计显著但不可交易"的因子，0 晋升是必然结果，而不是"闸门太严"。
- 建议：把 fitness 改为**与闸门同口径**的可交易指标（如 `cost_adj_return` 的稳健版本，或 `ls_sharpe` + 换手惩罚），至少把 `cost_adj_return`/`ls_t` 作为 fitness 的乘性项或硬约束；RankIC 只作为诊断字段。

### H2. `test_period` 同时进入 fitness 与最终回测 → "OOS" 实为验证集
- 证据：
  - `core/evaluation/evaluator.py:427-429`：只要存在 test split，`oos_enabled` 自动为真，`_build_fitness` 把它混进 `fitness_score`（`:444-457`）。
  - `core/experiment/pipeline.py:402-430`：`oos_prepared` / `backtested` 用的正是同一个 `test_period`（本次运行 `scope=2025-01-01..2025-06-30`），与 fitness 里消费的窗口一致。
  - 回测 summary 里的 `strict_oos: true`（`backtest_engine.py:592`）只表示**方向从 in-sample 快照学**，不代表评估窗口没被搜索用过。
- 影响：回测收益是"被选过的最优候选在同一窗口上的表现"，存在选择性偏差，不能当作无偏 OOS。真正干净的证据只有 strict holdout / 滚动预注册窗口。
- 建议：要么 mining 阶段只用 `mine_period` 算 fitness（OOS 只在最终评估出现一次），要么在报告里把该窗口明确标注为 "validation (used in selection)"，禁止称 OOS；`strict_oos` 改名 `orientation_from_train_only`。

### H3. `n_promoted` 恒为 0（status 从未写入 metrics）
- 证据：
  - `core/experiment/pipeline.py:68-80`：`"status": m.get("lifecycle_status")`，`m = cand.metrics`。
  - `core/miner/director.py:282-291`：只往 `cand.metrics` 写了 `p_adj/dsr/n_trials/empirical_p/gate_pass/gate_checks`，**没有写 `lifecycle_status`**；`lifecycle_status` 只写进了 `FactorMetadata`（`:304`）。
  - 实测该运行的 verdict：`status field: None`，而 `gate_checks` 是完整的。
- 影响：`pipeline._report_text` 的 `n_promoted`、以及任何依赖 `verdict["status"]` 的统计永远是 0，会掩盖真实晋升。
- 修复：`director.py` 在写 metrics 时补 `cand.metrics["lifecycle_status"] = g["status"]`；或 pipeline 改读 `gate_pass`。

---

## 2. 🟠 中危（正确性/口径，需修）

### M1. `ic_series` 用原始 `argsort` 排名，NaN 被当最大值 → 秩污染
- 证据：`core/evaluation/significance.py:37-65`。`_spearman_row` 直接对含 NaN 的数组做 `a.argsort().argsort()`，NaN 排到末尾并被赋最高秩；`ic_series` 未先做 `isfinite` 掩码。
- 复现（真实快照，NaN 占比 8.9%）：

  ```
  fac_4d219b4c: raw ic,t=(0.049, 4.649)  correct-masked=(0.055, 4.869)
  fac_6bff7f15: raw ic,t=(-0.054,-4.20)  correct-masked=(-0.063,-4.52)
  ```
  合成极端场景（缺失与收益相关）可把真实 IC=0.006 抬到 **0.48**。
- 影响：`hac_t/p_value/ir/orientation` 全部基于被污染的 IC；缺失模式与收益相关时会**凭空制造显著性**。当前数据上偏差约 10%（低估），但一旦数据有空档就会放大。
- 修复：`_spearman_row` 内先 `mask=isfinite(a)&isfinite(b)`，仅对有效对求秩；有效数 <3 返回 0。

### M2. `_coverage` 用 `dropna()` 整行删除，散射缺失时覆盖率被严重低估
- 证据：`core/evaluation/evaluator.py:702-713`：`factor_values.dropna().count().sum()/size`。`dropna()` 默认 `how='any'`，只要一行有一个 NaN 就整行删除。
- 复现：500×20、15% 散射 NaN 的面板，真实覆盖率 0.855 → `_coverage()`=**0.044**，触发 `(0.044/0.2)^2≈0.05` 的惩罚，fitness 被压到 5%。
- 现状：本次真实面板是"整块缺失"（晚期上市），`_coverage()`=0.9111 与真实一致，**未触发**；但一旦出现零散停牌/缺 bar 就会误杀。
- 修复：改为 `factor_values.notna().sum().sum()/factor_values.size`。

### M3. 闸门/walk-forward 按 1 bar 再平衡，回测按 5 bar → 换手口径不一致
- 证据：`evaluator.py:104-125` `gate_rebalance_bars` 默认取 `horizon_bars`（=1）；`walk_forward.py:176-203` 每 bar 重排；`backtest_engine.py:500` `rebalance_every=5`，而 `pipeline.py:420-429` 调用 `run_backtest` 未传 `rebalance_every`，落回默认 5。
- 影响：闸门假设的换手是回测的 5 倍。当前因因子排序稳定（turnover≈1%/bar）影响小，但对高换手因子会系统性高估成本、误杀。
- 修复：把 `rebalance_every` 统一从 config 读取，gate/wf/backtest 三处同源。

### M4. 闸门成本不含资金费，回测含真实 funding
- 证据：`portfolio_eval.py:177` `cost = turnover * cost_rate`，`cost_rate=18bp` 只含 commission+slippage；`backtest_engine.py:546-552/584` 真实 8h funding 计入 P&L。
- 影响：加密货币多空组合的资金费可能远大于手续费；闸门会**高估**净收益（尤其多头持有高资金费永续），造成"闸门过、回测爆"。
- 修复：闸门成本加入 funding 项（用 `crypto_streams` 的资金费均值），或在闸门里显式声明不含资金费并在报告标注。

### M5. 标签用 `current_close` 入场，回测用 NEXT_BAR 成交 → 差一根 bar
- 证据：`core/evaluation/targets.py:111-118`（`entry_price="current_close"` → `close[t+h]/close[t]`）；`backtest_engine.py:443-445`（`ExecutionMode.NEXT_BAR`, `CLOSE`）。
- 影响：训练目标从 `t` 开始，实盘从 `t+1` 开始；对短周期反转类因子会系统性高估。闸门与回测的收益口径也不一致。
- 修复：若坚持 next-bar 成交，target 改 `entry_price="next_open"` 或统一为 `open[t+1]→close[t+1+h]`；否则回测改同 bar 收盘成交。

### M6. `_finalize_linear` 丢弃名人堂，每次运行只产出 ~4 个因子
- 证据：`base.py:390-407`：`selected = _select_diverse_combo_candidates(..., top_k=3)`，`return [combined] + selected`。而 `mine()` 传入的是 `_get_best_factors()`（名人堂，最多 100 个，`base.py:537-545`）。
- 现状：GP 默认 `linear_combine=True`（`base.py:372`），config 未覆盖 → 实际只返回 1 组合 + ≤3 成员。运行结果 `n_candidates=4` 印证。
- 影响：其余 ~96 个已发现因子**既不进闸门也不入库**，多样性、假设覆盖、晋升机会被大幅压缩。
- 修复：`return [combined] + candidates`（前置组合、保留全部），或在 config 里显式 `linear_combine=false` 并另行组合。

### M7. OOS 重算没有 warmup → 滚动类因子损失前期样本
- 证据：`pipeline.py:131-141`：`values = cand.compute(data)`，`data` 只有 `test_period`。`ts_mean(close,20)` 之类算子前 19 根为 NaN，被 `stack/dropna` 丢弃。
- 影响：OOS 回测起点被推迟，样本变少；对长窗口因子可能损失 10%+ 评估区间。
- 修复：在 test 窗口前拼接 `max_window` 根 bar 作为 warmup，计算后再截回评估区间。

### M8. AlphaPool 权重拟合用 21 根位移，目标 horizon=1
- 证据：`pool.py:81` `weight_fit_shift=21` 默认；`base.py:427` 从 `alphapool_weight_fit_shift` 取，默认 21。而 config `target.horizon_bars=1`。
- 影响：组合权重是为"21 根后的收益"拟合的，却在 1 根 horizon 上评估/交易，权重与目标错配。
- 修复：`weight_fit_shift` 默认改为 `horizon_bars`，或显式说明这是"降重叠"手段并单独校准。

### M9. DSR 忽略样本偏度/峰度（固定 skew=0, kurt=3）
- 证据：`significance.py:277-305`：`deflated_sharpe_ratio` 的 `skewness=0.0, kurtosis=3.0` 恒为默认；`evaluator.py:582-584/680-682` 调用时未传真实矩。
- 影响：加密收益厚尾，真实峰度 >>3，DSR 分母偏大 → **DSR 偏乐观**（更容易过）。应把策略净收益序列的样本 skew/kurt 传进去。
- 修复：在 `_build_verdict` 里从 `ls` 收益序列估计 `skew/kurt` 并传入。

---

## 3. 🟡 低危（代码卫生/边角）

- **硬编码用户**：`director.py:295` `user_id="CharlesJ-ABu"`，多用户/审计会串号。
- **方向默认 -1**：`backtest_engine.py:128-134` 样本不足时 `infer_orientation` 静默返回 -1，可能把因子方向判反；应返回 0/抛错并在报告标注。
- **资金费缺失填常数**：`backtest_engine.py:415-423` 缺 bar 时 `fillna(constant)`，会把"无数据"当成"固定费率"，低估/高估都可能。
- **`load_snapshots` 只取第一个文件的 `forward_return`**：`walk_forward.py:104-107` 默认所有因子共用同一收益面板，若因子存储窗口不同会错位。
- **`_stage_combine` 只靠 metadata 识别组合**：`pipeline.py:95-110`，对没有 `member_factor_ids` 的旧组合会漏统计。

---

## 4. 功能缺失（相对成熟因子平台）

1. **只产出组合、不产出因子清单**（M6）：一次运行只暴露 ~4 个候选，难以做因子库沉淀与多因子组合。
2. **无 PIT / 动态成分**：`pairs` 静态，未处理上市/退市时点，存在幸存者偏差；加密虽轻，但新币上市期缺 bar 会失真。
3. **单一预测周期**：只有 `horizon_bars=1`，无多周期（1d/4h/1w）联合评估与 horizon 选择。
4. **无系统性中性化**：只有 `_cs_neutralize` 算子可选，闸门/回测层未对 beta/市值/行业做统一中性化（加密可用市值/板块代理）。
5. **无市场冲击/容量模型**：成本只有固定 bps，无 ADV/冲击成本，无法回答"这个因子能装多少钱"。
6. **无仓位/风险约束**：回测等权分腿，无单币上限、杠杆上限、回撤熔断。
7. **闸门不含资金费**（M4），与回测口径不一致。
8. **walk-forward 只验证组合，不验证闸门**：未在每折上重跑晋升判定，无法回答"策略本身是否稳定"。
9. **无因子退役/监控**：没有 IC 衰减、换手漂移、拥挤度的在线监控。
10. **resume 仅 stage 级**，不恢复 GP 种群/NN epoch。
11. **多用户/权限、分布式评估**仍属 S4/S5。

---

## 5. 建议修复优先级

| 优先级 | 项 | 理由 |
|---|---|---|
| P0 | H1 目标对齐（fitness 纳入可交易净收益） | 不改则永远挖出"IC 好、不赚钱"的因子 |
| P0 | H3 status 写入 metrics | 一行修复，恢复晋升统计 |
| P0 | M1 `ic_series` NaN 掩码 | 统计显著性正确性 |
| P1 | H2 验证集标注/隔离 | 避免把验证集当 OOS 汇报 |
| P1 | M4 闸门含资金费、M3 再平衡同源、M5 成交口径 | 经济口径一致性 |
| P1 | M6 保留名人堂 | 恢复因子产出多样性 |
| P2 | M2 coverage、M7 warmup、M8 shift、M9 DSR 矩 | 边角正确性 |
| P3 | 低危项与功能缺失 | 平台化 |

---

## 6. 复现命令

```bash
# 真实面板上的 NaN 秩污染
python - <<'PY'
import glob,os,numpy as np,pandas as pd
from core.evaluation.significance import ic_series
runs=sorted(glob.glob('user_workspace/runs/*/metrics.json'),key=os.path.getmtime,reverse=True)
rdir=os.path.dirname(runs[0]); fp=sorted(glob.glob(os.path.join(rdir,'factor_db','values','*.parquet')))[0]
df=pd.read_parquet(fp)
f=df.pivot(index='timestamp',columns='asset',values='factor').to_numpy(float)
r=df.pivot(index='timestamp',columns='asset',values='forward_return').to_numpy(float)
print('raw ic mean:', np.nanmean(ic_series(f,r)))
PY
```

---

## 7. 修复执行状态（2026-11-14）

本轮按 P0→P1 顺序修复，全量回归 **463 passed / 1 skipped / 56 subtests**（基线 447，净增 16 个回归测试，无回退）。

| 等级 | 问题 | 修复点（`路径:行号`） | 锁定测试 |
|---|---|---|---|
| P0 | H3 `n_promoted` 恒为 0 | `core/miner/director.py` 把 `g["status"]` 同时写入 `cand.metrics["lifecycle_status"]` | `test_candidate_metrics_carries_lifecycle_status` |
| P0 | M1 `ic_series` NaN 秩污染 | `core/evaluation/significance.py::_spearman_row` 对 a/b 联合有限掩码 | `TestSpearmanNanMask`（2 项） |
| P0 | M2 `_coverage` 整行删除 | `core/evaluation/evaluator.py::_coverage` 改为逐单元格 `isfinite` 计数 | `TestCoverageCellWise`（3 项） |
| P0 | H1 搜索目标≠闸门目标 | `_calculate_built_in_metrics` 用 `long_short_returns` 算 `ls_cost_adj_return`；`_fitness_score` 对「RankIC>0 但多空净收益≤0」乘 `tradeable_floor=0.1` | `TestFitnessTradeablePenalty`（2 项） |
| P1 | M6 名人堂被丢弃 | `core/miner/paradigms/base.py::_finalize_linear` 返回 `[combined] + candidates`（保留全部候选进闸门/入库） | `test_finalize_linear_keeps_all_candidates` |
| P1 | M3 再平衡口径不一致 | `core/experiment/pipeline.py::do_backtest` 传 `rebalance_every = gate_rebalance_bars`（默认 1） | 全量流水线（配置驱动） |
| P1 | M9 DSR 忽略偏度/峰度 | `portfolio_eval.py` 新增 `net_skew/net_kurtosis`（净收益序列）；`evaluator.py` 两处 DSR 与 `significance.py` 闸门 DSR 均传入 | `TestNetMomentsForDSR`（2 项） |
| P1 | M5 标签入场时点错位 | `targets.py` 新增 `next_close`（entry `close[t+1]`，exit `close[t+1+H]`，对齐回测 NEXT_BAR CLOSE）；`configExperiment.json` 启用 | `test_next_close_matches_next_bar_close_execution` |
| P1 | M8 AlphaPool shift 错配 | `pool.py`/`base.py` 默认 `weight_fit_shift` 21→0（对齐 1-bar label） | `test_alphapool_default_shift_matches_native_horizon` |
| P2 | M7 OOS 重算无 warmup | `pipeline.py::_oos_warm_panel` 预热计算后再切回真实测试窗口；`configExperiment.json` 设 `test_warmup_bars=120` | `TestOosWarmup`（2 项） |

### 仍待处理（需设计决策，非纯代码缺陷）

- **M4 闸门成本不含资金费**：修复需把 `funding_rate` 面板穿到 `_build_verdict`，按多空持仓计算每期资金费并加到 `gate_cost_rate`。属数据接入改造，建议单独排期，避免用静态近似引入新的口径偏差。
- **H2 `test_period` 同时用于 fitness 与最终回测**：本质是两段式切分的选择性偏差，需要三段式（train / validation / test）或明确让「正向 OOS 证据」只来自滚动 holdout 账本（2026-12 起）。属实验设计决策，未擅自改动 split 架构。

> 说明：本轮修复均未改变闸门的统计判定语义（仅修正 NaN 边角、目标对齐与口径一致性）。建议以修复后的配置重跑一次八阶段流水线，观察 `n_promoted` 与 `cost_adj_return` 的实际变化。
