# 因子挖掘的生产可用性评估与规模化路线

> 评估日期：2026-11-15 · 方法：源码级核查（附 `file:line`）+ 真实数据实测复现
> 基线仓库状态：commit `32001ab` + 本轮 P0/P1 修复，`pytest` 463 passed / 1 skipped

---

## 0. 结论速览

| 判断 | 结论 |
|---|---|
| **单机研究流水线** | ✅ **生产可用**。七条命令闭环（refresh → doctor → experiment → factors → promote → holdout → pipeline），有 preflight 硬中止、append-only 审计账本、可复现四件套、统计闸门不可绕过。 |
| **生产级因子工厂** | ❌ **不可用**。缺批量调度、并行评估（线程并行实测**负优化**）、候选级断点、监控告警、容量规划。 |
| **"多挖就能多产出"** | ❌ **不成立**。`n_trials` 是全局单调累积（`core/experiment/budget.py:9-13`），挖得越多 DSR 门槛越高，而当前信号量级低于门槛 3 倍以上。 |
| **最大的一处浪费** | 本地已有 **755 个 symbol 的 1d OHLCV + 758 个 symbol 的 funding_rate**，配置只用 **20 个 symbol、只用 OHLCV**。 |
| **最反直觉的发现** | 朴素 `mom60`（60 日动量、5 bar 再平衡）在 20 大币上净年化 SR **0.675**，高于所有已挖出因子（-0.30 ~ +0.19）。**问题不在数据量，在搜索空间与目标函数。** |

---

## 1. 实测基线（全部为本机真实测量，非估算）

### 1.1 单因子评估成本

| 环节 | 耗时 | 占比 |
|---|---|---|
| `ic_series`（1200×20 面板） | 25 ms | 0.5% |
| `newey_west_tstat` | 0.1 ms | ~0% |
| `long_short_returns`（含成本/换手） | 17 ms | 0.4% |
| **`target_permutation_tstats`（n_perm=60）** | **4599 ms** | **97.8%** |
| 合计 | **≈ 4.6 s / 候选** | 100% |

置换零分布独占 98% 成本，其余全部统计量加起来不到 50 ms。这是所有规模化决策的核心事实。

### 1.2 并行效率（`evaluation.max_workers=8` 的实际效果）

| 方式 | 4 个候选的墙钟 | 相对串行 |
|---|---|---|
| 串行 | 10.1 s | 1.00x |
| `ThreadPoolExecutor(4)`（**当前实现**） | 17.5 s | **0.58x（更慢）** |
| `ProcessPoolExecutor(2)` | 3.1 s | 1.43x |
| `ProcessPoolExecutor(4)` | 2.6 s | 1.68x |

当前 `core/evaluation/evaluator.py:291` 用线程池 + `numpy/pandas`：numpy 的排序在 GIL 下有争用，实测**比串行慢 72%**。`evaluation.max_workers` 这个参数在评估阶段是负优化。

进程池只有 1.68x（4 核）——因为 Windows `spawn` 每进程要重新 import numpy/pandas/core，且面板要 pickle 传输，启动开销吃掉了并行收益。

### 1.3 当前产出速率

- 配置：`population_size=40`、`max_iterations=3`、`survival_count=20`
- 实际：单 run **61–71 个候选**，去重后进闸门
- 耗时：约 10–15 分钟 / run（含 8 阶段流水线）
- 吞吐：**≈ 4–6 个候选/分钟**，单机

若按当前架构硬推到 10 万候选：`100000 × 4.6s ÷ (1.68 进程加速) ≈ 76 小时`（单机 4 核），且见 §3。

---

## 2. 五维生产可用性评分

| 维度 | 评分 | 依据 |
|---|---|---|
| **流程闭环** | ★★★★★ | refresh/doctor/experiment/factors/promote/holdout/pipeline 全齐；`doctor` 11 项检查、FATAL 中止；`pipeline` 单一退出码 |
| **可复现性** | ★★★★★ | manifest（`hash_json`/`git_sha`/`operator_set_hash`/`data_snapshot`/seed）；`--seed` 贯穿；实例级 RNG |
| **统计纪律** | ★★★★★ | 闸门不可绕过；OOS 不反灌选优（`director.py:105`）；holdout 一次性揭示 + append-only 账本；BH-FDR 跨 run 累积 |
| **吞吐/规模** | ★☆☆☆☆ | 单配置单进程；线程并行负优化；无队列/sweep；`resume` 仅 stage 级（`core/commands/pipeline.py:252`） |
| **可运维性** | ★★☆☆☆ | 有 run registry 与结构化报告；无指标上报、无告警、无失败重试编排、无资源配额 |

