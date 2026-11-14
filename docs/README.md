# FactorMiner V4 官方文档体系

欢迎查阅 FactorMiner V4 的官方文档体系！为了配合底层架构向“领域驱动”和“多范式挖掘”的彻底重构，我们将文档划分为以下三个核心区域，帮助您快速找到需要的内容。

## 🏛️ 1. 核心架构红皮书 (Architecture)

这里存放着指导整个 V4 系统设计的顶层文档。如果您是核心开发者或希望深度定制引擎的高级研究员，请务必阅读本区内容。

*   **[FactorMiner V4 架构设计红皮书](architecture/v4_architecture_design.md)**
    *   **核心内容**：详述了系统基于“控制反转 (IoC)”的设计哲学。
    *   **关键模块**：彻底解耦的数据供养层 (`data_feed`)、异构因子存储层 (`storage`)、评价器 (`evaluation`) 以及极速落地编译层 (`execution`)。

## 🗺️ 2. 用户操作与实战指南 (Guides)

本区存放面向一线量化研究员的实操教程，帮助您快速上手，无需关注底层的复杂实现。

*   **[FactorMiner 产品需求文档 (PRD)](FactorMiner_PRD.md)**
    *   项目整体演进路线、功能需求和四大挖掘流派（GP/RL/NN/LLM）的产品愿景。
*   **[Web UI/UX 设计方案与前后端交互规范 (WEB_UI_PRD)](../web/WEB_UI_PRD.md)**
    *   详述了指挥中心、任务发射台、执行控制台以及批量数据下载器沉浸式体验的前端产品定义。
*   **[网络与环境配置指南](guides/vpn_setup.md)**
    *   团队内部基础网络连通与代理设置指南。
*   **[自定义 LLM 与实验留痕指南](guides/custom_llm_experiments.md)**
    *   介绍 `MyCustomLLM` 的用户扩展点、环境变量凭证、独立 Recorder、无头运行和研究报告生成。
*   *(待更新)* **Config-Driven 策略编写指南**
    *   如何通过 `config.json` 零代码启动一个挖掘闭环。
*   *(待更新)* **自定义算子注册教程**
    *   教您如何使用 `@OperatorRegistry.register` 注入您的独门秘籍。

## 🕰️ 3. 历史遗留归档 (Legacy V3)

V3 时代的各种文档（包含不再兼容的 `factorlib` 结构设计、旧版类继承算法规范、旧版 API）已全部移至归档区。
**注意**：这些文档仅作考古与兼容性对照之用，其实际代码在 V4 生产环境中**已完全弃用**。

*   [V3 API 接口废弃列表](legacy_v3/api.md)
*   [V3 旧版因子存储格式说明](legacy_v3/factorlib_structure_guide.md)
*   [V3 用户算法继承范例](legacy_v3/user_algorithm_paradigm.md)
*   [V3 旧版架构变迁文档](legacy_v3/new_architecture_guide.md)

## 🔬 4. 竞品与行业研究 (Research)

面向架构决策的源码级调研，均以「实读源码 + `路径:行号` 证据」为准，README 宣称与实现不符处显式标注。

*   **[业界因子挖掘框架源码层调研报告（18 个仓库）](research/industry_factor_mining_survey.md)**
    *   **核心内容**：算子体系 / 挖掘算法 / 组合层 / **防过拟合统计闸门**的横向矩阵，含 FactorMiner 源码基线与 P0–P2 借鉴清单。
    *   **主结论**：瓶颈是统计显著性与可复现的留出纪律，不是算子或范式数量。
*   **[AlphaMaster 竞品源码层研究报告（第 19 个仓库 / 增量）](research/alphamaster_competitor_review.md)**
    *   **核心内容**：MT5 单品种时序因子 + Web 训练控制台这一新定位；62 算子 / 65 特征 / 动作掩码 / REINFORCE；一条「验证集反复复用 → 无试验次数折减 → 因子退化为 beta」的实证病因链。
    *   **作用**：从反例侧加固上篇的 P0 排序，并补入 L5 产品化层与若干 P1 落地项。
