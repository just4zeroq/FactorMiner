# 现在该怎么做：几百币横截面组合多因子

> 2026-11-15 23:4x · 依据：本机真实数据只读实测（`user_workspace/reports/pool_cost_baseline.py`）
> + 源码级核查 + 前三轮评估（`cross_section_roadmap.md` / `crypto_factor_stack_survey.md`）
> 本轮**未改动任何生产代码**；新增：本文件、基线脚本、基线结果、研究计划草稿。

---

## 0. 一句话

**停止选框架。按「定死口径 → 修四项 P0 → 预注册 trial family → 分层开跑」走；今天不要跑 `experiment`。**

不是因为系统不能跑（`doctor` 对 439 币配置已是 0 FATAL / 2 WARN），而是因为
`n_trials` 是**全局单调累积**（`core/experiment/budget.py:9-13`）：现在每跑一次口径未定的
实验，都会永久抬高后续所有实验的 DSR 门槛，且结论因池定义而不可复现。**先修口径是在省统计预算，不是在拖延。**

---

## 1. 三个待拍板项：替你定了

| 问题 | 裁决 | 理由 | 何时重审 |
|---|---|---|---|
| ① PIT 数据：采购 Kaiko / Coin Metrics 还是自建退市表 | **不采购，先自建** | 本轮实测「池定义」一项就能让朴素动量的净 SR 变动 **0.15–0.91**（§2-B），比成本假设的影响更大；在连池都没定死时花 ~$1k/月买数据是本末倒置。现有 755 个挂牌快照带 `onboard_ms`，先用它 + 交易所公告补一张退市表，并把「动态滚动 top-K」作为主口径 | 出现净 SR>1 且过闸门的候选、准备上实盘时 |
| ② 组合层：接 skfolio 还是自研扩容 AlphaPool | **先自研扩容 `AlphaPool`** | 本机未装 `cvxpy`；skfolio 的优化器假设是「资产收益预测 → 权重」，与当前「因子表达式 → RankIC → 闸门」链路不同构。它真正值钱的 `SelectCompleteAssets` / `SelectNonExpiringAssets` 语义，用几十行自研即可落地，不必引入依赖 | 需要带约束权重优化 / CP-CV 时，再把它当可选后端 |
| ③ vectorbt 的 Commons Clause | **暂不引入，搁置** | 98% 评估耗时在置换零分布（4.6s 中 4.599s），分层漏斗用纯 numpy 就能降 30–50 倍，没必要为批计算碰非 OSI 许可 | 漏斗做完仍不够快，或明确要商业化时做法律确认 |

**另：研究口径问题一并裁决** —— 接受「当前存活合约 + 上市后才有数据」作为**探索口径**，
但任何标为「可交易」的结论必须补退市表；池定义必须写进 run manifest 并作为 trial family 键。
只做研究、不上实盘时，这个口径可用；它不能用来宣称策略可交易。

---

## 2. 本轮实跑：三个数字改变了优先级

脚本：`venv/Scripts/python.exe user_workspace/reports/pool_cost_baseline.py`
结果：`user_workspace/reports/pool_cost_baseline.json`（84 行，窗口 2023-04-01..2026-06-30，439 币）

口径刻意与生产对齐：分位 0.2 多空、`√365` 年化、`f[t]=close[t]/close[t-k]-1`、
`r[t]=close[t+1]/close[t]-1`；**uniform 分支逐行与 `core/evaluation/portfolio_eval.py:88
long_short_returns` 断言一致**（`n_obs` 与 `net_mean_return`）。
PIT 池按每根 bar 的 trailing-90-bar 成交额中位数重排；静态池按 2026-08..11 成交额排名，
明确标记为**前视**、仅用于复现历史对比。成本分层是 square-root impact 的**情景假设**
（15bp / 50bp per-side 上限两档），不是实测价差 —— 只有方向和相对幅度可解释。

### 2.1 扩池稀释是真的，成本分层让它更糟（推翻上一轮猜测）

`mom20`、5 bar 再平衡、PIT 池：

