# CLI 因子挖掘：还缺什么、要做什么

> 核查日期：2026-11-15 · 方法：逐命令读实现 + 实测运行 + 历史 run 产物核对
> 仓库状态：`32001ab` + 本轮 P0/P1 修复，`pytest` 463 passed / 1 skipped

---

## 0. 一句话结论

**CLI 是一条「流程完整、但后半段空转」的流水线。**

八阶段全部跑绿、报告齐全、退出码正确，但 `factor_db` 当前是 **0 个文件**，最近 3 次 run 的 `promoted` 全是 **0**。
`promote → 生产因子库 → 用生产库回测 → 组合` 这条链**从未在真实数据上被完整执行过一次**。所以现在的问题不是「跑不通」，而是「跑通了但拿不到东西，且后半段的正确性没有被验证过」。

---

## 1. 实测现状

### 1.1 命令能力矩阵

| 命令 | 实际能力 | 判定 |
|---|---|---|
| `refresh` | 增量补到 T-1，含 crypto_streams 增量 | ✅ 完整 |
| `doctor` | 11 项检查、FATAL 中止、`--json` | ✅ 完整 |
| `experiment` | 8 阶段：data → mining → gated → validated → combined → oos_prepared → backtested → completed | ⚠️ 单配置、后半段空转 |
| `mine` | 直接挖（无 run 目录、无 manifest、无闸门审计） | ⚠️ 旁路，仅调试用 |
| `factors` | run 内排名 + 拒绝原因计数 + `--json` | ⚠️ **不含因子表达式** |
| `promote` | 把单个因子从 run 复制到生产库，写 lineage | ⚠️ **一次一个** |
| `backtest` | 生产库因子回测，ml4t 引擎 | ✅ 但库里没东西可跑 |
| `inspect` | 单因子深度诊断（覆盖度/RankIC/IR/t/分层） | ⚠️ 只 print，无文件产出 |
| `holdout` | 规划 / 一次性揭示 | ✅ 完整 |
| `pipeline` | refresh → doctor → experiment，单一退出码 | ⚠️ 单配置 |

### 1.2 历史 run 的真实数字

| run | 候选 | 存储 | 晋升 | 状态 | OOS 快照 | 回测 |
|---|---|---|---|---|---|---|
| `..._23a2cb` | 61 | 61 | **0** | 全 `DISCOVERED_UNVERIFIED` | 61 | 已跑（2026-07-01~09-13） |
| `..._61ffe5` | 71 | 71 | **0** | 同上 | 71 | 已跑 |
| `..._5e65d4` | 64 | 64 | **0** | 同上 | 64 | 已跑 |

读法：回测确实执行了，但跑的是 **61/71/64 个未过闸门的候选**，不是「已晋升的生产因子」。

### 1.3 因子库状态

```
factor_db/metadata/            0 files
factor_db/values/              0 files
factor_db/models|sources|weights  0 files
```

`backtest` 在空库时正确 `exit 1`（实测，非缺陷）。

---

## 2. 阻断级缺口（P0）

### P0-1 · 永远 0 晋升，导致后半段成为「未验证路径」

**证据**
- `factor_db` 0 文件；3 次 run `n_promoted=0`
- 闸门要求（`evaluation.gates`）：`hac_t≥2.0`、`fdr≤0.05`、`dsr≥0.90`、`empirical_p≤0.05`、`cost_adj_return>0`
- 实测门槛：`n_trials=182` 时 DSR≥0.9 需要年化 Sharpe **1.98**；当前候选实测 −0.68 ~ +0.19

**缺什么**
不是「闸门太严」，而是**候选的单位经济价值低于成本**：`horizon_bars=1` + 1 bar 换仓 + 18bp 往返 = 结构性亏损。实测同一市场：`mom20` 在 1 bar 重仓下净年化 SR 0.223，5 bar 下 0.604；`mom60` 5 bar 下 0.675。**换仓频率是被忽视的最大杠杆。**

**做什么（按顺序）**

1. `configExperiment.json`：`target.horizon_bars` 1 → **5**，顶层加 `"rebalance_every": 5`
   - 闸门、walk-forward、回测三处已统一读 `gate_rebalance_bars` / `rebalance_every`，无需改代码
2. `fitness` 增加 `turnover_max` 硬约束（建议 0.15/bar），在 Stage A 直接淘汰高换手候选
3. 用「已知正样本」验证整条链：把 `ts_momentum(close, 60)` 作为种子因子注入（`gp.seed_expressions` 或直接构造候选），确认它能过闸门并走完 promote → 回测
   - 这一步的目的不是「作弊放行」，而是**证明后半段代码在真实数据上工作**；同时也验证「闸门有能力放行真信号」