*   **[框架横向评估：成熟度 × 加密货币适配度（20+1 个框架）](research/framework_maturity_crypto_fit.md)**
    *   **核心内容**：21 个框架的工程指标脚本化盘点、双维度评分与象限定位；加密专用框架的可运行性逐仓核查；两处对既有结论的口径修正。
    *   **主结论**：最成熟的是股票框架（qlib / RD-Agent），最适合加密且能直接跑的是 `crypto-factor-lab`，两者不重合；不存在可直接搬用的成熟加密框架，FactorMiner 应走「主干自有 + 机制按点抄」，且先修 7×24 年化口径。
    *   **实施状态（2026-11-14）**：报告顶部两份补记分别记录「实施前复核收窄」与「P0-a / P0-b 已落地」；`core/evaluation/annualisation.py`、`core/evaluation/holdout_guard.py` 为对应产出，全量测试 326 passed / 1 skipped。
*   **[平台成熟度差距评估：做一个成熟的因子挖掘平台还差什么](research/platform_maturity_gap.md)**
    *   **核心内容**：把「成熟」拆成能力覆盖 / 纪律可信 / 结果产出 / 运营可复现四维评分；按「现在补 / 等数据 / 有正向因子后补」分档列出差距，每条附源码证据与验收标准；含三处对既有报告的口径收窄与 S1–S5 路线图。
    *   **主结论**：**不缺功能，缺的是「已实现的能力没接进主流程」和「一个正向结果」**。因子库 84/84 全为 `DISCOVERED_UNVERIFIED`、唯一严格 OOS 含成本 -6.40%；先补接线（随机零分布、账本 API 化、`num_trials` 跨机器）、验证纪律（gap/embargo + 逐折重拟合）、可复现四件套，再等 2026-12 后的新窗口拿正向证据，最后才谈实盘 / 分布式 / 多用户。
    *   **实施状态（2026-11-14 18:20–18:55）**：报告顶部「✅ 实施完成补记」记录 S1→S3 的接线、时间隔离与数据口径落地，以及一键端到端流水线。四维评分更新为能力覆盖 8 / 纪律可信 8 / **结果产出仍为 1** / 运营可复现 6；全量测试 383 passed / 1 skipped；正式 run `user_workspace/runs/run_20261114T104814Z_b9eb49/` 8 阶段全通过且 `reproducibility=True`。**正向 OOS 证据仍未获得**（验证段含成本 -4.43%、0 promoted）。另见 §6：2026-11-14 20:00 后追加了闸门阳性对照（证明闸门功效不足也是 0 promoted 的原因之一）、universe holdout 与滚动预注册（首个干净窗口 ETA 提前到 2026-12-13）；全量测试 431 passed / 1 skipped。

## 🧪 5. 一键端到端实验流水线 (Experiment)

面向「跑通一个完整、可审计、可复现的因子挖掘流程」的正式入口。`core/experiment/` 把此前散落在脚本与库函数里的能力编排成八阶段流水线。

### 快速开始

```bash
python -m core.cli experiment \
  --config user_workspace/configs/configExperiment.json \
  --miner GP --seed 20261114
```

*   `--config`：实验配置（窗口预注册、成本、闸门、walk-forward 参数都在此固定）。
*   `--miner` / `--iterations`：覆盖配置中的范式与迭代数。
*   `--seed`：全局随机种子，写入 manifest；同种子两次运行回测收益只差浮点末位。
*   `--resume`：按 **stage 级**跳过已完成阶段（不恢复 epoch 级 GP 种群，见下方限制）。
*   `--run-id` / `--user-dir` / `--output-root`：指定 run id、用户算子目录与归档根。

### 流程阶段

`STAGES` 定义 8 个执行阶段（`data_checked / mining / gated / validated / combined / oos_prepared / backtested / completed`），归档时另有 `created` 预注册节点，故 `status.json` 共记录 9 个状态：

`预注册(created) → 数据质量 → 挖掘 → 晋升闸门 → walk-forward → 组合检查 → 验证段 OOS 重算 → 回测 → 报告/归档`

