# FactorMiner 优化方案（基于业界框架源码调研）

> 调研依据：`docs/research/industry_factor_mining_survey.md`（18 仓库实读源码）
> 诊断前提：真样本外验证（`user_workspace/reports/oos_validation_2025H1.md`）显示 6 个 INSPECTED 因子严格 OOS 全部亏损（总收益 -11.18%、Sharpe -1.14、零成本仍 -7.37%）→ 典型过拟合 / 拟合噪声。

## 0. 核心结论：瓶颈不是算子，是「统计闸门」

调研的横向量化对比表明：

- 我们的算子（59 个）/ 四范式（GP+RL+LLM+NN）/ 末层线性组合，已处于第一梯队，**不需要再扩算子或堆范式**。
- 全 `core/` 检索不到 `FDR/BH/Holm/DeflatedSharpe/purge/embargo/NeweyWest/随机零假设/holdout ledger` —— 即我们**有打分、没有证伪**。这正是 OOS 全亏的根因：factor 在样本内被反复挑选、反复看到 OOS，却从未被"一次性留出 + 多重检验校正"真正证伪过。
- 业界真正成熟且直接对症的两家原型：`crypto-factor-lab`（一次性 holdout ledger + purge + FDR + 成本后入库）与 `LLM-Alpha-Mining-Framework`（NW HAC t + BH-FDR + Romano-Wolf + 随机语法池 max|t| 零分布 + DSR + 8 道 promotion gates）。

**设计原则**：闸门优先于复杂度。在把"统计闸门 + 留出协议"落地并用**新区间**重挖出 OOS 成立的因子之前，**不扩算子、不堆范式、不那时变权重**——否则只是把"重复采样"放大成"更高效的重复采样"。

> **外部底座可用性（2026-11-14 补充）**：本次新增调研 `trade-learn`（见 survey 第 5.6 节）——它是回测/研究平台，**非因子挖掘器**（因子生成在其侧仍属"待实现"），但内置 `CausalSelector`（PC/FCI 因果筛选，可补我们软去重/抗伪相关的缺口）、Rust 快回测（对比 Backtrader 119x）、MLflow 追踪与 alphalens 多周期 IC 报告，且同样以 KunQuant 为计算后端。结论：**不替代 FactorMiner，可作其下游验证层**。本方案在 Phase 1 接入闸门后，可将挖出的候选因子同步送 trade-learn 做交叉验证，但**不把 trade-learn 的因果筛选当作统计闸门（P0）的替代品**——两者层次不同（它自己声明"不证明样本外有效性"）。

## 0b. 进度跟踪（2026-11-14）

- **Phase 0（统计闸门）**：✅ 已实现并测试。`core/evaluation/experiment_trace.py`（HoldoutLedger + ExperimentTrace）、`core/evaluation/significance.py`（NW-HAC t / BH-FDR / Holm / 随机公式零分布 / DSR / promotion_gates），对应 `tests/test_experiment_trace.py` + `tests/test_significance.py` 共 28 项全绿。验收点（一次性/重叠揭示必抛异常、FDR 受控 ≤5%）已覆盖。
- **Phase 1（闸门接入挖掘闭环）**：✅ 已实现并测试。
  - `core/evaluation/evaluator.py`：新增 `_build_verdict()`，在 `safe_compute` 里把 `hac_t / p_value / dsr / ir / cost_adj_return` 并入每个因子的 metrics（跨截面面板模式；顺序单标的模式返回中性 verdict，需走 temporal assessment，见 survey 5.6 对 trade-learn 的说明）。
  - `core/miner/director.py`：`_save_excellent_factors` 调用 `apply_promotion_gates` 做**批量 BH-FDR**，全绿才标 `INSPECTED`，否则 `DISCOVERED_UNVERIFIED`；每次尝试记入跨轮 `ExperimentTrace`（多重检验分母）；新增 `_enforce_holdout()`（由 `run()` 调用，仅当 `evaluation.gates.strict_holdout` 配置时启用，重复/重叠揭示 fail-loud）。
  - 新增 `tests/test_evaluator_verdict.py` + `tests/test_director_gating.py`，与既有 gate 测试合计 **38 项全绿**。
  - **已知限制**：跨截面 N 较大时 IC 时间序列标准误极小，单因子原始 t 检验会"假性显著"，因此闸门**不依赖原始 p，而靠 BH-FDR + DSR + OOS 一致性 + 成本后为正**共同兜底（与 survey 结论一致）。

