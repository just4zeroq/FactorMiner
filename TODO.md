# FactorMiner V4 待办事项

> 🚀 **项目状态**: V4 架构底层引擎已搭建完毕，成功跑通 GP、RL、NN、LLM 四大异构挖掘范式。评估引擎 (`ParallelEvaluator` + `custom_fitness` 钩子) 已通过沙盒与真实数据回测的闭环验证。统计闸门（NW-HAC t / BH-FDR / DSR / 一次性 holdout）、静态 AlphaPool 与加密原生特征（3A）均已落地；当前重心是**用未使用过的区间做严格 OOS 复验**，并补齐单标的时序闸门。

## 🔄 当前核心任务 (进行中)

### 1. **Web UI 界面联调与可视化配置** (完成)
**优先级**: 最高  
**说明**: 将 V4 强大的后台引擎接入 React 前端，抛弃纯 CLI 运行。
- [x] **可视化 Config 生成**: 在前端增加配置面板，支持用户点选 Universe、Mine Period、Test Period 以及所需的行情特征列 (Features)。(通过直接解析工作区 Config JSON 实现)
- [x] **范式动态切换**: 允许用户在界面上选择 `MyCustomGP` 或 `MyCustomRL`，并自动呈现对应的参数表单。
- [x] **启停控制与后台任务池**: 将前端表单序列化下发至后台 `/api/launch`，后端以异步线程启动 `FactorMinerDirector`，并通过 WebSocket 将挖掘进度实时广播至前端 Drawer。
- [x] **Research Dashboard 重构**: 首页改为由 `/api/dashboard` 驱动的研究指挥台，聚合真实任务状态、因子归档/审查覆盖、最高 fitness 候选、Miner 构成和近期执行记录，并提供至 Launchpad、Inspector 的行动跳转。
- [x] **三语界面框架（第一批）**: 提供浏览器语言识别和本地偏好持久化，支持中文、English、Deutsch；已覆盖全局导航、Research Dashboard 与 Factor Inspector，研究工件保持原始文本。
- [x] **README WebUI、概览与扩展教程**: 以当前实现重写项目简介、核心能力和目录地图；补充 Research Dashboard、Factor Inspector 的实机截图，并记录四大范式的结果契约、自定义算子接入方式和 Fitness Hook 配置范式。
- [ ] **三语界面扩展（第二批）**: 将 Launchpad、Data Downloader 的配置表单、实时日志周边提示和后端错误消息映射补齐，继续保持 API 枚举及因子内容不随语言变化。

### 2. **算子与计算引擎扩展** (进行中)
**优先级**: 中
**说明**: 扩展现有的单品种串行计算能力。
- [x] **Cross-Asset 截面计算**: 当 `mining_mode` 设置为 `cross_asset` 时，重构底层数据对其逻辑，支持横截面算子 (如 `cs_rank`, `cs_zscore`) 的计算。
- [x] **用户算子端到端接入**: 动态加载的 OperatorRegistry 已接入 GP/RL 的搜索空间、按 arity 生成 AST、统一求值和运行期异常报告；MyCustomGP 默认使用四则运算、ts_mean/ts_std 和衰减、Z-score、变化、排名、波动率等用户时序算子。
- [x] **扩展启动校验与四范式 CLI smoke tests**: CLI/Launchpad 会在读取数据前校验 Miner、算子、Fitness Hook 与用户模块加载错误；MyCustomGP、MyCustomRL、MyCustomLLM、NN 均有真实 Feather 数据的一轮 CLI smoke test。
- [x] **MyCustomGP 真实数据回归与指标契约核验**: 重启 FastAPI 后以当前 `config.json` 完整运行 BTC、SUI 永续挖掘，确认用户算子进入 AST、候选评分非零，并将 `IC`、`RankIC`、`Turnover`、`fitness_score` 与 Phase II 值/未来收益快照一同落盘。Tracker 重启后内存任务会清空，因子档案仍以 `factor_db` 为准。
- [x] **更多原生算子支持**: `ts_corr`、`ts_cov`、`ts_decay_linear`、`ts_min/ts_max` 等已在 `OperatorRegistry` 中预置并参与 arity 生成与求值（早期遗留项，实际已完成）。