**判定**：一个人、一台机器、按批次手动跑——完全可用。当服务跑、多人并发、每天几百 run——不可以。

---

## 3. 三个硬约束

### 3.1 统计约束（最硬，且不可绕过）

`n_trials` 是**全局单调累积**的单例（`core/experiment/budget.py:9-13`：*"Semantics: monotonic. sync never lowers a stored count"*），所有 run 共享同一个分母。

实测 DSR≥0.90 所需的 bar 级 Sharpe（`n_obs=1200`，默认 `sigma=1/sqrt(T)`）：

| n_trials | 所需 SR_bar | 折年化（×√252） |
|---|---|---|
| 1 | 0.0370 | **0.59** |
| 10 | 0.0991 | 1.57 |
| 100 | 0.1248 | 1.98 |
| 1,000 | 0.1445 | 2.29 |
| 10,000 | 0.1612 | 2.56 |
| 100,000 | 0.1759 | **2.79** |

门槛只按 `sqrt(2 ln N)` 增长——**很温和**。好消息：规模化不会让门槛爆炸。坏消息：当前信号离门槛太远。

真实对照（20 币、2023-04-01~2026-06-30、18bp 往返）：

| 因子 | 再平衡 | turnover | net/bar | 净年化 SR | DSR(n_trials=182) |
|---|---|---|---|---|---|
| `mom60` | 5 bar | 4.4% | +0.00056 | **0.675** | 0.036 |
| `mom20` | 5 bar | 7.9% | +0.00046 | 0.604 | 0.027 |
| `mom20` | 1 bar | 18.9% | +0.00018 | 0.223 | 0.003 |
| 已挖出因子（3 个快照） | 1 bar | ~1% | -0.00027~+0.00008 | -0.68~+0.19 | <0.01 |

**读法**：`mom60` 的年化 0.675 已接近 `n_trials=1` 的门槛 0.59——如果这是唯一一次试验，它会被判显著。但 `n_trials=182` 已把门槛推到 1.98，于是它被正确拒绝。

**结论**：瓶颈是**单位信号量级**，不是算力，也不是试验次数。把候选数从 100 提到 10 万，门槛只从 1.98 抬到 2.79（+41%），而你仍然一个都过不了。

### 3.2 算力约束

- 98% 时间在置换零分布 → 降本必须从这里下手
- 线程池是负优化 → 必须换进程
- 单机 4 核仅 1.68x → 真正的吞吐提升只能靠**跨 run 分片**（每个进程跑独立配置/universe，而不是把一代候选拆开）

### 3.3 数据约束

| 资源 | 本地已有 | 配置使用 | 利用率 |
|---|---|---|---|
| 1d OHLCV（futures） | 755 symbol | 20 symbol | **2.6%** |
| funding_rate（8h） | **758 symbol，~4000-5800 行/个，2023 起** | **0（仅用于回测成本）** | **0%** |
| open_interest | 20 symbol | 0 | 0% |
| basis | 20 symbol | 0 | 0% |
| 1h / 5m / 1m | 各 1-2 个 symbol | 0 | ~0% |

funding rate 在加密市场是公认的强信号源（carry / 拥挤度 / 情绪代理），本地有 758 个 symbol 的完整历史，**却完全没有进入搜索空间**（`search_space.allowed_operators` 只对 OHLCV 面板做算子组合）。

同时：横截面只有 20 个资产，且都是高相关大币（共同 beta 高），**有效独立资产数远小于 20**，这直接压制了多空组合的分散化收益。

---

## 4. 为什么"挖更多"不带来更多产出

三件事叠加，让线性扩大搜索量几乎不产出：