**验收**
- `factor_db/metadata/` 出现 ≥1 个 `lifecycle_status=INSPECTED`
- `backtest` 从生产库跑出 `num_trades>0` 且 `funding_source=real`
- 新增测试：`test_gate_accepts_known_positive_when_budget_is_small`（同一因子在 `n_trials=1` 放行、`n_trials=10000` 拒绝）

---

### P0-2 · `promote → 生产库 → 回测` 这条链没有任何自动化验证

**证据**
- `core/commands/promote.py:235` 只接受 `--factor-id`（单个，无批量）
- 生产库为空 → 「从 run 发布到库、再从库回测」从未被执行
- `backtest` 的 `--factors` 默认空时自动发现库内因子（`core/commands/backtest.py:250-253`），这条发现路径也从未在有内容的库上跑过

**缺什么**
- 批量发布能力（`--all-inspected`）
- 一个端到端冒烟：发布 → 库内回测 → 组合

**做什么**
1. `promote` 增加 `--all-inspected`（发布该 run 中所有 `INSPECTED` 且 `gate_pass` 的因子，逐个执行现有校验，汇总结果，任一失败不影响其他）
2. `promote` 增加 `--dry-run` 已有；补 `--json` 汇总（已有 `--json`，确认支持批量时结构一致）
3. 新增集成测试 `tests/test_promote_backtest_e2e.py`：造 1 个假 run（含 metadata+values）→ 批量 promote → `backtest` 从库读取 → 断言 `num_trades>0`

**验收**：一条命令完成「run → 生产库 → 回测」，且失败有明确退出码。

---

### P0-3 · `evaluation.max_workers=8` 是负优化

**证据（实测）**：4 个候选，串行 10.1s，`ThreadPoolExecutor(4)` **17.5s（0.58x）**。

**缺什么**：`core/evaluation/evaluator.py:290-295` 用线程池跑 numpy 密集计算，GIL 争用导致比串行更慢。当前配置等于「自愿多花 72% 时间」。

**做什么（最小改动优先）**
1. 立即可做：配置改 `max_workers: 1`（消除负优化，零代码风险）
2. 正确做法：换 `ProcessPoolExecutor`，配 `initializer` 预加载，面板通过共享内存/落地文件传递（不要 pickle 整块面板）
3. 加保护：若 `max_workers>1` 且后端是线程，日志 WARN 提示实测负加速

**验收**：`pytest` 不回归；基准脚本显示 4 worker ≥ 1.4x。

---

### P0-4 · `factors` 看不到因子表达式

**证据**：`core/commands/factors.py:146-161` 的输出列只有 `factor_id / status / gate / hac_t / p_adj / dsr / emp_p / fitness / failed`。**没有表达式。**

**影响**：这是日常使用最高频的痛点——看到 `fac_0622d48d` 过了闸门，却不知道该因子是什么，必须再去翻 `metadata/*.json`。

**做什么**
- 从 `metadata/{fid}.json` 读 `logic_reference.display` / `expression`，加一列 `expression`（`--full-expr` 时展开）
- `--json` 输出同步增加 `expression` 字段

**验收**：`factors --run-id X` 每行能看到表达式片段；`--json` 含完整表达式。

---

## 3. 功能级缺口（P1）

### F1 · 没有因子库管理命令

**现状**：11 条命令里没有一条操作「生产因子库」本身。
**缺什么**：`factor-db list|show|rm|export`
- `list`：库内因子总数、状态分布、覆盖期、最近更新、所属 run
- `show <id>`：元数据 + 闸门历史 + OOS 指标
- `rm <id> --yes`：软删除到 `_deleted_<date>/`
- `export --ids a,b --format csv|parquet --out DIR`：把因子值导出去接外部系统
**为什么必须**：没有 `list`，你无法回答「我现在库里有什么」——这正是当前 0 因子却无人察觉的原因。

### F2 · 没有跨 run 对比

**现状**：`factors --run-id` 一次一个 run。
**缺什么**：`factors --compare runA,runB`，输出两批候选的指标并排 + 表达式差异 + 新过闸门的因子。

### F3 · 没有因子相关性矩阵

