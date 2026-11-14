# FactorMiner 平台成熟度差距评估：做一个成熟的因子挖掘平台，还差什么

> **评估日期**：2026-11-14（当日第四轮；基线 = P0-a / P0-b 落地后、全量测试 326 passed / 1 skipped 的当前工作区）
> **分析方式**：脚本化盘点 + 源码精确检索，所有结论附 `路径:行号`；「未找到」均为全仓（排除 `venv/`、`.git/`、`node_modules/`）检索零命中后的如实结论。
> **本报告回答**：不是「还缺哪些功能」，而是「距离一个能持续产出可验证 alpha 的平台，差在哪一层」。
> **与前序报告的关系**：延续 `industry_factor_mining_survey.md`（18 仓库）、`alphamaster_competitor_review.md`（第 19 个）、`framework_maturity_crypto_fit.md`（21 框架双维度）的口径；本轮**收窄三处既有结论**，见 §4。

> **✅ 实施完成补记（2026-11-14 18:20–18:55，用户「先把整体计划列出来，然后逐一实现」）**：本轮按 S1→S3 把差距清单里的接线、时间隔离、数据口径逐项落地，并新增一键端到端流水线。**全量测试 383 passed / 1 skipped / 56 subtests**（`384 tests collected`；基线 326/1，新增 57 个测试），无退化。唯一 skip 是 `tests/test_factor_analytics.py:70` 缺 `httpx` 的既有环境问题。注：补记初稿记为 382，是写入前那次运行的结果；随后 `tests/test_p5_pipeline.py` 于 18:54 补入最后一个回归锁用例，收尾复跑得 383。
>
> **新增模块**：`core/experiment/`（`manifest.py` 可复现身份、`registry.py` run 归档、`budget.py` 跨机器试验分母、`costs.py` 成本恒等式、`walk_forward.py` purge/embargo + 逐折重拟合、`pipeline.py` 八阶段编排）、`core/commands/run_experiment.py`（CLI `experiment`）、`user_workspace/configs/configExperiment.json`（正式严谨配置）、5 个测试文件 + `tests/conftest.py`（预算快照隔离）。
>
> **四维评分更新**：能力覆盖 **8→8**（未扩功能）、纪律可信 **7→8**（零分布真正接入晋升判定、账本 API 化、`num_trials` 跨机器、逐折重拟合）、运营可复现 **4→6**（run manifest + registry + 算子集版本戳 + 全局种子 + 阶段级 resume）、**结果产出仍为 1**（84/84 未验证、正向 OOS 仍待 2026-12 后的新数据）。R 是乘数，这一项没动。
>
> **本轮推翻了本文三处原始判断**：① §0.3 / §3.1-A1 的「随机零分布未接入」→ 已接入 `apply_promotion_gates` 并把 `empirical_p` / `gate_checks` 写回因子档案；② §3.1-A4 的「全仓 purge/embargo 零命中」→ `walk_forward.py` 已实现，实测 5 折 gap 均为 6 bars、每折权重独立；③ §3.3-C3 的「funding 常数网格」→ `build_funding(..., funding_series=...)` 支持真实 8h 流，实测 20 币全部命中。
>
> **端到端实测**（`user_workspace/runs/run_20261114T104814Z_b9eb49/`）：8 个 stage 全通过；数据质量 0 warning；4 个候选全部计算 `empirical_p`，3 个被 `empirical_null_pass` 拦下；walk-forward 5 折逐折重拟合（`weights_differ_across_folds=True`）；验证段重算 4 份 OOS 快照、方向从样本内库学习；回测使用真实 funding（20 assets），scope 2025-01-01..2025-06-30；`manifest_final.json` 的 `reproducibility=True`、无 mismatch；两次相同种子运行的回测收益只差浮点末位。
>
> **一个被校验抓出来的真实缺陷**：首版 `reproducible=False`，原因是 `load_user_modules()` 在挖掘阶段才注册用户算子，而 manifest 在预注册阶段就指纹化了算子集。已把用户模块加载提前到 manifest 生成之前——这恰好证明算子集版本戳不是装饰。
>
> **仍未解决**（对应 S4/S5，不在本轮范围）：正向 OOS 证据、PIT 资产池、OI 只有 25 行、liquidation 数据源、NN 独立特征路径接入、真实 funding 与 gate 成本的进一步细化、epoch 级断点续训（`--resume` 仅阶段级）、Hypothesis 属性测试、实盘/下单/风控、认证与多用户、分布式评估、因子自动退役。
>
> **用法**：`python -m core.cli experiment --config user_workspace/configs/configExperiment.json --miner GP [--seed N] [--resume]`。产物在 `user_workspace/runs/<run_id>/`（manifest / metrics / walk_forward / report.md / status）。