| 环节 | 落地要点 |
|---|---|
| 预注册 | `manifest.json` 固定 `config_hash` / `git_sha` / `operator_set_hash` / `data_snapshot_hash` / seed / 窗口；`holdout_guard` 默认 strict；成本恒等式 `gate_cost_rate = 2·(commission + slippage)` 校验 |
| 数据质量 | `panel_validator` 校验重复时间戳、缺口、覆盖率、极端收益；本次正式 run 641 bars / 0 warning |
| 挖掘与闸门 | 候选隔离写入 `<run_dir>/factor_db`；随机目标置换零分布接入 `apply_promotion_gates`，`empirical_p` / `gate_pass` / `gate_checks` 写回因子档案；累计 `num_trials` 由可提交的 `.hypothesis_budget.json` 跨机器同步 |
| 时间隔离 | `walk_forward.py`：expanding-window 折、`purge=1` + `embargo=5`（gap 6 bars）、每折独立标准化与岭回归重拟合，`weights_differ_across_folds=True` |
| 验证段 OOS | 因子值重算到 `<run_dir>/factor_db_oos`；回测用 `orientation_db_root` 从样本内库学方向，因子值只取 OOS 重算库，杜绝前视 |
| 数据与成本 | 真实 8h `funding_rate` 流按 bar 频率 `sum(min_count=1)` 聚合，缺失 bar 回落配置常数；summary 记录 `funding_source` |
| 归档 | `user_workspace/runs/<run_id>/`：`config.json` / `manifest.json` / `manifest_final.json` / `state.json` / `metrics.json` / `walk_forward.json` / `report.md` / `status.json`；`index.jsonl` 追加索引 |

### 正式实测（2026-11-14）

`run_20261114T104814Z_b9eb49`：8 stages completed，4 候选 / 4 落库 / **0 promoted**，`empirical_p` 4/4 已计算（3 个被 `empirical_null_pass` 拦下），验证段读取计数 87，5 折逐折重拟合，4 份 OOS 快照，真实 funding 命中 20 个标的，`reproducibility=True`、mismatches `{}`，验证段回测 **-4.43% / Sharpe -0.37**。

> **诚实边界**：绿色流水线只证明「过程可复现、可审计」，**不等于挖到了赚钱的因子**。一次性 strict holdout 未被触碰；正向证据由 §6 的**滚动预注册窗口**提供（锚点 2026-11-14，30 根/窗、5 根 embargo，首个窗口 ETA 2026-12-13），也可用 §6 的 universe holdout 走标的维度。`--resume` 仅为阶段级，不恢复 epoch 级 GP population；LLM 范式因外部 API 仍非确定。

**相关模块**：`core/experiment/{manifest,registry,budget,costs,walk_forward,pipeline}.py`、`core/commands/run_experiment.py`、`core/evaluation/holdout_guard.py`、`core/backtest/backtest_engine.py`。

## 🔍 6. 数据预算与闸门校准 (P7)

回答「还没到 12 月，怎么给数据，是不是流程有问题」。核查结论：时间约束是真的（`.holdout_ledger.json` 里 2025-07-01→2026-11-13 已一次性揭示，20 币历史全部被读过，复用即为偷看），但原流程确有**三处缺陷**：预算一次性烧光且无分层留出、预注册是静态 90 根批处理、把「方法正确性」与「经济 alpha」捆成同一个验收条件。

### ① 闸门阳性对照：`core/experiment/gate_calibration.py`

此前只有「0/84 全拦住」的阴性记录，从没验证过闸门在真信号存在时能否放行。该模块用已知真实 IC 的半合成 DGP 生成面板，**所有指标经生产函数计算**后交给真实 `apply_promotion_gates`：

```bash
python -m core.experiment.gate_calibration \
  --out user_workspace/reports/gate_calibration.md
```

实测（`user_workspace/reports/gate_calibration.md`）：虚警率 **0.0%**；MDE 随 `n_trials` 从 1→0.06 升到 787→0.15；**正式 run 的 `n_trials=182` 需要真实 IC ≥ 0.10**，远高于加密日频常见的 0.02–0.05，瓶颈是 `dsr_pass`。所以「0 promoted」至少有一部分是**功效不足**。报告明确声明：证明的是统计校准，不是挖到 alpha，也不构成正向 OOS 证据。