- **Phase 1 实测验收（2026-11-14 收尾）**：✅ 完成并跑通。
  - 新增 `user_workspace/experiment_tools/validate_gates.py` 验证脚本，分两部分：
    - **Part 1 旧因子复核**：从 `factor_db/values` 与 `factor_db_oos/values` 读取 6 个老因子样本内/样本外面板的因子值与前瞻收益，用与 `ParallelEvaluator._build_verdict` 完全一致的算法重算 `hac_t / p_value / dsr / cost_adj_return`，并判定 `oos_consistent`，调用真实 `apply_promotion_gates` → **6/6 全部 `DISCOVERED_UNVERIFIED`（被拦截）**。报告落 `user_workspace/reports/gate_validation.md`。
    - **Part 2 真挖掘闭环**：一次真实 cross-asset GP 挖掘（`Directoy` + gates + 一次性 holdout），4 个候选 → 闸门判定 `1 INSPECTED / 3 DISCOVERED_UNVERIFIED`（每次运行因子不同），验证了 Phase 1 接线在真实数据上可用；脚本用 throwaway db_root 且 `finally` 清理，不污染真实 `factor_db`。
  - **Phase 1.5（OOS 强度闸门，关键补强）**：复核中发现原晋升闸门只在样本内判 `cost_adj_positive / dsr` 且仅检查 OOS 方向一致性（不检查 OOS 幅度）——这正是 6 个因子当初"样本内漂亮、OOS 崩塌却仍被 INSPECTED"的根因。新增 `oos_strength` 闸门（`require_oos_strength` + `oos_rankic_min=0.02`），**仅在配置了 `test_period` 时才激活**（否则保持向后兼容、静默）。`significance.py` 的 `promotion_gates` / `apply_promotion_gates` 已支持，对应新增 `tests/test_significance.py` 三项用例。
  - **顺带修复真实 bug**：`get_global_storage()` 此前忽略 `config["storage"]["db_root"]`，导致 Director 始终写入默认 `factor_db`。已让 `get_global_storage(db_root=None)` 接受根目录、`director.__init__` 传入配置根；新增 `tests/test_director_gating.py::test_storage_root_honored_from_config` 回归测试。
  - 当前 gate 相关测试：**42 项全绿**（`test_experiment_trace` + `test_significance` + `test_evaluator_verdict` + `test_director_gating`）。
  - **Phase 1.6（多重检验分母修正，2026-11-14 复核发现）**：`_build_verdict` 里 `deflated_sharpe_ratio(..., n_trials=1)` 把 DSR 退化成普通 Sharpe 检验，`ExperimentTrace.num_trials` 实际从未进入闸门。已修：`ParallelEvaluator` 累计 `total_evaluated`（所有进入评估的候选，含未入短名单的失败者），`director._save_excellent_factors` 用 `trace.num_trials + 本轮评估数` 作分母重算 DSR，并把 `dsr / n_trials` 写回 metrics 供审计。新增回归测试（同一因子 1 次 vs 5000 次试验，DSR 0.94→≈0、闸门翻转）；全量 **152 项通过**。
    - 仍存缺口：BH-FDR 只在**本轮保存的候选**内校正，未覆盖跨轮全部 trial。彻底对齐需让 trace 记录 raw `p_value` 并做累积 FDR（列为后续，不阻塞 OOS 复验）。
  - **Phase 1 验收结论（2026-11-14 更正）**：旧 6 因子确实全部被拦，但**最初那版"6/6"部分是符号 bug 的副作用**——早期 `_build_verdict` 用带符号 IC 算 DSR/成本，负 IC 因子被一律判死。修正定向后：`n_trials=1`（无多重检验）时 5/6 会过线；`n_trials=84`（库规模）或 787（P0 搜索预算）时才 6/6 被 **DSR** 拦住。也就是说，真正拦住它们的是"搜索预算下的 DSR"，不是符号。详见 `user_workspace/reports/gate_validation.md`。
  - 真挖掘闭环跑通且闸门生效；"通过闸门的新因子在严格 OOS 是否站得住"已在 Phase 4 给出正面实证（IC 站住、经济价值没站住）。下一步 Phase 2（静态 AlphaPool）已同步完成。