> **✅ P7 补记（2026-11-14 20:00–21:00，用户「都没到 12 月，怎么给数据，是不是流程有问题」）**：本轮直接质疑「把最后一环挂在一份物理上还不存在的数据上」。核查后确认：**时间约束是真的**（`user_workspace/.holdout_ledger.json:2-8` 只有一条 `holdout_2025H2_2026`，2025-07-01→2026-11-13 已一次性揭示；`data/` 81 个 feather，最新 bar 2026-11-13，20 币全部参与过挖掘），但「等 12 月」这句话掩盖了三处真实流程缺陷，本轮按顺序全部修复。
>
> **① 把「方法正确性」和「经济 alpha」捆成一个验收条件** → 拆开。新增 `core/experiment/gate_calibration.py`，用已知真实 IC 的半合成 DGP、全部指标走生产函数、再交给真实 `apply_promotion_gates`。实测 `user_workspace/reports/gate_calibration.md`：虚警率 **0.0%**，`n_trials=182` 下 **MDE = 0.10**，远高于加密日频现实 IC 0.02–0.05，瓶颈 `dsr_pass`。**这是本文 §0.1 / §3 漏掉的一条**：项目从没有过一次阳性对照，因此「0 promoted」无法区分「因子都不行」与「闸门硬到连真信号都拦掉」。现在有了答案——两者都有，且后者可量化。这不改变 R=1（仍无正向 OOS），但把 R 里「方法没验过」的那部分清掉了。
>
> **② 数据预算没有第二根轴** → 新增 `core/experiment/universe.py` 标的维度留出：14 训练 / 6 预留，`pipeline.py` 挖掘用 train-only 配置、OOS 重算与回测用独立预留 client、方向仍从样本内库学习；一次性账本 `.universe_ledger.json` + `clean` 标记（leave-K-out 强制标注「仅验机制，不是独立证据」）+ `require_fresh` 硬开关。正式机制验证 run `run_20261114T125520Z_2722fb`：6 币 OOS 快照、真实 funding 命中 6 资产、75 笔交易、-20.53%。附带修掉 `run_backtest` 在 6 币下 **0 交易却报成功**的静默错误（`adapt_book_to_universe()`）。`scripts/download_universe_holdout.py` 提供「下载真正不重叠新币」的入口，默认拒绝伪留出。
>
> **③ 数据预算一次性烧光 + 预注册是静态批处理** → 新增 `core/experiment/windows.py` 滚动预注册。`experiment_window.json` 改 `start=auto`（锚点 = 账本最后揭示日次日，当前 **2026-11-14**，不再硬编码月初丢弃 11-14→11-30 的 bar）、按真实 bar 交集分块（30 根/窗 + 5 根 embargo，修掉 `freshness.py:213` 用日历天数当 bar 数）、滚动揭示不推移动锚点（预注册边界固定）、每窗只揭示一次。**首个可用窗口 ETA 由 2027-02-28 提前到 2026-12-13**，从第 30 根起即可累积证据。本文 §0.2 / §3 表格 / §5 路线图中所有「0/90 根、ETA 2027-02-28、2026-12-01 起」均为**当时快照，已被本补记作废**；路线图 S4 的时间判断相应收紧。
>
> **四维评分**：A=8、D=8、O=6 不变，**R 仍为 1**。全量测试 **431 passed / 1 skipped / 56 subtests**（基线 383/1，新增 48 例），无退化。仍未解决：正向 OOS 证据、PIT 资产池、OI 仅 25 行、liquidation 数据源、NN 独立特征路径、epoch 级断点续训、属性测试、实盘/风控、多用户、分布式、自动退役。