1. **目标函数与闸门错配**（本轮 H1 已部分修复，但只加了惩罚项）：fitness 优化 `|RankIC|`，闸门判 `cost_adj_return`。`|RankIC|=0.10` 的因子可以在分位组合上净收益为负。
2. **换手 × 18bp 是硬淘汰**：`rev1` 在 1 bar 再平衡下 turnover 75%，成本 0.00135/bar，直接吃掉全部 gross。搜索空间里所有高频算子（`ts_delta`、`ts_momentum` 短窗）产出的候选在这一条上就死了。
3. **1d horizon=1 的标签**：`target.horizon_bars=1` + 1 bar 再平衡 = 每天全换仓。加密 1d 上，单日可预测性最弱、成本占比最高。`mom60` 在 5 bar 再平衡下 SR 是 1 bar 的 **3 倍**——这不是因子变好，是成本被摊薄。

**所以规模化的正确顺序是：先让单位候选值钱，再谈数量。**

---

## 5. 大规模挖掘方案（三层，按投入产出排序）

### L1 统计层 —— 先做，收益最大

**L1.1 分层漏斗（降本 30-50 倍）**

```
Stage A 粗筛（~42 ms/候选，无置换）
  IC + LS 净收益 + turnover 上限 + coverage
  → 保留 top 0.5%
Stage B 精算（~4.6 s/候选，n_perm=60）
  HAC t + 置换零分布 + DSR
  → 保留 top 1%
Stage C 全闸门（BH-FDR + holdout + walk-forward）
```

当前所有候选都跑完整评估（4.6 s），而 99% 的候选在 Stage A 就会被淘汰。仅此一项就把 10 万候选从 76 小时压到 ~3 小时。

**L1.2 自适应 n_perm**

- 粗筛 20 次、精算 60 次、最终确认 500 次
- `n_perm` 从 60 提到 500 只增加 8 倍成本，但 `empirical_p` 的分辨率从 0.017 提升到 0.002 —— 对 `empirical_p_max=0.05` 的判定精度是实质提升

**L1.3 trial family 分账（必须做，否则规模化的统计基础不成立）**

当前 `n_trials` 全局单例，意味着「挖了 5 个 universe」和「挖了 1 个 universe 5 倍深度」惩罚相同。正确做法是**在看数据之前声明 family**：

```json
"experiment": {
  "trial_family": "crypto_1d_mom_v1",     // 显式分区键
  "family_declared_at": "2026-11-15",
  "family_budget": 5000                    // 预注册上限
}
```

- 同一 family 内共享 `n_trials`
- 不同 family（不同 horizon / universe / 范式）独立计数
- **必须在跑之前写进配置并提交 git**，事后不能改——否则就是 p-hacking

这是从「挖矿"升级为"研究计划"的关键一步，也是唯一能让大规模搜索在统计上合法的做法。

**L1.4 目标函数对齐**

- `fitness` 已加 `tradeable_mult`（本轮），但应更进一步：直接以 `cost_adj_return / std` 为 fitness 主项，`|RankIC|` 降为约束
- 加 `turnover_max` 硬约束（如 0.15/bar），把高换手候选挡在 Stage A
- 把 `target.horizon_bars` 从 1 提到 **5**，`rebalance_every` 同步到 5

### L2 工程层

**L2.1 进程池评估**

`core/evaluation/evaluator.py:290-295` 把 `ThreadPoolExecutor` 换成 `ProcessPoolExecutor`，注意三点：

1. 候选对象与面板必须可 pickle（面板走共享内存或落地一次、子进程 mmap）
2. Windows `spawn` 下入口必须 `if __name__ == "__main__"`（`core/cli.py` 已满足）
3. 初始化用 `initializer=` 预加载 numpy/pandas，摊掉启动开销

预期：4 核 1.7x、16 核 3-4x（不是线性，但比 0.58x 好 3 倍）。

**L2.2 跨 run 分片（真正的吞吐杠杆）**

单机并行天花板低，正确做法是**按配置分片**：

```
pipeline --config base.json --sweep sweep.json --shard 3/8
```

`sweep.json` 声明维度（universe × horizon × 范式），每个分片是独立进程、独立 run 目录，共享 `n_trials` 账本（已有跨进程文件锁，`core/evaluation/experiment_trace.py:51-57`）。

**L2.3 面板与表达式缓存**