- **Phase 2（静态 AlphaPool，组合层升级）**：✅ 已实现并测试。
  - 新建 `core/miner/pool.py`：`AlphaPool` 固定容量 K；新因子入池前算与池内各成员的 **mutual-IC**（两因子 IC 时间序列的相关系数，>0.99 拒收去冗余，对齐 AlphaGen）；每次入池后**重优化全部权重**（岭回归，目标为前瞻收益，可选 `weight_fit_shift` 前移防止前视，对齐 AlphaForge `lstsq`）；直接优化**组合 IC** 而非单因子 IC；容量满时贪心替换（替换后能提升组合 IC 才换）。`long_to_wide` 工具 + 复用 `significance.ic_series` / `newey_west_tstat`。
  - `core/miner/paradigms/base.py`：`_finalize` 新增 `finalize_method` 开关（`linear` 默认 / `alphapool`），`alphapool` 路径把挖掘出的候选喂入 AlphaPool，选出非冗余成员后用其拟合权重构建 `LinearCombinedFactor` 前置返回（权重仅在样本内拟合，OOS 指标由 evaluator 真实计算）。新增配置项 `alphapool_capacity / alphapool_mutual_ic_max / alphapool_weight_fit_shift(默认21) / alphapool_reg_lambda / alphapool_top_m`。
  - 新增 `tests/test_alphapool.py`（11 passed）：克隆拒收、独立因子入池、去冗余后组合 IC 不弱于单因子、L1 权重归一、容量替换、无目标守卫、long→wide、pipeline 集成（`finalize_method='alphapool'` 返回 `LinearCombinedFactor`）。
  - 新增验证脚本 `user_workspace/experiment_tools/validate_alphapool.py` + 报告 `user_workspace/reports/alphapool_validation.md`：用真实 84 个因子面板演示 —— **mutual-IC 去冗余实测**（克隆因子 mutual-IC=0.999→拒绝 ✅）；OOS 探针（6 legacy 因子池化后组合 RankIC=+0.0168，优于最优单因子 ≈0，因弱因子噪声被分散）。诚实结论：legacy 因子本身弱（Phase 1 已证），池化价值在于去冗余+权重优化，需配合"在闸门下重挖的 OOS 成立因子"才能体现组合 alpha。
  - 真因子套件：`tests` 相关 6 文件合计 **57 passed**（gate 42 + alphapool 11 + linear 4）。
  - **Phase 2 验收结论达成**：mutual-IC 去冗余生效；组合层在真实数据上可运行，且对弱因子也能通过分散噪声给出非负 OOS 组合 IC。下一步为 Phase 3（加密原生特征 + LLM 闭环）或先用闸门重挖验证组合 alpha。