---

## 0. 执行摘要

### 0.1 一句话结论

**我们不缺功能，缺的是「三样东西接进主流程」和「一个正向结果」。** 四范式、59 算子、全套统计闸门、Web 控制台、CI/Docker、327 个测试都已具备，方法纪律在 21 个框架里是领先项；但因子库 84 个因子**全部**是 `DISCOVERED_UNVERIFIED`，没有一个通过闸门，唯一一次严格 OOS 含成本 **-6.40%**（被拒的反而 +23.35%）。所以现阶段把目标定成「照着 qlib 补平台功能」会走错方向——qlib 是最成熟的平台却不含挖掘器，AlphaMaster 工程最完整却退化成 beta。**真正的差距不是平台规模，是闭环尚未证明能赚钱，且若干已实现的能力还停在库函数 / 脚本 / 手动层，没有成为平台能力。**

### 0.2 四维成熟度评分（可复算，人为分档）

| 维度 | 得分 | 判据与证据 | 相对 21 框架的位置 |
|---|---|---|---|
| **A 能力覆盖** | **8 / 10** | 4 范式（GP/RL/LLM/NN）、59 算子含 30 参数化窗口、用户算子动态注册、Cross-Asset 截面、静态 AlphaPool、ml4t 事件驱动回测、22 个 API 端点 + React SPA、数据下载/新鲜度/增量更新 | 上游（多数竞品只有单一范式、无 Web 或无回测）；扣在实盘链路、归因层、多数据源 |
| **D 纪律可信** | **7 / 10** | NW-HAC t、BH/Holm + 累积 FDR、DSR（折净 Sharpe）、`num_trials` 跨 run 分母、一次性 `HoldoutLedger` + 全局守卫、成本入 fitness、数据质量闸门、面板校验 | **单项第一梯队**（原报告 M4 满分档）；扣分见 §3.1 的四个接线缺口 |
| **R 结果产出** | **1 / 10** | 全库重审 84 个 → **0 通过**，6 个旧 `INSPECTED` 全降级；P0 OOS 含成本 -6.40%；新窗口 `2026-12-01` 起需 90 根 1d，**当前 0/90，ETA 2027-02-28** | 给 1 分而非 0：有诚实的失败记录、预注册窗口与一次性账本，不是「没有记录」；但**有效产出为零** |
| **O 运营可复现** | **4 / 10** | 有 CI（Python 3.12 + Node 20）、多阶段 Docker + compose、27 测试文件 / 327 用例、29 篇文档、MIT、任务状态持久化与取消 | 中等；缺统一 run registry、算子集版本戳、断点续训、认证/调度/监控、属性测试 |

> **读法**：A 和 D 已经花完主要工程投入，边际收益最低；**O 是低成本高收益（多为接线）**；**R 是唯一决定「这是不是平台」的维度，且不能靠写代码补**。四项不是平均关系，R 是乘数：R=1 时 A/D 再高也只等于「一套很严谨的、暂时还没挖到东西的研究工具」。

### 0.3 最反直觉的一条

**已经有实现的三个能力，其实没接进主流程**：

| 能力 | 实现位置 | 实际接入情况 |
|---|---|---|
| 随机公式零分布 / 目标置换检验 / 经验 p | `core/evaluation/significance.py:147,169,208` | 全仓检索**只有 `tests/test_significance.py:10-12,80-102` 引用**，主流程零调用 → 前序报告把它算作领先项，属高估，见 §4.1 |
| 一次性 holdout 账本 | `core/evaluation/holdout_guard.py`、`experiment_trace.py` | CLI 与 `director.run()` 已接；但 `api/` 下零命中 → **Web 用户看不到哪些窗口已烧**，守卫还不是产品能力 |
| 累积试验次数分母 | `FdrLedger` / `ExperimentTrace` | `.fdr_ledger*.jsonl`、`.experiment_trace.jsonl` 仍被 `.gitignore` 忽略 → 换机器 clone 后 `num_trials` 归零，DSR 分母被系统性低估（前序报告已记为遗留风险） |