| 池 | 截面宽度 | 统一 18bp | 分层 mild | 分层 harsh | 成本/bar |
|---|---:|---:|---:|---:|---:|
| `pit_top20` | 20 | -0.098 | -0.109 | -0.109 | 1.48e-4 → 1.60e-4 |
| `pit_top60` | 60 | **0.743** | **0.669** | **0.668** | 1.42e-4 → 2.07e-4 |
| `pit_top150` | 150 | 0.438 | 0.260 | 0.159 | 1.39e-4 → 3.19e-4 |
| `pit_all` | 226 | 0.289 | 0.056 | **-0.258** | 1.39e-4 → **4.34e-4** |

静态全池 439（`mom20`/`mom60`，rb=5）：统一 **0.255 / 0.311** → mild **0.029 / 0.174**
→ harsh **-0.278 / -0.015**。

> **更正**：上一轮我写「统一 18bp 至少部分解释『20 币 SR 0.568 → 78 币 -0.016』，
> 不能简单归为稀释」—— 方向反了。分层成本把大池的成本/bar 抬高 **212%**，
> 把 439 池从勉强正打到负。**成本分层不是救大池的药，是证明大池更不该做的证据。**

### 2.2 池定义的影响比成本假设更大（因此 P0 顺序要改）

静态（前视）池 vs PIT 动态池，统一成本下的净 SR 差：

| 因子 / rb | top20 static → PIT | top60 static → PIT |
|---|---:|---:|
| mom20 / 1 | 1.021 → 0.158（**-0.864**） | 1.255 → 0.838（-0.417） |
| mom20 / 5 | 0.808 → -0.098（**-0.907**） | 1.208 → 0.743（-0.465） |
| mom60 / 1 | 0.609 → 1.455（**+0.847**） | 0.788 → 1.075（+0.287） |
| mom60 / 5 | 0.728 → 1.009（+0.280） | 0.690 → 0.834（+0.145） |

差异 **0.145–0.907**，且**方向随因子翻转**（mom20 静态高估、mom60 静态低估）。
这不是「静态池一定高估」，而是：池定义本身就是一个能翻转结论的自由度 ——
不定死并预注册它，事后选池就是 p-hacking。另外静态 top20 的截面宽度中位数只有 **17**
（末期头部币在 2023 多未上市），PIT top20 稳定为 20，这也是前视的一部分。

> **优先级更正**：上一轮建议「先从成本分层 A2 单项开始」，应改为 **A1 池定义先行**。
> 池定义造成的 SR 摆动（最大 0.91）远大于成本分层（最大 0.55）。

### 2.3 主战场是 PIT 动态 top60 一带，不是 439 全池

`pit_top60` 在三种成本情景下都是最稳的正值（mom20 rb=5：0.743/0.669/0.668；
mom60 rb=5：0.834/0.784/0.784），`pit_top20` 反而因分位组合太窄而不稳（mom20 rb=5 为负）。

**这不否定「几百币横截面组合多因子」这个目标**，只是否定「等权分位全池」这种做法：
正确形态是**用几百币做 universe 覆盖与中性化，把权重约束在流动性足够的 60–150 层**，
每层独立 trial family。这也正是 `readiness` 报告 §L3.1 分层建议的方向。

> **附带更正**：`readiness` 报告「mom60 5-bar 再平衡 SR 是 1-bar 的 3 倍」在 PIT 动态池下
> **不成立**（`pit_top60` harsh：rb1 0.967 vs rb5 0.784）。该结论来自固定 20 币池，
> 不能无条件照搬到 `horizon_bars=5 / rebalance_every=5`。调参前需按新池重测。

---

## 3. 执行顺序

### 阶段 0｜今天：预注册，不开跑

| 动作 | 产物 / 命令 |
|---|---|
| 写死研究计划（trial family、池定义、成本口径、验收门槛），提交 git 后不改 | `user_workspace/reports/research_plan_cs_mom_v1.json`（本轮已生成草稿） |
| 复现本轮证据 | `venv/Scripts/python.exe user_workspace/reports/pool_cost_baseline.py` |
| 确认体检基线 | `venv/Scripts/python.exe -m core.cli doctor --config user_workspace/configs/configAllUSDTFutures.json --json` |

**不要执行** `experiment` / `pipeline`（会写入全局 `n_trials`）。