- **Phase 3A（加密原生特征，2026-11-14）**：✅ 已实现、真实数据验证并测试。
  - 新增 `core/data_feed/crypto_streams.py`：funding_rate / open_interest / basis / long_liquidations / short_liquidations 五条流的采集、落盘与**过去式前向对齐**（`reindex(method="ffill")`，绝不 backfill）。存储布局 `data/<exchange>/<trade_type>/crypto_streams/<symbol>-<tf>-<stream>.feather`，与 OHLCV 文件名解析互不冲突；增量写入按日期合并。
  - **真实数据已落盘**：20 个 1d 永续标的（ADA…XRP）各 funding ≈4240 行（2023-01→2026-11）、basis ≈1413 行、OI 25 行；BTC 1h 另有 funding 94 / basis 736 行。限制：Binance 公共接口 OI 仅保留约 30 天；liquidation 无免费接口，只提供用户自备文件的加载与校验（模板在文件出现后自动生效）。
  - 新增 `core/miner/crypto_features.py`：**29 个参数化加密特征模板**（funding 7 / open_interest 7 / basis 6 / liquidation 5 / interaction 4），全部复用现有 59 算子，**未新增算子**；按数据中实际存在的流过滤、窗口无关模板按 AST 签名去重，`iter_crypto_features` 同时返回模板与窗口便于归因。
  - 接入：`RealDataClient` 新增 `data_feeds.crypto_streams` 配置，加载时把流按 bar 索引前向对齐并入面板（缺文件静默降级为 OHLCV）；`GPFactorMiner` 自动把存在的加密流扩充为 terminals，并按 `gp.crypto_seed_ratio`（默认 0.3）用模板播种初始种群；`LLMFactorMiner` 在 system prompt 注入可用字段与加密语义（**仅数据感知，不是 RD-Agent 闭环**）。
  - 新增 `user_workspace/experiment_tools/materialize_crypto_streams.py`（批量采集工具）与 `validate_crypto_features.py`（验证）+ 报告 `user_workspace/reports/crypto_features_validation.md`。
  - **真实验证结果（20 标的 × 1d × 2023-01→2026-11，次日收益，无成本）**：56 个可用 AST；最强为 `ts_rank(basis, w=48)` mean RankIC **+0.0178**（NW t +2.79）、`sign(funding_rate)` **-0.0153**（t -2.51，资金费率为正→次日回落，符合拥挤度先验）；但 **BH-FDR(q≤0.05) 通过 0/56（最小 q≈0.197）**。结论：管线与对齐已验证，信号只是"第一眼读数"，**不可晋升**；OI 类因样本仅 25 天被排除出排名。
  - **端到端 Director 运行**（`--live`，20 标的、2 轮 × 种群 10、cross-asset 1d、`crypto_streams=[funding_rate,basis,open_interest]`）：返回 4 个候选，闸门判定 **1 INSPECTED / 3 DISCOVERED_UNVERIFIED**；候选含 `abs(mul(basis, close))`（fitness 5.04 / RankIC +0.0445）与 `ts_rsi(cs_rank(funding_rate))`（fitness 2.39 / RankIC -0.0180），证明加密特征真实进入挖掘与评估链路；throwaway db + `finally` 清理，真实 `factor_db` 保持 84 个不变。
  - 测试：`tests/test_crypto_features.py` 20 项；连同 gate/pool/linear 合计 **77 项全绿**。
- **Phase 3B（LLM 闭环）**：⏸ 按用户要求**暂缓，待单独讨论**。当前仅落地"字段感知"（prompt 列出真实可用列与加密语义），未替换 LLM miner 的 hypothesis→code→critique→memory/dedup 流程。

- **Phase 4（真 OOS 复验，2026-11-14）**：✅ 已执行，报告 `user_workspace/reports/p0_holdout/README.md`。
  - 设计：mine 2023-04→2024-12、验证 2025H1、**一次性 strict holdout 2025-07→2026-11**（此前未使用）；GP 60×20 代，实际评估 787 个候选；闸门全开（HAC t≥2 / BH-FDR / DSR≥0.90 / OOS 同向且 ≥0.02 / 成本后为正）。
  - 判定：4 个最终候选 → **2 INSPECTED / 2 DISCOVERED_UNVERIFIED**（`fac_eb287869`、`fac_0d870d2b` 通过）。
  - **IC 层面成立**：3 个可重算候选在 holdout 上全部同向显著（\|t\| 4.17–5.45，无方向翻转）；通过组等权组合 RankIC +0.0577 / t +4.11。对照旧 6 因子（OOS \|IC\| 衰减到 0.028、2/6 翻转）是实质改善。
  - **经济层面不成立**：通过组含成本 **-6.40%**（Sharpe -0.07），零成本仅 +2.22%；被拒的 `fac_0edecf9e` 反而净 **+23.35%**（Sharpe 0.57）。原因：通过组十分位收益不单调、尾部价差仅 ~0.006%/日（年化 ~1.5%），RankIC 显著不等于可交易；且 5 日调仓成本 8.6% 远高于毛收益。
  - **顺带修掉 4 个缺陷**：① DSR 的 `n_trials` 恒为 1（多重检验空转）；② DSR 基准量纲错误（`expected_max_sr` 未按 `1/√n_obs` 缩放）；③ 负 IC 被符号误杀（先定向再算 IR/成本，记录 `orientation`）；④ `evaluation.gates` 阈值未传入闸门 + CLI 无 `--iterations` 时 `None` 崩溃。
  - **结论**：闸门能有效阻止"OOS 方向翻转 / IC 崩塌"的旧失败模式，但**不能排序经济价值**；下一步应把 `cost_adj_return` 改成收益口径（分位多空收益 − 换手成本），并用**新的**未触碰区间预注册验证（2025-07→2026-11 已消耗）。