这不是「还缺功能」，是**「功能做完了但没成为平台能力」**——成本最低、最该先补的一类。

---

## 1. 「成熟」到底指什么

原报告 §1.3 的 M1–M5 评的是**框架成熟度**（工程规范 / 测试文档 / 实现完整度 / 统计证伪 / 维护诚实度）。但「做一个成熟的因子挖掘平台」多两层要求：

1. **可重复地产出**，而不是「跑过一次严谨实验」；
2. **换个人、换台机器、换一批数据，结论仍然成立且可审计**。

因此本轮按上表四维拆开。一个可直接对照的判据：**成熟平台应当能回答「这个因子是哪次运行、哪份配置、哪版算子集、哪版数据、哪个随机种子产生的，当时的试验分母是多少，之后在哪些窗口被验证过」**。我们目前能回答前半句的一部分（`data_lineage` 有源/交易所/币对/区间），后半句基本答不出（§3.2）。

---

## 2. 已经有的（避免把「差什么」误读成「什么都没有」）

按原报告 L0–L5 分层盘点，均已源码核实：

| 层 | 已有 | 证据 |
|---|---|---|
| L0 数据 | 20 币 1d + BTC 5m feather；60 个 funding / basis / OI 流；`RealDataClient` 按 `mine_period` / `test_period` 切片；CCXT 批量下载、命名规范统一、覆盖率与新鲜度视图、幂等增量更新 | `core/data_feed/real_client.py:25-26,126-148`、`freshness.py`、`scripts/update_data.py` |
| L1 表达 | 59 算子（30 个参数化窗口）、`FactorExpressionAST`、代码型因子 + `RestrictedSandbox`、MD5 硬去重 + 面值相关性软去重 | `core/miner/operator_runtime.py`、`core/evaluation/code_sandbox.py`、`core/miner/diversity_filter.py` |
| L2 挖掘 | GP / RL / LLM / NN 四范式 + 用户自定义 Miner / 算子 / Fitness Hook；加密特征模板 29 个经 `crypto_terminals()` 进 GP | `core/miner/paradigms/`、`core/miner/crypto_features.py`、`gp_miner.py:18,49-52` |
| L3 证伪 | HAC t / BH-FDR / Holm / DSR / 一次性 holdout ledger / 全局守卫 / 成本闸门 / 数据质量闸门 / 假设预算 | `core/evaluation/significance.py`、`holdout_guard.py`、`experiment_trace.py`、`core/data_feed/panel_validator.py` |
| L4 组合 | 静态 `AlphaPool`（互 IC 去重 + ridge 权重）、线性组合、`member_factor_ids` 权重贯通回测 | `core/miner/pool.py:74-150`、`core/miner/paradigms/base.py:412-481` |
| L5 产品化 | 事件驱动回测（下一根收盘成交、commission 4bp + slippage 5bp + funding + 杠杆）、22 API 端点、WebSocket 进度、任务持久化/取消、Dashboard / Inspector / 组合回测 / 三语（第一批）、CI + Docker | `core/backtest/backtest_engine.py:340-494`、`api/main.py`、`api/task_store.py`、`.github/workflows/ci.yml` |

> 这一层已经比 21 个框架中的绝大多数完整。**所以下面列的不是「补功能」，是「闭合链路」。**

---

## 3. 差距清单（按「现在该不该补」分档）

### 3.1 A 档 — 现在就补：接线 / 纪律，不依赖新数据（最高 ROI）