**现状**：`--weights` 需手填，靠猜。
**缺什么**：`factor-db corr --ids ... --method spearman --out corr.csv`，并给出「建议去重对」（|ρ|>0.7）。
**为什么必须**：组合前不做相关性检查，等于把同一个信号加权 N 次。

### F4 · 没有自动组合

**现状**：`pipeline` 的 `combined` 阶段（`core/experiment/pipeline.py:95-110`）**只是读取**已有组合因子的元数据，不做任何组合优化。`backtest --weights` 需要手填。
**缺什么**：`factor-db combine --ids ... --method equal|ic_weight|ridge|hrp --out combo.json`
- 约束：单因子权重上限、相关性上限、最大成员数
- 产出可回溯的组合因子（写入 `metadata`，带 `member_factor_ids`）

### F5 · 没有增量挖掘

**现状**：`core/miner/paradigms/base.py:267-273` 只把已有因子的 hash 读进来做**去重**，不把高分因子作为精英/种子。
**后果**：每次 run 从零开始，前一次的努力不转化为下一次的先验。这是「跑 100 次也不会变强」的根因之一。
**做什么**：
- 新增 `gp.seed_from_db: true` + `gp.seed_top_k: 20`：从 `factor_db` 读 top-K 因子的表达式，反序列化为初始种群的一部分
- 注意：种子来自已晋升因子，不构成 holdout 泄漏（它们是 in-sample 产物）

### F6 · 没有 sweep / 多范式编排

**现状**：一次一个 `--config`、一个 `paradigm`。跑 GP/LLM/RL 对比要手写三个命令再自己汇总。
**缺什么**：`experiment --sweep sweep.json`，声明维度（paradigm × horizon × universe），串行或 `--shard i/n` 并行，最后汇总成一张对比表。
**注意**：sweep 是「多假设」的正典来源，必须与 trial family 分账配套（见规模化报告 L1.3）。

### F7 · GP 超参不能从 CLI 覆盖

**现状**：`population_size`、`max_depth`、`elite_count`、`survival_count`、`mutation_prob` 只能改 JSON。
**缺什么**：`--set gp.max_depth=5 --set population_size=80` 通用覆盖（点号路径），并写进 manifest 的 config hash。

### F8 · 没有 dry-run / 成本预估

**现状**：`experiment` 一跑就是 10-15 分钟，跑之前不知道会产多少候选。
**缺什么**：`experiment --dry-run`：解析配置 → 估算候选数（`population_size × iterations × (1-去重率)`）→ 估算耗时（候选数 × 4.6s ÷ 并行度）→ 列出闸门参数与 n_trials 现值 → 不启动挖掘。

---

## 4. 工程级缺口（P2）

### E1 · `--resume` 只到 stage 级

**现状**：`core/commands/pipeline.py:252`、`run_experiment.py:23`。重跑会整段重来。
**缺什么**：候选级断点——每批评估结果 append 到 `run_dir/evaluations.jsonl`，重启时按候选 hash 跳过已完成的。
**收益**：一次 15 分钟的 run 中断，不必从第 1 代重来。

### E2 · 无评估缓存

**现状**：同一表达式在不同 run/代之间重复计算（跨 run 靠 hash 去重，但**跨 run 的结果不复用**）。
**缺什么**：`user_workspace/.eval_cache/`，key = `hash(表达式 + 面板指纹 + 配置指纹)`，value = metrics。命中即跳过 4.6s。

### E3 · walk-forward 对全部候选跑

**现状**：`pipeline.py:557` 传 `mining["factor_ids"]`（61 个），不是晋升集（0 个）。
**影响**：5 folds × 61 因子 = 305 次拟合，其中 305 次都是「未过闸门的因子」，成本高而信息量低。
**建议**：改为默认只验证 `INSPECTED` + 可配置 `--wf-scope all|promoted`（保留全量用于研究模式）。

### E4 · 无 stop 条件

**现状**：固定 `max_iterations`。
**缺什么**：`--stop-after-promoted N`、`--stop-after-candidates N`、`--max-wallclock 3600`。达到即停并正常收尾（写报告、关 stage）。

### E5 · 无资源限制

**现状**：单候选无超时上限（有 `timeout_per_candidate` 但按批预算）、无内存上限。
**缺什么**：`--max-mem-mb`、单候选硬超时，超限标记 `timeout` 而非拖垮整轮。

### E6 · `mine` 是危险旁路