## 0c. 剩余待办（2026-11-14，按优先级）

> 加密原生（3A）已完成、LLM 闭环（3B）按用户要求暂缓；以下为**不含这两块**的剩余工作。

| 优先级 | 事项 | 现状 / 为什么还缺 | 落点 |
|---|---|---|---|
| **P0** | ~~**真 OOS 闭环复验**~~ ✅ 已完成（见 Phase 4） | 2025-07→2026-11 一次性 holdout：2/4 通过闸门；OOS IC 同向显著（t 4.2–5.5），但含成本组合 -6.4%，被拒因子 +23.4%。结论：闸门能防假阳性，不能排序经济价值。 | `user_workspace/reports/p0_holdout/README.md` |
| **P0.5** | ~~**收益口径的成本闸门**~~ ✅ 已完成（见 `reports/p0_5_return_gate.md`） | `cost_adj_return` 改为分位多空组合的**每期净收益**（gross 1.0、美元中性、18 bp 单边往返成本、调仓频率默认 = `target.horizon_bars`）。用新口径复判 P0 四因子：**0/4 通过**，旧闸门放行的两个（毛收益为正、被换手吃光）被精确拦下。核心结论：**必须在交易周期上挖**，1 日目标 + 20 币的换手成本（年化 6–17%）大于可得边际。正向验证仍需 2026-12 之后的新数据预注册。 | `core/evaluation/portfolio_eval.py` + `evaluator.py` + `significance.py` |
| **P1** | ~~**幅度感知筛选**~~ ✅ 已完成 | DSR 改为折减**策略净 Sharpe**（`sr = cost_adj_return / ls_vol`）而非 IC-IR：IC-IR 无量纲，奖励"稳定但很小"的因子；策略 Sharpe 随真实边际增大。副作用是门槛显著变硬 —— 787 次试验下需要年化 SR ≳ 2.3 才能过 DSR 0.90，这是多重检验的真实代价。 | `core/evaluation/evaluator.py` + `significance.py` |
| **P1** | ~~**单标的时序闸门**~~ ✅ 已完成 | `_build_verdict` 对 Series 走 `_build_temporal_verdict`：滚动分位择时（默认 252 根、只用到当期）→ 净收益序列 → NW t + DSR，另出 `ls_exposure`。BTC 1d 实测 12 个候选全部拿到真实判决（此前恒为 t=0/p=1），最好一个 t=2.31 / p=0.021 / OOS RankIC 0.121，仅差 DSR。 | `core/evaluation/portfolio_eval.py`（`timing_returns`）+ `evaluator.py` |
| **P1** | ~~**全库重审**~~ ✅ 已完成 | `scripts/reaudit_factor_db.py`（默认只读 / `--apply` 备份后写回）重算 84 个存量因子：**0 个通过**，6 个旧 `INSPECTED` 全部降级。分母敏感性：n_trials=84/20/6 均 0 通过，即使关掉多重检验也只有 1/84 —— 主要拦截原因是成本后为负。报告 `reports/factor_db_reaudit.md`。 | `factor_db/metadata_pre_reaudit/` 备份 |
| **P1** | ~~**累积 FDR**~~ ✅ 已完成 | `ParallelEvaluator.run_p_values` 累计所有成功评估候选的原始 p（含未入短名单）；新增 `FdrLedger`（`.fdr_ledger.jsonl`，按 run_id 幂等）；`apply_promotion_gates(family_p_values=...)` 让 BH 作用于历史全部 + 本轮全部假设。冒烟校验：10 个假设时最小 p 0.000532 → p_adj 0.005321（×10）。 | `experiment_trace.py` + `significance.py` + `director.py` |
| **P2** | **相关性软去重** | `DiversityFilter` 只有源码 MD5 硬去重，docstring 承诺的"相关性去冗余 + 与历史库正交性"仍是占位。可复用 `AlphaPool` 的 mutual-IC / `_safe_pearson`，只对通过初筛的候选算，避免全量因子值开销。 | `core/miner/paradigms/base.py` |
| **P2** | **评估超时与资源边界** | `max_workers` 已可配（1–64），但 `executor.map` 无单任务超时/取消：一个卡死的因子会拖住整轮。需 per-task timeout + 异常回收。 | `core/evaluation/evaluator.py` |
| **P3** | **Inspector 组合回测/归因** | 已有快照与滚动 IC，缺分位组合净值、交易成本、行业/市场中性化与 NN 归因。 | `core/inspector/*` + Web |
| **P3** | **三语 i18n 第二批** | Launchpad、Data Downloader 表单与后端错误映射未覆盖。 | `web/` |
| **P3** | **测试环境统一** | 测试实际用系统 Python 3.13（venv 缺 pytest/polars/ccxt）；`test_factor_analytics` 因缺 `httpx2` 无法收集。需固定解释器与 dev 依赖说明。 | `requirements*.txt` / README |