| # | 差距 | 源码证据 | 验收标准 |
|---|---|---|---|
| A1 | **把随机零分布接进晋升判定**：`target_permutation_tstats` / `random_formula_null` / `empirical_p` 只有测试调用 | `significance.py:147,169,208`；引用仅 `tests/test_significance.py:10-12` | `apply_promotion_gates` 增加可选 `empirical_p_max`，用与主流程相同的采样器生成 ≥200 个零样本；补一条「零分布能拦下 HAC t 显著但纯噪声因子」的测试 |
| A2 | **holdout 账本产品化**：Web/API 层完全看不到已烧窗口，用户可能在 UI 上重复选到消耗过的区间 | `api/` 全目录 `holdout\|ledger\|reveal` 零命中；守卫只在 CLI 与 director | 新增 `GET /api/gates/holdout` 返回已揭示窗口 + 预注册窗口进度；Launchpad 表单在选中已烧区间时前端禁用并提示；`holdout_guard` 的入口覆盖断言扩到 API |
| A3 | **`num_trials` 跨机器一致**：FDR / trace ledger 被 gitignore，fresh clone 从 0 计数，DSR 门槛被低估 | `.gitignore`（`.fdr_ledger*.jsonl` / `.experiment_trace.jsonl`）；`framework_maturity_crypto_fit.md` 顶部实施补记「仍未解决」 | 二选一：①账本改为可提交的快照 + 追加日志双文件；②启动时若检测到账本为空且 `factor_db` 非空，直接 fail-fast 要求导入历史分母。补跨进程/跨 clone 回归测试 |
| A4 | **验证集的一次性纪律**：`fitness` 以 `oos_exponent=0.5` 把 `test_period` 的 RankIC 几何混合进优化目标，每个候选每代都读验证集，却无试验次数折减、无 gap/embargo、无逐折重拟合 | `evaluator.py:39,59-60,398-470`；`configOOSHoldout.json` 的 `test_period = 2025-01-01..2025-06-30`（验证段）；全仓 `purge\|embargo\|walk_forward\|cpcv` **零命中** | 三件：①训练/验证之间加 `gap ≥ horizon_bars`；②把「验证集被读取次数」写入 `ExperimentTrace`（前序报告给 P0-2 补的新指标）；③实现**逐折重拟合**的 roll-forward（注意避开 AlphaMaster「全样本算完再切片」的假 walk-forward）。**注意口径**：这里被反复使用的是**验证段**，最终 holdout 由独立脚本重算，未被 fitness 读取——不要误写成「holdout 被污染」 |
| A5 | **默认配置就是严谨的**：主 `config.json` 的挖掘窗口只有 12 天、验证 14 天，而严谨配置要另外指定 `configOOSHoldout.json` | `user_workspace/configs/config.json` vs `configOOSHoldout.json` | 默认配置改为足以支撑统计判定的窗口（≥1 年 mine / ≥6 月 val），玩具配置移入 `examples/` 并显著标注 |

> A 档的共同特点：**不改任何统计结论、不依赖新数据、不动表达力**，但直接决定「这套系统能不能被别人信任地复用」。

### 3.2 B 档 — 可复现性与工程治理：让结论跨机器成立

| # | 差距 | 证据 | 说明 |
|---|---|---|---|
| B1 | **可复现四件套缺三**：`data_lineage` 只有源/交易所/币对/区间，无 `operator_set_hash`、无 `git_sha`、无 `config_hash` / `data_snapshot_hash`、无全局随机种子 | 抽查 `factor_db/metadata/*.json` 的 keys；`random_seed` 仅 `core/miner/nn.py:204,211,214` 有；GP / RL 用全局 `random` 无记录 | 抄 AlphaMaster `vocab.py:48-58` 的 `sha256(算子名序列)[:12]`，落库时校验；否则「算子集改了但旧因子值照用」无人发现 |
| B2 | **无统一 run registry**：`tasks.json` 存任务状态、`factor_db` 存因子，两者不绑定「哪次运行 / 哪份配置 / 哪版代码」 | `api/task_store.py`（仅状态镜像、日志裁剪到 60 行）；`factor_db/metadata` 无 `run_id` | 引入轻量 `run_id` → 配置快照 + 代码版本 + 数据快照 + 试验分母 + 产出因子 id 列表；这是「平台」与「脚本集合」的分界线 |
| B3 | **无断点续训**：长跑中断只能从头再来 | 全仓 `checkpoint\|resume\|save_state` 仅 NN 模型反序列化命中（`nn.py:47,316,404`），非训练状态 | 每个 epoch 落一次种群 / 策略权重 / 已消耗试验数，支持 `--resume`；与 B2 的 run_id 共用 |
| B4 | **工程治理细节**：327 个用例全是 example-based，无 Hypothesis 属性测试；无 ADR / CHANGELOG / CONTRIBUTING | `tests/` 27 文件；`docs/` 无 adr / CHANGELOG / CONTRIBUTING | 属性测试优先覆盖「未来函数不泄漏」「成本单调性」「账本幂等」三类数学性质（抄 AlphaMaster 的 `tests/property/`，但只抄方法不搬 AGPL 代码） |
| B5 | **单 worker + 无认证 + 无调度 + 无监控** | `docker-compose.yml` API 单 worker；`api/` 与 `core/` 的 `auth\|jwt\|user_id\|rbac\|scheduler\|celery\|ray` 零命中；`TODO.md` §3 明确「分布式评估暂缓」 | 见 §5 的「先别做」——当前规模不构成瓶颈，但**至少要有健康检查告警与任务失败通知**，否则无人值守时会静默失败 |