### 2b. **加密原生数据与特征** (3A 完成)
**优先级**: 高
**说明**: 永续合约的 funding / OI / basis / 爆仓流是股票没有的 alpha 来源；已在 59 个通用算子上以"新数据流 + 特征模板"方式接入，不新增算子。
- [x] **加密流采集与对齐**: `core/data_feed/crypto_streams.py` 支持 funding_rate / open_interest / basis 采集、feather 落盘、增量合并与**过去式前向对齐**（绝不 backfill）；liquidation 提供用户自备文件接口。
- [x] **数据源接入**: `RealDataClient` 新增 `data_feeds.crypto_streams`，把流并入 cross-asset / sequential 面板，缺文件静默降级。
- [x] **特征模板库**: `core/miner/crypto_features.py` 提供 29 个参数化模板（funding / OI / basis / liquidation / interaction），按可用流过滤、窗口无关模板去重。
- [x] **挖矿接入**: GP 自动扩充 terminals + 初始种群加密播种（`gp.crypto_seed_ratio`）；LLM prompt 注入可用字段与加密语义（仅数据感知）。
- [x] **真实数据验证**: 20 个 1d 标的、2023-01→2026-11；56 个 AST，最强 `ts_rank(basis,48)` RankIC +0.0178（NW t 2.79），但 BH-FDR 0/56 → 不可晋升（报告 `user_workspace/reports/crypto_features_validation.md`）。
- [ ] **爆仓数据源**: 接入付费/交易所数据门户的 liquidation 历史（当前仅支持自备 feather）。
- [ ] **多交易所价差**: 需要第二交易所 OHLCV 与资金费，作为跨所基差特征。
- [ ] **Point-in-time 资产池**: 上市/退市时间表，彻底消除幸存者偏差（当前以当期非空 + `min_assets` 近似）。

### 3. **去重与评估执行能力补齐** (进行中)
**优先级**: 中
**现状**: `DiversityFilter` 已实现基于候选源代码 MD5 的全局硬去重；相关性软去重目前仍为占位逻辑。`ParallelEvaluator` 的线程数已配置化（`evaluation.max_workers`，1–64），但尚无单任务超时与异常回收。
- [ ] **相关性软去重**: 计算候选因子面值的相关性，按阈值剔除高相关候选，并补充与历史因子库的正交性检查（可复用 `AlphaPool` 的 mutual-IC 实现，仅对通过初筛的候选计算以控成本）。
- [x] **可配置并行评估**: `max_workers` 已从 `evaluation.max_workers` 读取并校验（1–64），默认 8。
- [ ] **评估超时与资源边界**: 为单候选评估加 timeout 与取消/回收，避免个别卡死因子拖住整轮。
- [ ] **分布式评估后端**: 评估并按需接入 Ray 或 Celery —— 当前明确暂缓（单机 20 标的规模不构成瓶颈，先完成统计闭环再谈横向扩展）。