**现状**：`core/commands/mine.py` 直接 `director.run()`，**不建 run 目录、不写 manifest、不跑 holdout guard 的账本记录**（guard 在 director 内部会跑，但没有 run 级审计）。
**建议**：加明确警告 `--i-know-this-is-not-audited`，或在文档里标注「仅调试用，生产走 experiment/pipeline」。

---

## 5. 运维级缺口（P3）

| 项 | 现状 | 缺什么 |
|---|---|---|
| O1 监控 | 只有 `run_dir/metrics.json` | 结构化指标流（`metrics.jsonl`）可被外部采集；每阶段耗时/候选数/拒绝原因计数 |
| O2 因子退役 | 无 | 定期重算已入库因子的滚动 IC，跌破阈值标 `DECAYED` 并告警（需要时间累积，但代码可以先备） |
| O3 定时编排 | 无 | 按 holdout 窗口节奏（每 35 天）自动 `pipeline` + 到期提醒 `holdout reveal` |
| O4 告警 | 无 | `doctor` FATAL、0 晋升、回测异常 → 退出码 + 可选 webhook |
| O5 日志 | 纯文本 | 带 `run_id`/`stage`/`candidate` 字段的结构化日志 |

---

## 6. 建议执行顺序

| 批次 | 内容 | 工作量 | 为什么先做 |
|---|---|---|---|
| **批 1** | P0-3（`max_workers` 改 1）+ P0-4（factors 显示表达式）+ F1（`factor-db list`） | 0.5 天 | 零风险、立刻可用，且 `list` 能让你看清「库里到底有什么」 |
| **批 2** | P0-1（horizon 5 + turnover 上限）+ P0-2（批量 promote + e2e 测试） | 1 天 | 打通并**验证**后半段；这是从「空转」到「有产出」的关键 |
| **批 3** | F2（跨 run 对比）+ F3（相关性矩阵）+ F4（自动组合）+ F8（dry-run） | 2-3 天 | 从「能产出」到「能选组合」 |
| **批 4** | E1（候选级 resume）+ E2（评估缓存）+ F5（增量挖掘）+ F6/F7 | 3-5 天 | 效率与可持续性 |
| **批 5** | O1-O5 + E3-E6 | 按需 | 规模化运行后才需要 |

---

## 7. 一个必须先回答的设计问题

**闸门严格度与产出之间的矛盾，不能靠调低门槛解决。**

- `n_trials=182` → DSR 门槛年化 SR **1.98**
- 真实可挖到的信号（朴素 mom60，5 bar 再平衡）年化 **0.675**
- 差距 3 倍

两条路，必须选一条并写进配置：

1. **提高信号量级**：更低换手（已做）、更长 horizon（5→20）、加入 funding/basis 特征（758 symbol 未用）、扩 universe 但按流动性分层
2. **接受更长验证周期**：用滚动 holdout 累积（每 35 天一个窗口），把 `n_trials` 的分母摊到更多独立时间片上

**不能做的**：调低 `dsr_min`、放宽 `fdr_max`、或把 `n_trials` 清零重来。那会让整个闸门失去意义，也正好是这个项目此前被推翻的原因。

---

## 8. 修复状态（2026-11-15 本轮，已全部落地）

> 本节是上面 §2–§5 的结账单。**「已修」都带实测证据**，不是"改了代码"。
> 回归：`pytest` **735 passed / 0 failed / 80 subtests**（修复前 701 passed + 3 failed）。