### 3.3 C 档 — 数据面：决定「能挖出什么」，独立于统计纪律

| # | 差距 | 证据 | 影响 |
|---|---|---|---|
| C1 | **Point-in-time 资产池缺失**：固定 20 币，用「当期非空 + `min_assets`」近似，上市/退市时间表没有 | `core/data_feed/real_client.py:64,120`（覆盖率告警措辞）；`TODO.md` §2b 明列未做 | 幸存者偏差，IC 与回测同时虚高 |
| C2 | **加密数据面名不副实**：20 个 `open_interest` 文件**各仅 25 行**（币安 30 天上限），实际不可用；liquidation 仅支持自备 feather；无多交易所价差 | 实测 `data/binance/futures/crypto_streams/*open_interest*.feather` 20 个均 25 行；`TODO.md` §2b | 60 个流里真正能用的只有 funding_rate + basis；OI 类特征模板目前是空跑 |
| C3 | **回测 funding 是常数网格**：`grid["rate"] = rate`（默认 0.0001），而真实 8h funding 流已在盘上 | `core/backtest/backtest_engine.py:340-347,437` | 成本入 fitness 用的是 18bp 往返常数、回测用另一个常数 funding；**两处口径不统一**，且都没用真实费率 |
| C4 | **一等终端只接了 GP**：`crypto_terminals()` 只在 GP，RL / NN 仍是 5 个 OHLCV | `gp_miner.py:18,49-52` vs `rl_miner.py:19,65-69` | 前序报告已指出口径：是「一等终端没接全」，不是「完全没接」 |
| C5 | 数据无版本 / 快照 hash，无法回答「这个因子用哪一版数据算的」；品种与频率单一（20 币 1d + BTC 5m） | `data/` 盘点 | 与 B1 同源 |

### 3.4 D 档 — 结果层：唯一无法靠写代码补的

| # | 事实 | 证据 |
|---|---|---|
| D1 | 因子库 **84 / 84 全为 `DISCOVERED_UNVERIFIED`**，全库重审 0 通过，6 个旧 `INSPECTED` 全降级 | 实测 `factor_db/metadata/*.json` 状态分布；`user_workspace/reports/factor_db_reaudit.md` |
| D2 | 唯一一次严格 OOS：通过闸门的 2 因子含成本 **-6.40%**（零成本 +2.22%），**被拒因子反而 +23.35%** | `user_workspace/reports/p0_holdout/README.md:52,60` |
| D3 | 新验证窗口 `2026-12-01` 起需 90 根 1d，**当前 0/90，ETA 2027-02-28** | `user_workspace/experiment_window.json`；`TODO.md` P0.5 |
| D4 | 根因已有诊断但未被消解：1 日目标 + 20 币的年化换手成本 **6–17%** 大于可得边际 → **必须在交易周期上挖** | `user_workspace/reports/p0_5_return_gate.md` |