**明确不做（本阶段）**：扩算子（59 个已够，`ts_corr`/`ts_decay_linear` 早已存在）、Ray/Celery 分布式、时变权重（等 P0 成立）、爆仓/多交易所/PIT 资产池、LLM 闭环。

## 1. 分阶段方案

### Phase 0 — 统计闸门（最优先，直接封堵 OOS 全亏根因）

| 编号 | 内容 | 借鉴来源（源码行号） | 落点文件 |
|---|---|---|---|
| 0.1 | **一次性 holdout 揭示账本**：holdout 窗口只能"揭示"一次，重复/重叠揭示直接抛异常 | `crypto-factor-lab/factor_lab/catalog.py:282-299`、`service.py:900-906/955-966` | 新建 `core/evaluation/experiment_trace.py` |
| 0.2 | **ExperimentTrace + num_trials**：跨 run 记录全部评估过的候选（含失败的），作为多重检验分母 | `LLM-Alpha-Mining-Framework`（把全部尝试当分母） | 新建 `core/evaluation/experiment_trace.py` |
| 0.3 | **Newey-West HAC t 统计 + BH-FDR / Holm 校正**：IC 的 t 值做异方差自相关稳健，再按族错误率校正 | `LLM-Alpha-Mining-Framework` 的 `nw_tstat` + `bh_fdr` + `holm` | 同上 + `core/evaluation/significance.py` |
| 0.4 | **随机公式零分布**：从随机语法池采样 N 个合法表达式算 max\|t\|，得到"无信息下也能撞到的显著性"基线 | `LLM-Alpha-Mining-Framework` 的 `random_max_abs_t` | `core/evaluation/significance.py` |
| 0.5 | **DSR（Deflated Sharpe Ratio）+ promotion gates**：因子晋升 INSPECTED 前须过：HAC t 显著、FDR 通过、DSR 显著、OOS 方向一致、成本后为正 | `LLM-Alpha-Mining-Framework` 的 8 道 gates + `crypto-factor-lab` 三条件 | `core/evaluation/significance.py` + `director.py` |

### Phase 1 — 闸门接入挖掘闭环（让挖掘"看不见 OOS"）

- `ParallelEvaluator` 在 `_build_fitness` 之后新增 `_build_verdict()`：返回 `significant_fdr / dsr_pass / oos_consistent / cost_adj_positive` 等布尔 + `t_stat / p_adj`，并写入 metadata.metrics。
- `director._save_excellent_factors` 改为：只有 `verdict` 全绿才允许 `lifecycle_status` 进入 INSPECTED（其余仍落库但标记为 `DISCOVERED_UNVERIFIED`）。
- **留出协议落地**：config 显式 `test_period`；挖掘阶段只喂 mine_period，holdout 仅在"最终一次性验证"时揭示（由 `HoldoutLedger` 强制）。
- 用新留出区间重挖 → 真 OOS 验证，确认闸门有效（目标：通过闸门的因子 OOS 不再全亏）。

### Phase 2 — 组合层升级（静态 AlphaPool）