- 同一 batch 内，候选共享同一个 `data` 面板 → 只 pickle 一次
- 表达式 AST 级缓存：`ts_mean(close,20)` 在不同候选里重复出现，算子结果可 memo
- 因子值级去重已存在（`DiversityFilter`，`base.py:275-283`），但发生在评估**之后**；应提到评估前按 AST 归一化去重

**L2.4 候选级 resume**

当前 `--resume` 是 stage 级（重跑整个 mining 阶段）。改为：每批评估结果写 `evaluations.jsonl`（append-only），重启时跳过已评估的候选 hash。

### L3 数据层

**L3.1 universe 从 20 扩到 60-100（按成交额分层，不要无脑扩）**

实测：同一 `mom20` 因子，20 币净 SR 0.568 → 78 币 -0.016。**盲目扩池会稀释信号**，因为小币微观结构噪声大、成本高。

正确做法：
- 按 30 日美元成交额分 3 层（Top20 / 21-60 / 61-150）
- 每层一个独立 trial family
- 主战场放在 Top20-40（流动性与信号质量的平衡点）

**L3.2 把 funding 族接进搜索空间（最高性价比的新 alpha 源）**

758 个 symbol 的 8h funding rate 是现成的、未被使用的强信号：

```
funding_carry     = funding_rate.rolling(N).mean()          // carry 溢价
funding_zscore    = cs_zscore(ts_zscore(funding, 30))       // 拥挤度
funding_momentum  = funding.diff(N)                          // 情绪变化
oi_price_divergence = oi.pct_change(N) - price.pct_change(N) // 杠杆背离（OI 仅 20 symbol）
```

`crypto_streams` 已有加载器与 PIT 对齐（`merge_asof` 仅用历史值），接入成本主要是把 stream 面板并进 `RealDataClient.get_data()` 的返回结构。

**L3.3 多 horizon / 多 timeframe**

- horizon ∈ {1, 5, 20}，各自独立 family（这是合法的：预注册时声明）
- 1h 数据本地几乎没有，需要补下载（`refresh` 已支持任意 timeframe）

---

## 6. 最小可执行路线

### 阶段 A（1-2 天，不改架构，收益最大）

| 改动 | 位置 | 预期效果 |
|---|---|---|
| A1 把 `horizon_bars` 提到 5、`rebalance_every` 同步 5 | `configExperiment.json` | 单位信号质量 ×3（实测 mom60 证据） |
| A2 `turnover_max=0.15` 硬约束进 Stage A | `portfolio_eval` + `evaluator` | 砍掉所有必死的高换手候选 |
| A3 `n_perm` 分档（20/60/500） | `significance.py` + gate cfg | 成本 ↓40% |
| A4 补充 `funding_rate` 面板接入 | `real_client` + `crypto_streams` | 新 alpha 源（758 symbol） |

### 阶段 B（3-5 天，工程）

| 改动 | 位置 |
|---|---|
| B1 线程池 → 进程池 + 面板共享 | `evaluator.py:290-295` |
| B2 分层漏斗（Stage A/B/C） | `evaluator.py` 新增 `prefilter` |
| B3 候选级 resume（`evaluations.jsonl`） | `base.py::mine` |
| B4 `--sweep` + `--shard` | `core/commands/pipeline.py` |

### 阶段 C（1-2 周，统计基础设施）

| 改动 | 位置 |
|---|---|
| C1 trial family 分区（配置声明 + 分账） | `budget.py` + `significance.py` |
| C2 universe 分层（Top20/60/150） | `universe.py` |
| C3 监控：run 指标导出（Prometheus 或 jsonl 指标流） | 新增 `core/observability` |

---

## 7. 一句话总结

**当前 CLI 是一条严谨但慢的单机研究流水线——它能防止你自欺，但不能让你更快地找到 alpha。**

真正卡住产出的不是算力（门槛只按 log 增长、并行只差 3 倍），而是：
1. 单位候选的经济价值太低（horizon=1 + 1 bar 换仓 + 18bp = 结构性亏损）
2. 手里 758 个 symbol 的 funding 数据完全没用
3. `n_trials` 全局单一分母，让"多挖"变成自我惩罚而非自我增强

**先修 1 和 2，再谈规模化。** 在 `mom60` 这种朴素因子都能拿到 0.675 年化净 SR 的市场里，把搜索空间对准「低频、低换手、带 funding 信息」的区域，比把候选数从 100 推到 10 万更有价值。