> **这一档才是「平台」与「研究工具」的真正分界**：闸门已经能防假阳性，但**没有证明能排序经济价值**。在 D 未破之前，A/B/C 都是「让一个还没产出的方法更严谨」，价值在于不返工，而不在于提高产出。

### 3.5 明确缺、但排在最后的功能（列全，不代表现在做）

- **实盘 / 模拟盘链路**：`core/execution/compiler.py:109-114` 明写 `Live server deployment transport is not implemented`；无下单、无风控（止损/最大回撤/日内限制）、无持仓落地，回测与实盘不共用信号函数（AlphaMaster 反而做了三端共用）。
- **组合与归因**：`AlphaPool` 权重一次拟合后固定（`pool.py:74-150` 的 `_weights` 无时变更新）；无风险模型 / 约束优化 / 中性化；`TODO.md` §4 的 NN 特征归因、暴露分解、Brinson 未做。
- **生命周期自动化**：`DISCOVERED → INSPECTED → PAPER_TRADING → LIVE → RETIRED` 全靠手动改状态，无 IC 衰减监控与自动退役（`entities.py:12` 只是注释里的设计）。
- **服务化规模**：Ray / Celery 分布式评估（`TODO.md` §3 已明确暂缓）、任务调度、多用户权限、数据库与并发控制。
- **表达力扩张**：扩算子、堆范式、逐时间截面时变权重。

**共同理由**：qlib 是最成熟的平台但不含挖掘器，AlphaMaster 工程最完整却因证伪为零退化成 91.8% 做多的 beta。在 R=1 时堆这些，是把复杂度建在零产出上，负 ROI。

---

## 4. 三处对既有报告的口径修正

### 4.1 「随机公式零分布」被高估

`alphamaster_competitor_review.md` §4 与 `framework_maturity_crypto_fit.md` 的能力对照中，把「随机零假设 / 随机公式零分布」列为 FactorMiner 的领先项。**精确检索结果**：三个函数 `target_permutation_tstats` / `random_formula_null` / `empirical_p` 定义在 `significance.py:147,169,208`，全仓非测试引用为 **0**。正确表述应为：

> **已实现为库函数并有单元测试覆盖，但未接入 `apply_promotion_gates` 或任何主流程**。因此「有实现」成立、「闸门已启用」不成立。AlphaMaster 在这一点上仍是「全仓零命中」，我们仍领先一个身位，但优势比原文所述小。

这一条同时是 §3.1 A1 的由来。

### 4.2 fitness 混合的是**验证段**，不是最终 holdout

`evaluator.py:398-470` 确实把 `test_period` 的 RankIC 以 0.5 权重混合进 `fitness_score`，前序报告写「搜索目标：样本内外 RankIC 混合」是对的，但容易读成「holdout 被优化目标污染」。核实 `configOOSHoldout.json`：`test_period = 2025-01-01..2025-06-30`（验证段），最终 holdout `2025-07-01..2026-11-13` 由独立脚本重算、**不进 fitness**。所以准确表述是：

> **验证集被每代每候选反复读取用于选优（与 AlphaMaster 的 val 复用同型，只是我们多一层独立 holdout 兜底）**。缺的是验证集的试验次数折减与 gap/embargo/逐折重拟合，不是「最终 holdout 已泄漏」。

这也解释了为什么「每段新数据都得重新烧一个窗口」——**验证纪律不可复用**，是平台不能持续复验的根因。

### 4.3 回测 funding 已实现，但是常数、且与 fitness 成本不同源

前序报告写「成本入 fitness」「回测有下一根成交」，均成立。补充精确口径：`backtest_engine.py:340-347` 的 `build_funding()` 生成的是 `grid["rate"] = 0.0001` 的**常数网格**，不是真实 8h 资金费序列；而 `DEFAULT_FITNESS_CFG["gate_cost_rate"] = 0.0018`（`evaluator.py`）是另一套 18bp 往返成本。**两处成本口径不统一**，且都没用已在盘上的 `funding_rate` 流。这是 §3.3 C3 的由来，也修正了「成本模型已闭合」的印象。

---

## 5. 路线图：什么先做，什么等，什么不做