### 3b. **统计验证闭环** (P0/P1)
**优先级**: 最高
**说明**: Phase 0/1/1.5/2/3A 已落地；剩余是把"闸门有效"从"拦住了坏因子"推进到"好因子在严格 OOS 上站得住"。
- [x] **真 OOS 闭环复验**: 2025-07→2026-11 一次性 holdout（mine 2023-04→2024-12、验证 2025H1、787 次评估）。4 个候选 → 2 INSPECTED；OOS IC 同向显著（\|t\| 4.17–5.45，无翻转），但含成本组合 **-6.40%**、零成本 +2.22%；被拒因子反而净 +23.35%。结论：闸门能防"OOS 方向翻转/IC 崩塌"，不能排序经济价值。报告 `user_workspace/reports/p0_holdout/README.md`。
- [x] **收益口径成本闸门（P0.5）**: `cost_adj_return` 改为分位多空组合的每期净收益（gross 1.0、美元中性、18 bp 往返成本、调仓频率默认 = `target.horizon_bars`），新增 `ls_return/ls_cost/ls_turnover/ls_sharpe/ls_t` 落库与可选 `ls_t_min` 闸门。复判 P0 四因子 **0/4 通过**，旧闸门放行的两个（毛收益为正、被换手吃光）被精确拦下。核心结论：必须在交易周期上挖（1 日目标 + 20 币的年化换手成本 6–17% 大于可得边际）。报告 `user_workspace/reports/p0_5_return_gate.md`。
- [ ] **P0.5 正向验证**: 用 2026-12 之后的新区间做预注册实验，验证新闸门放行的因子在 OOS 确实为正（2025-07→2026-11 已消耗，不可复用）。
- [x] **幅度感知筛选**: DSR 改为折减**策略净 Sharpe**（`sr = cost_adj_return / ls_vol`）而非 IC-IR。代价是门槛变硬：787 次试验 × 610 个观测下，基准日 Sharpe 0.148（折年化 2.35），即搜索预算本身决定了要多强的策略才算显著。报告 `user_workspace/reports/p1_gate_upgrades.md`。
- [x] **单标的时序闸门**: Series 走 `_build_temporal_verdict` —— 滚动分位择时（252 根、只用当期信息）→ 净收益序列 → NW-HAC t + DSR，附 `ls_exposure`。BTC 1d 实测：12 个候选首次拿到真实判决，最好 t=2.31 / p=0.021 / OOS RankIC 0.121（此前恒为 t=0/p=1）。
- [x] **全库重审**: `scripts/reaudit_factor_db.py` 重算 84 个存量因子 → **0 通过**，6 个旧 `INSPECTED` 全部降级为 `DISCOVERED_UNVERIFIED`（备份 `factor_db/metadata_pre_reaudit/`）。分母敏感性 84/20/6 均 0 通过、n_trials=1 仅 1/84 —— 主因是成本后为负。报告 `user_workspace/reports/factor_db_reaudit.md`。
- [x] **累积 FDR**: `ParallelEvaluator.run_p_values` 累计所有成功评估候选的原始 p；新增 `FdrLedger`（`.fdr_ledger.jsonl`，按 run_id 幂等）；BH 作用于历史 + 本轮全部假设（冒烟校验 10 个假设时 p 0.000532 → p_adj 0.005321）。
- [x] **DSR 真实多重检验分母**: 已修 `n_trials=1` 的空转问题 —— 评估器累计 `total_evaluated`，导演以 `trace.num_trials + 本轮评估数` 重算 DSR 并写回 `dsr/n_trials`（新增回归测试）。

### 4. **Factor Inspector 审查台** (Phase II 完成)
**优先级**: 高
**说明**: Inspector 已从静态演示升级为基于 `factor_db` 的真实因子目录、异构逻辑白盒详情、数据快照 Tearsheet 与批量审查工作流。
- [x] **持久化因子目录**: 提供 FactorStorage 列表/生命周期更新能力与 `/api/factors`、`/api/factors/{id}` API；不依赖易失的任务内存。
- [x] **异构逻辑详情**: 按 GP AST、LLM 源码、RL 动作轨迹与 NN 模型版本/通道呈现真实存储产物；Launchpad 结果可直接跳转审查页。
- [x] **审查状态流转**: 支持 `DISCOVERED`、`INSPECTED`、`PAPER_TRADING`、`LIVE`、`RETIRED` 的显式更新和持久化。
- [x] **审查快照与 Tearsheet**: 因子入库时保存对齐的因子值、未来收益和数据血缘；据此提供真实滚动 IC、分位收益、换手率和数据范围。无快照档案明确要求重新挖掘，严禁模拟图表。
- [x] **多因子比较与批量审查**: 支持选择 2–5 个有快照的因子比较滚动 IC，并可批量更新生命周期状态。
- [ ] **高级归因与投资组合回测**: 在已保存快照之上增加 NN 特征归因、行业/市场中性化、分位组合净值与交易成本模型。

---

## ✅ 已完成核心里程碑 (V4 架构)