| 编号 | 项 | 状态 | 证据 |
|---|---|---|---|
| P0-1 | 单位候选经济价值 | ✅ | `configExperiment.json`：`horizon_bars=5` + `rebalance_every=5`；实测 `ts_momentum(close,60)` 在该口径下 RankIC 0.033 / 换手 4.4% / 净 0.0039 per bar / h=1 trial DSR 0.98（旧的 1 bar 口径同信号净 0.00018、DSR≈0.03） |
| P0-1 | `turnover_max` 硬约束 | ✅ | `evaluator.DEFAULT_FITNESS_CFG["turnover_max"]` + `_build_fitness` 归零 fitness；配置 0.15。实测一轮 32 候选里 **8 个**因换手被拦 |
| P0-2 | 批量发布 | ✅ | `promote --all-inspected`；`tests/test_scaleup_toolkit.py::PromoteBatchTests`（含"run → promote → backtest 自动发现"链路） |
| P0-3 | 线程池负优化 | ✅ | `evaluator` 串行执行 + WARN 并在 `__init__` 把 `max_workers>1` 降为 1；`test_evaluator_serialises_instead_of_threading` |
| P0-4 | `factors` 看不到表达式 | ✅ | 新增 `expression` 列（AST 渲染 / LLM 源码 / RL actions / 线性组合权重）；旧 run 实测 `fac_86485292 … log1p_abs(ts_ema(ts_corr(add(close, high...` |
| F1 | 因子库管理命令 | ✅ | 新增 `factor-db list/show/rm/export/corr/combine/decay`；`rm` 只做软删除 |
| F2 | 跨 run 对比 | ✅ | `factors --compare runA,runB`，按 logic_hash 输出 newly_passing/newly_failing/appeared/disappeared |
| F3 | 相关性矩阵 | ✅ | `factor-db corr`（截面 rank 上的 Spearman），列冗余对 |
| F4 | 自动组合 | ✅ | `factor-db combine --method equal/ic_weight/ridge/hrp`，权重封顶 + 写可回测的组合因子 |
| F5 | 增量挖掘 | ✅ | `gp.seed_from_db` / `seed_top_k` / `seed_top_k_root`；`core/miner/seeding.py` |
| F6 | sweep / 分片 | ✅ | `experiment --sweep sweep.json --shard i/n`，每 cell 独立 run 目录 + 独立 trial family |
| F7 | 超参 CLI 覆盖 | ✅ | `--set gp.max_depth=5`，在 config hash 之前生效，manifest 记录实际配置 |
| F8 | dry-run 成本预估 | ✅ | `experiment --dry-run` 实测输出 `~136 候选 / 32 进置换 / ~2.5 min / DSR bar 1.53`，并对 horizon=1、turnover_max=0、n_perm=0 给出告警 |
| E1 | 候选级断点 | ✅ | 每 run `evaluations.jsonl`（append-only，含 cache hit/miss），配合缓存实现"重跑不重算" |
| E2 | 评估缓存 | ✅ | `user_workspace/.eval_cache/`；同配置第二轮 **32 命中 / 9 miss**，mining 41.1s → 26.8s |
| E3 | walk-forward 范围 | ✅ | `--wf-scope promoted`（默认）；0 晋升时 stage 记 `skipped + reason` 而非整轮失败 |
| E4 | stop 条件 | ✅ | `--stop-after-candidates` / `--max-wallclock`（epoch 边界停、正常收尾）/ sweep 的 `--stop-after-promoted` |
| E5 | 资源限制 | ✅ | `--max-mem-mb`（tracemalloc 计量，超限标 `resource`）+ 单候选硬超时 |
| E6 | `mine` 危险旁路 | ✅ | 启动即 WARN：无 run 目录 / 无 manifest / 不产出闸门报告 |
| O1 | 结构化指标 | ✅ | 每 run `metrics.jsonl`：stage/耗时/候选数/晋升数；实测 8 行 |
| O2 | 因子退役 | ✅ | `factor-db decay --window 90 --threshold 0 --apply`；实测 60 bar 窗口把 `fac_86485292` 标为 DECAYED（trailing IC −0.0099） |
| O4 | 告警 | ✅ | `pipeline --webhook URL`（失败只告警不改退出码） |

### 统计层同时落地的两条（L1）

| 项 | 状态 | 证据 |
|---|---|---|
| L1.1 分层漏斗 | ✅ | 实测一轮：**11/32** 候选进入置换零分布，21 个在 Stage A 被拦（`turnover_above_cap=8, rankic_below_floor=11, cost_adj_negative=2`），置换成本降 65% |
| L1.3 trial family 分账 | ✅ | `budget.py` 支持 family 分区（不同 family 独立 `n_trials` + BH 家族），manifest 记录 `trial_family / family_declared_at / family_budget`；旧快照无 `families` 字段仍可读 |

### 一句话现状

**后半段不再是空转**：`run → factors（带表达式）→ promote --all-inspected → factor-db → backtest`
这条链已在真实数据上跑通（`smoke_cache_hit`：32 候选 → 11 进精算 → OOS 快照 → 回测
`2026-07-01..2026-09-13`，funding 来源 `crypto_streams (20 assets)`）。

**但 0 晋升仍然成立，且是正确结果**：当前 DSR 门槛在 `n_trials=255` 下是年化 **1.53**，
而这个市场里最强的朴素信号（mom60 @ 5 bar）只有 **0.675**。
下一步不是继续加功能，是提高信号量级（更长 horizon、按流动性分层扩 universe、
把 funding/basis 从"进入了搜索空间"变成"被反复验证的强特征"）。