| 阶段 | 内容 | 前置 / 验收 | 为什么在这个位置 |
|---|---|---|---|
| **S1（现在，1 轮改动）** | A1 零分布接线、A2 账本 API 化、A3 `num_trials` 跨机器一致、A5 默认配置、B1 算子集版本戳 | 不依赖新数据；验收 = 全量测试不退化 + 新增接线断言 | 已实现但未成为平台能力，成本最低、直接提升 D 与 O |
| **S2（现在，与 S1 并行）** | A4 gap/embargo + 验证集读取计数 + **逐折重拟合** roll-forward；B2 run registry；B3 断点续训 | 验收 = 用一个历史窗口做多段复验，账本能列出每次揭示 | 让「验证」可复用，不必每验一次烧一段数据 |
| **S3（等数据，被动但要有设计）** | C1 PIT 资产池、C2 OI 数据源（换 30 天上限的供应商或改频率）、C3 真实 8h funding 入回测并统一两处成本、C4 RL/NN 终端接入 | 验收 = 60 个流里 ≥3 类可用、两处成本同源 | 扩大「能挖出什么」，独立于统计纪律 |
| **S4（2026-12 起累积，2027-02 后可验）** | P0.5 预注册正向实验；**把目标周期/换手按 `p0_5` 结论重新设计**（1d 目标 + 20 币的成本结构不支撑当前边际）；用等待期做前瞻 paper trading 记录 | 90 根新 1d + 一次性揭示 | 唯一能改变 R 的阶段；在此之前所有工程都是防返工 |
| **S5（R 破之后）** | 实盘链路、动态权重 / 风险模型 / 归因、认证与多用户、调度监控、分布式评估、生命周期自动化 | 至少 1 个通过闸门且含成本为正的因子 | 有了可上线的东西，平台化投入才有回报 |

**一句话排序**：先接线（S1）→ 让验证可持续（S2）→ 补数据面（S3）→ 等新数据拿正向证据（S4）→ 才谈平台化扩展（S5）。**不要先扩算子、不要先做实盘、不要先上分布式**。

---

## 6. 附录：本轮检索索引

| 结论 | 位置 |
|---|---|
| 四维评分硬指标 | `tests/` 27 文件 / 327 用例；`api/main.py` 22 端点；`docs/` + `user_workspace/reports/` 29 篇 md；`.github/workflows/ci.yml`、`Dockerfile`、`docker-compose.yml` |
| 因子库状态 | `factor_db/metadata/*.json`（84 个，全 `DISCOVERED_UNVERIFIED`） |
| 零分布未接入 | `core/evaluation/significance.py:147,169,208`；`tests/test_significance.py:10-12,80-102` |
| fitness OOS 混合 | `core/evaluation/evaluator.py:39,59-60,398-470`；`user_workspace/configs/configOOSHoldout.json` |
| 时间隔离缺失 | 全仓 `purge\|embargo\|walk_forward\|cpcv` 零命中（仅 `pool.py:115` 的 `np.roll` 注释含 "roll forward"） |
| 可复现字段 | `core/miner/entities.py:25` `data_lineage`；`core/miner/nn.py:204` 唯一 `random_seed` |
| 数据面 | `data/binance/futures/crypto_streams/*open_interest*.feather`（20 × 25 行）；`core/data_feed/real_client.py:64,120` |
| 回测成本 | `core/backtest/backtest_engine.py:340-347,437,482-483`；`core/evaluation/evaluator.py:47` |
| 终端接入差异 | `core/miner/paradigms/gp_miner.py:18,49-52` vs `rl_miner.py:19,65-69` |
| 实盘未实现 | `core/execution/compiler.py:109-114` |
| 结果层 | `user_workspace/reports/p0_holdout/README.md:52,60`、`p0_5_return_gate.md`、`factor_db_reaudit.md`、`user_workspace/experiment_window.json` |

---

*本报告由当前工作区源码实读整理，未运行任何新增实验。四维评分是人为分档、规则见 §0.2，用于相对定位而非绝对评价；所有「未找到」均为精确检索无命中。*