### 阶段 1｜P0 四项，按新顺序（每项单独提交 + 跑 `pytest`）

| # | 项 | 位置 | 验收条件 |
|---|---|---|---|
| 1 | **A1 池定义 PIT 化** | `scripts/build_universe_config.py`、`core/experiment/universe.py`、`doctor.py` | 生成按 bar 可用的存续/流动性池；池 id + 参数写进 manifest；`doctor` 新增检查「PIT 池已声明」；退市表缺失时明确标 `exploratory_only` |
| 2 | **A2 成本分层** | `core/experiment/costs.py`、`backtest_engine.py`、`doctor.py` | ≥3 档按成交额；`gate_cost_rate` 由持仓权重动态推导，恒等式 `gate == 2*(commission+slippage)` 在分层下仍成立；本脚本 harsh 情景能被生产配置复现 |
| 3 | **A3 覆盖约束** | `core/data_feed/panel_validator.py:176` | 低覆盖可配置 critical；改为「逐资产 coverage 下限」而非整面板平均 |
| 4 | **A4 N(t) 感知** | `core/miner/operator_runtime.py`、`portfolio_eval.py` | 有效 N < 阈值时跳过截面算子；RankIC 与组合收益按 N 分层报告 |

**重跑基线**：四项做完后，用同一脚本在 PIT 口径下复算，`pit_top60` mom60 的净 SR 应仍 > 0。

### 阶段 2｜第一次 `experiment` 之前必须完成

- **trial family 分账**（`core/experiment/budget.py` + `significance.py`）：不同 universe / tier /
  horizon 独立计 `n_trials`，配置里 `trial_family` + `family_budget` **预注册后不可改**。
  这是让大规模搜索在统计上合法的唯一办法，也是本阶段唯一硬阻塞。
- 分层漏斗 Stage A/B/C（42ms 粗筛 → 4.6s 精算）、`evaluator.py:290` 线程池 → 进程池。
- 开跑命令：`venv/Scripts/python.exe -m core.cli experiment --config ... --preflight`
  （FATAL 即 exit 1 且不建 run 目录），随后 `factors --run-id ...` 看闸门拒绝原因。

### 阶段 3｜让「多因子组合」真正成立

- `crypto_streams`：758 个 symbol 的 funding/OI/basis 接进搜索空间（当前 `doctor` WARN：
  挖矿只看 OHLCV）。
- `AlphaPool`：`capacity` / `min_obs` 参数化、加 `to_dict` 持久化与成员 lineage 落盘、
  因子暴露矩阵 + 截面中性化 + 流动性权重约束。
- 可选：在 `core/miner/paradigms/` 加截面模型范式（LightGBM / 岭回归 + 中性化），
  输出仍走 `apply_promotion_gates`。

### 阶段 4｜才轮到外部件

skfolio 约束优化 / Qlib 离线 Alpha158 对照 / 采购机构 PIT 数据。
**任何外部件的输出都必须回到 `apply_promotion_gates`，绝不让 IC / Sharpe 的外部反馈当晋升判据。**

---

## 4. 「可以开始挖」的验收门槛

1. `doctor` 0 FATAL，且三项新检查通过：PIT 池已声明、成本分层已生效、trial family 已预注册。
2. 研究计划文件已提交 git，未事后修改。
3. 本基线脚本在 PIT 口径下重跑，`pit_top60` mom60 净 SR > 0（口径变更没有把基线打没）。
4. 全量 `pytest` 通过（当前基线 697 passed）。
5. 首次实验用 `--preflight`，`family_budget` 已写入配置。

---

## 5. 本轮改动清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/reviews/next_actions.md` | 新增 | 本文件 |
| `user_workspace/reports/pool_cost_baseline.py` | 新增 | 只读诊断脚本，与生产 `long_short_returns` 断言对齐 |
| `user_workspace/reports/pool_cost_baseline.json` | 新增 | 84 行实测结果 + 全部口径与 caveat |
| `user_workspace/reports/research_plan_cs_mom_v1.json` | 新增 | 研究计划草稿（`DRAFT_NOT_EXECUTED`，CLI 不读取） |

**生产代码：零改动。** 阶段 1 的四项需要你一句确认再动手。