- 实现 `AlphaPool`（`core/miner/pool.py`）：固定容量 K；新因子入池前算与池内 mutual-IC，>0.99 拒收（去冗余）；每加入一个因子用 IC 目标重优化全部权重（L1/岭）；直接优化组合 IC 而非单因子 IC。
- 对齐 AlphaGen `pool.py` + AlphaForge `lstsq` 权重（`shift=21` 防前视）。
- **时变权重**（用户此前问的"逐时间截面回归"）留作 Phase 2.5：仅在 AlphaPool 在真 OOS 上成立后做增强，避免给噪声加自适应外衣。

### Phase 3 — 加密原生特征 + LLM 闭环

> **状态（2026-11-14）**：加密原生特征（3A）✅ 已完成；LLM 闭环（3B）⏸ 待讨论。

- **加密原生特征**（3A，✅ 已完成）：funding rate、未平仓量 OI、基差 basis、爆仓量；7x24 连续、无涨跌停、资产池随时间变化需处理幸存者偏差。
  - 已落地 `core/data_feed/crypto_streams.py` + `core/miner/crypto_features.py`；复用 59 算子，不新增算子。
  - 24h 成交量可用现有 `volume` 直接表达；多交易所价差需要第二交易所数据源，暂不在本期范围。
  - liquidation 无免费公共接口，已提供加载/校验接口（用户自备 feather）。
  - 资产池幸存者偏差：横截面 IC 只在当期有数据的标的上计算，并以 `min_assets` 过滤过窄截面；更严格的 point-in-time 上市/退市表留作后续。
- **LLM 闭环升级**（3B，⏸ 待讨论）：用 `RD-Agent` 式 hypothesis→code→run→critique→memory/dedup 替换当前弱 LLM miner（同 prompt 重复采样、弱 reflection）。因子库做 embedding 相似度去重 + SOTA 知识注入。本期只做了 prompt 的可用字段/加密语义注入，未改闭环结构。

## 2. 落地文件清单（按优先级）

1. `core/evaluation/experiment_trace.py` —— HoldoutLedger + ExperimentTrace（Phase 0.1/0.2）
2. `core/evaluation/significance.py` —— nw_hac_tstat / bh_fdr / holm / random_formula_null / dsr / promotion_gates（Phase 0.3/0.4/0.5）
3. `core/evaluation/evaluator.py` —— 接入 `_build_verdict`（Phase 1）
4. `core/miner/director.py` —— INSPECTED 晋升闸门（Phase 1）
5. `core/miner/pool.py` —— 静态 AlphaPool（Phase 2）
6. `core/data_feed/crypto_streams.py` + `core/miner/crypto_features.py` —— 加密原生特征（Phase 3A，已完成）
7. `core/miner/paradigms/llm_miner.py` —— RD-Agent 式闭环（Phase 3B，待讨论；当前仅 prompt 字段感知）

## 3. 验收标准

- **Phase 0 验收**：`tests/test_experiment_trace.py` + `tests/test_significance.py` 全绿；HoldoutLedger 对重复/重叠揭示必抛异常；BH/Holm 在仿真多重检验下 FDR 受控（族错误率 ≤ 5%）。
- **Phase 1 验收**：6 个老因子经新闸门判定为"不显著 / 不晋升"，证明闸门确实能拦住 OOS 失效因子；用新留出区间重挖，通过闸门的因子集合在严格 OOS 上不全亏。
- **Phase 2 验收**：AlphaPool 组合因子 OOS Sharpe 优于单因子等权；mutual-IC 去冗余生效。
- **Phase 3 验收**：3A 加密原生特征接入回测 —— ✅ 已达成（真实 20 标的 1d 面板，56 个 AST 全部可评估，流对齐过去式、配置缺省时静默降级；BH-FDR 0/56 是对"不可直接晋升"的诚实结论）；3B LLM 闭环单测覆盖 hypothesis→code→critique 一轮 —— ⏸ 待讨论后实施。

## 4. 明确不做（避免过度工程）

- 不扩算子（已 59 个，第一梯队）。
- 不堆新范式（四范式足够）。
- 不上时变权重（直到 Phase 2 静态池在真 OOS 成立）。
- 不复制 `AlphaForge-Crypto`/`alphaminingv2` 等残缺/README 包装仓的骨架代码。