### ② universe holdout：`core/experiment/universe.py`

标的维度的第二根干净数据轴。挖掘只用训练池，OOS 重算/回测只用预留池，方向仍从样本内库学习；预留币与原池不重叠时 `clean=True`，留一法切分则 `clean=False` 并在报告中强制标注「仅验机制」。一次性账本 `user_workspace/.universe_ledger.json` 防止反复偷看同一批预留标的。

```bash
# 机制验证（14 训练 / 6 预留，clean=False）
python -m core.cli experiment --config user_workspace/configs/configUniverseHoldout.json --miner GP

# 真正干净的留出：先下载不重叠的新币，再改配置 universe_holdout + strict
python scripts/download_universe_holdout.py --symbols "SUI/USDT:USDT,..." --dry-run
```

正式机制验证 run `run_20261114T125520Z_2722fb`：6 币 OOS 快照、真实 funding 命中 6 标的、75 笔交易、-20.53%。同时修掉一个静默错误：回测默认 5 多/5 空要求 10 资产，6 币预留池下会产出「0 交易、0% 收益却报成功」；`adapt_book_to_universe()` 现按实际资产数自适应，20 币行为不变。

### ③ 滚动预注册：`core/experiment/windows.py`

```bash
python -m core.experiment.windows          # 当前计划（只读）
python -m core.experiment.windows --json  # 完整窗口列表
```

`user_workspace/experiment_window.json` 已改为 `rolling=true, start=auto, window_bars=30, embargo_bars=5`：锚点从 holdout 账本最后揭示日次日自动推导（**当前 2026-11-14**），按「所有标的都有该 bar」的真实交集分块而非日历天数，窗口边界固定不随滚动揭示漂移，每窗只揭示一次。`GET /api/data/freshness` 与 `GET /api/gates/holdout` 均返回滚动计划。

**效果**：原先 11-14→11-30 的新 bar 会被硬编码的 12-01 起点丢弃；现在纳入第一个窗口，首个可用窗口 ETA 从 **2027-02-28 提前到 2026-12-13**，且从第 30 根起就能开始累积证据，不必一次等 90 根。规划是只读的，真正揭示必须走 `holdout_guard(purpose="final")` 写账本。

> **仍未解决**：正向 OOS 证据（R=1）、PIT 资产池、OI 仅 25 行、liquidation 数据源、NN 独立特征路径、epoch 级断点续训、Hypothesis 属性测试、实盘/风控、认证/多用户、分布式评估、因子自动退役。

### 系统级代码 Review

*   **[FactorMiner 系统级代码 Review](reviews/system_code_review.md)**
    *   **范围**：架构层 / 流程与统计纪律 / 代码缺陷 / 测试与运维文档一致性，四路只读侦察 + 逐条手工复现（含 `pytest` 基线 431 passed / 1 skipped）。
    *   **核心结论**：**统计纪律主干正确**（OOS 未反灌选优、闸门链路完整、成本恒等式存在）；高危项集中在两个 NaN 边角——`hac_t=NaN` 候选在 empirical null 闸门**假通过**（🔴）、walk-forward 短腿选到全 NaN 列并静默丢 bar（🟠），以及 3 个一次性账本 `??` 未提交导致跨机器守卫失效（🔴）、`docs/README.md` 7 处死链、`aiohttp` 缺失依赖声明、API 无鉴权。所有发现附 `路径:行号`，并给出 P0–P3 修复路线图。
    *   **✅ 修复已落地**：P0–P3 全部执行（非仅诊断），详见报告 §8。修复后基线 **447 passed / 1 skipped / 56 subtests**，含 16 个新增回归测试（empirical_p NaN 假通过、walk-forward 短腿、成本单一真相源、API holdout 守卫、API 鉴权/CORS、账本测试隔离）。三个账本已 `git add`，`runs/` 已 gitignore。

---
*Generated by Antigravity during the V4 restructuring phase.*