- [x] **V4 架构底座奠基**: 抽象出统一的 `BaseFactorMiner` 和 `FactorMinerDirector` 流程，支持异构流派。
- [x] **统一评判标准**: 抽象出 `FactorExpressionAST` 语法树节点执行规范，允许因子转换为可执行的 Pandas 计算图。
- [x] **真实数据切片器**: 完成 `RealDataClient` 的开发，支持读取高频 `.feather` 并在时间片上（`mine_period`, `test_period`）无缝拼接。
- [x] **工业级 Config-Driven 模式**: 告别硬编码，全盘迁移至类似 Freqtrade 的 `config.json` 及 `factorminer` 驱动。
- [x] **GP 范式落地**: 编写 `MyCustomGPMiner`，完成达尔文式的变异、交叉及精英保留闭环验证。
- [x] **RL 范式落地**: 编写 `MyCustomRLMiner`，彻底解耦 PyTorch 依赖，通过 Policy Gradient 权重字典完成概率采样与反馈闭环验证。
- [x] **LLM 范式落地**: 编写 `MyCustomLLMMiner`，实现大语言模型的自然反思机制 (Reflection) 及 API 容灾降级容错，直接生成 Python 源代码并通过安全沙盒评估。
- [x] **NN 范式落地**: 编写可替换的 `MyCustomNNMiner` 参考实现，使用纯 NumPy MLP、反向传播、梯度裁剪、Early Stopping 与通道多样性约束，验证 V4 引擎对端到端神经因子的原生兼容性。
- [x] **NN 训练产物闭环**: 支持单品种与跨资产输入，在 `mine_period` 训练、按 `test_period` 样本外指标筛选；冻结跨轮最佳通道，并以包含权重、Scaler、特征 schema 的 `.npz` 模型包落盘，同时兼容旧 `.pt` 档案和现有 WebUI。
- [x] **Temporal NN 教学模板**: 新增独立 `MyTemporalNN`，演示无泄漏多周期 OHLCV 特征、可配置远期收益标签、MSE + IC 联合目标、有符号 RankIC 筛选和自定义模型格式重载；提供约 10 分钟的单品种学习配置。
- [x] **评估与沙盒闭环验证**: 成功剥离出 `user_workspace/custom_fitness/` 并在真实执行流中验证了 `EvaluatorRegistry` 钩子注入机制（如 `my_bear_market_hunter`）；跑通了防御性沙盒 `RestrictedSandbox` 及针对 NN 的张量短路评估机制。
- [x] **因子落盘与持久化存储**: 补全了 `LocalFactorStorage`，实现了每个 Epoch 结束时将优质因子、元数据及评价指标落盘至 `factor_db/` 数据库。
- [x] **多品种序列及横截面挖掘引擎**: 实现了 `sequential_single` 以及基于矩阵计算的 `cross_asset` 并行截面 IC 计算，并在 CLI 终端完美输出跨资产综合战报。
- [x] **数据自动拉取与补全**: 强化 `RealDataClient`，支持当本地缺失配置的行情 `.feather` 时，自动调用 `DataDownloader` 尝试后台无感下载并对齐目标数据段。
- [x] **健壮的 IC 评估机制 (Anti-Bloat)**: 针对 GP 范式由于代码膨胀 (Bloat) 生成的常数无效因子，在 Pandas 矩阵 `corrwith` 时引入告警拦截与降级，保证挖掘控制台输出清爽。
- [x] **全局逻辑硬去重 (Global Hard Deduplication)**: 打通 `FactorStorage` 与 `DiversityFilter` 的通讯，在引擎启动时自动将全库因子历史 Hash 注入拦截网，阻止重复因子的无效计算与污染。
- [x] **高级批量数据下载器集成 (Advanced Batch Downloader)**: 将 CCXT 动态元数据获取、网络降级熔断机制、以及基于笛卡尔积排列组合的批量下载和覆盖率分析无缝集成到统一后台。
- [x] **Web UI 下载控制台集成**: 在前端实现下载日志的实时 Console 打印，提供对运行中下载任务的透明度和进度监控。
- [x] **市场元数据优化与联动**: 在后端实现基于市值/流动性（Quote Volume）的智能排序，并优化前端 `Exchange -> TradeType -> Symbol` 的级联过滤逻辑，防止跨市场误选。
- [x] **底层存储命名规范闭环**: `Batch Downloader`、`Main API`、`Real Client` 统一使用 `{safe_symbol}-{timeframe}-{trade_type}.feather`；期货配置必须使用 CCXT 原始标的（如 `1000CAT/USDT:USDT`），旧命名文件不再参与读取。
---

**最后更新**: 2026年11月14日
**维护者**: @CharlesJ-ABu  

> 💡 **提示**: 此文件记录了 V4 重构后的核心骨架与待落地的待办事项。
