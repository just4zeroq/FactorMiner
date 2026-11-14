# 业界因子挖掘框架源码层调研报告

> **调研日期**：2026-11-14
> **调研对象**：18 个开源仓库，源码已全部下载至 `D:\code\github\`
> **分析方式**：逐仓库 `Read` / `Grep` / `Glob` 实读源码，所有关键结论附 `路径:行号`；README 声明与源码实现不符处显式标注
> **对照基线**：`D:\code\github\FactorMiner`（本项目）
> **触发背景**：刚完成 6 个 INSPECTED 因子的**真样本外验证**，严格 OOS 结果 `total_return -11.18% / Sharpe -1.14`，确认因子为过拟合噪声。本报告意在回答：业界的算子体系、挖掘算法、以及**防过拟合工程闸门**分别做到什么程度，有哪些值得我们移植。

---

## 0. 执行摘要（先看这一页）

### 0.1 一句话结论

**我们在"算子丰富度"和"范式数量"上已经不落后（59 个内置算子、GP/RL/LLM/NN 四范式、OOS-aware fitness、末层岭回归组合、事件驱动回测），但缺了业界最成熟的那层东西：统计显著性闸门与可复现的留出纪律。** 这恰好解释了为什么 6 个因子样本内 IC 显著、样本外全亏——我们有一套"打分"，但没有一套"证伪"机制。

### 0.2 五个最重要的发现

| # | 发现 | 证据 |
|---|---|---|
| 1 | **多重检验校正是分水岭**：18 个仓库里只有 `LLM-Alpha-Mining-Framework` 与 `crypto-factor-lab` 两家把 FDR / Holm / Romano-Wolf / Deflated Sharpe / 随机公式零分布真正写进了代码。多数 LLM 挖因子项目（含微软 RD-Agent）**没有任何多重检验校正** | `LLM-Alpha-Mining-Framework/factors/multiple_testing.py:66-306`、`crypto-factor-lab/factor_lab/mining.py:378-394` |
| 2 | **严格 OOS 必须"一次性揭示"**：`crypto-factor-lab` 用账本记录每个 dataset 已揭示过的 holdout 区间，重叠即抛异常，强制"想再看一次就得补没见过的数据"。我们的严格 OOS 已被使用过一次，若继续据此调参就不再是 OOS | `crypto-factor-lab/factor_lab/catalog.py:282-299`、`service.py:900-906`（本次已亲自复核） |
| 3 | **组合层差距最大**：我们的末层是「样本内一次岭回归、之后固定」；AlphaGen 的 AlphaPool 是「容量池 + mutual-IC 去冗余 + 每加入一个因子重优化全部权重 + 直接优化组合 IC/ICIR + L1 稀疏」，AlphaForge 更进一步「每期重选 top-N 因子并 walk-forward 重估权重」 | `AlphaGen/alphagen/models/linear_alpha_pool.py:61-104,285-404`、`AlphaForge/combine_AFF.py:160-243` |
| 4 | **RL 挖因子的关键不是 PPO，是 masking**：AlphaGen 用栈式 RPN + 分类 `validate_op` 生成动作掩码，保证采样的每个表达式语法/类型 100% 合法；AlphaForge 把掩码做成可微模块（`-1e8` + Gumbel-Softmax），让梯度能穿过合法性约束 | `AlphaGen/alphagen/data/tree.py:42-88`、`rl/env/wrapper.py:60-83`、`AlphaForge/gan/network/masker.py:21-57` |
| 5 | **qlib 不是挖掘器，是执行/数据平台**；但它有三个我们缺的底层契约：算子声明历史数据依赖（`get_longest_back_rolling` / `get_extended_window_size`）、以表达式字符串为 key 的子表达式级缓存、AST 白名单安全解析 | `qlib/qlib/data/base.py:209-235,184-203`、`ops.py:764-778`、`expression_parser.py:50-124` |

### 0.3 借鉴优先级速览

| 优先级 | 建议 | 主要来源 | 预计落点 |
|---|---|---|---|
| **P0** | 一次性留出揭示账本（holdout ledger） | `crypto-factor-lab` | `core/evaluation/` + 报告层 |
| **P0** | 多重检验校正：记录 `num_trials`、BH-FDR、随机公式零分布、Deflated Sharpe | `LLM-Alpha-Mining-Framework` | `core/evaluation/evaluator.py` 新增闸门层 |
| **P0** | purge + embargo 时间隔离，roll-forward 多段而非单次切分 | `crypto-factor-lab` / `Alpha-Research-Discovery` | `core/data_feed/real_client.py` + evaluator |
| **P0** | 组合层升级：容量池 + mutual-IC 去冗余 + 组合 IC 目标 + 逐期权重 | `AlphaGen` / `AlphaForge` | 替换 `core/miner/expressions.py` 的固定岭回归 |
| **P1** | RL 动作掩码 + PPO；搜索 reward 改为「加入池后的组合增量」 | `AlphaGen` | `core/miner/paradigms/rl_miner.py` |
| **P1** | LLM 范式：结构化假设→因子规格两段式 + 执行反馈闭环 + SOTA 库相关去重 | `RD-Agent` | `core/miner/paradigms/llm_miner.py` |
| **P1** | 子表达式 DAG 缓存 + `sliding_window_view` 向量化 | `qlib` / `gpquant` / `KunQuant-rs` | `core/miner/operator_runtime.py` |
| **P2** | 加密特有：funding/OI/basis/liquidations 作为一等字段 + point-in-time 资产池 | 4 个加密仓库 | `core/data_feed/` |
| **P2** | 补条件三元 / 通用二元幂 / `vwap,adv,returns,amount` 终端 | `Alpha101` / `gpquant` | `operator_runtime.py` + `gp_miner.py` |

> ⚠️ **不建议先做的**：再扩算子、再堆范式、以及用户此前提到的「逐时间截面回归（时变权重）」。在统计闸门补齐前，任何提升搜索效率/表达力的改动都只会把"重复采样"放大成"更高效的重复采样"。

---

## 1. 调研范围与方法

### 1.1 框架全景图

按**在因子研究流水线中的位置**分类，而不是按 star 数：

```
┌─────────────────────────────────────────────────────────────────────┐
│ L0 数据 / 表达式执行层                                                │
│   qlib(52 算子名/47 算子类) · KunQuant-rs(编译执行) · alphalens(PIT对齐)│
├─────────────────────────────────────────────────────────────────────┤
│ L1 公式化因子挖掘器（搜索算法）                                       │
│   gplearn · gpquant · AlphaGen(PPO+AlphaPool) · AlphaForge(GAN+动态组合)│
│   Alpha101(人工模板) · AlphaForge-Crypto(组合搜索) · crypto-factor-lab  │
├─────────────────────────────────────────────────────────────────────┤
│ L2 LLM / Agent 驱动挖掘                                               │
│   RD-Agent(微软) · EvoAlpha · CosmosAlpha · LLM-Alpha-Mining-Framework │
│   WorldQuant-LLM · alphaminingv2                                      │
├─────────────────────────────────────────────────────────────────────┤
│ L3 加密货币专用                                                       │
│   crypto-factor-lab · AlphaForge-Crypto · alpha-factor-mining-crypto   │
│   Alpha-Research-Discovery                                            │
├─────────────────────────────────────────────────────────────────────┤
│ L4 评价 / 统计闸门（我们最缺的一层）                                  │
│   crypto-factor-lab ★★★ · LLM-Alpha-Mining-Framework ★★★              │
│   AlphaForge-Crypto ★ · 其余 ≈ 0                                       │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 源码清单（全部位于 `D:\code\github\`）

| 目录 | 上游仓库 | 版本 / commit | 文件数 | 定位 |
|---|---|---|---|---|
| `qlib` | microsoft/qlib | `5435523` | 673 | 量化平台：算子/数据/模型/回测 |
| `AlphaGen` | RL-MLDM/AlphaGen | `259687e` | 134 | KDD'23，PPO 挖公式因子 + AlphaPool |
| `AlphaForge` | DulyHao/AlphaForge | `d0cfc27` | 128 | AAAI'25，GAN 生成-预测 + 动态组合 |
| `RD-Agent` | microsoft/RD-Agent | `484776c` | 950 | 微软 LLM 自动因子研究 v1.0.0 |
| `gplearn` | trevorstephens/gplearn | `0390aea` | 82 | 通用符号回归 GP 范本 |
| `gpquant` | UePG-21/gpquant | tarball | 13 | 符号回归挖技术因子（60 算子） |
| `alphalens` | quantopian/alphalens | `77084f1` | 74 | 单因子描述性评价 |
| `WorldQuant_alpha101_code` | yli188 | `3bb9918` | 32 | Alpha101 公式与算子语义 |
| `KunQuant-rs` | 本地已有 | v0.3.0 | — | 高性能表达式编译执行 FFI |
| `AlphaForge-Crypto` | warren618/AlphaForge | tarball | 25 | 加密永续端到端因子系统 |
| `crypto-factor-lab` | ZMGgGG | tarball | 66 | **防过拟合闸门最系统** |
| `alpha-factor-mining-crypto` | tianchen95hk | tarball | 20 | Binance 永续横截面因子 |
| `Alpha-Research-Discovery` | VedantUpasani46 | tarball | 43 | 30 个 alpha + DSR/walk-forward |
| `EvoAlpha` | AAAAndrews | tarball | 24 | 多智能体 LLM 进化（骨架） |
| `CosmosAlpha` | wzhmtldswz | tarball | 132 | MCTS / Graph Flow / Pareto 搜索 |
| `LLM-Alpha-Mining-Framework` | Aroesler1 | tarball | 697 | **统计闸门最完整 + 诚实审计** |
| `WorldQuant-LLM` | ChenNachuan | tarball | 29 | WQ Brain 在线提交闭环 |
| `alphaminingv2` | hsrxr | tarball | 80 | WQ Brain Direct Agent |
| `trade-learn` | MuuYesen/trade-learn | v0.3.x (tarball) | — | 回测/研究平台（**非挖掘器**，下游互补；含 KunQuant 后端） |

> **下载方式说明**：`git clone` 走本机代理（`127.0.0.1:59047`）时 `github.com` 的 CONNECT 隧道会间歇 502；前 8 个仓库用 `git clone --depth 1` 成功，后 10 个改用 GitHub API tarball（`https://api.github.com/repos/{owner}/{repo}/tarball`）下载解压，源码完整但**不含 `.git`**，故标为 tarball 而非 commit。

> **后续增量（2026-11-14 当晚）**：新增第 19 个竞品 `AlphaMaster`（MT5 因子挖掘 + 回测 + Web 训练控制台，`main` @ `3727daf2d2cb`，tarball 下载至 `D:\code\github\AlphaMaster`），因定位与本文 18 个框架差异较大，单独成篇 → **[AlphaMaster 竞品源码层研究报告（增量）](alphamaster_competitor_review.md)**。该报告沿用本文口径，新增 L5 产品化/交易终端层、RL 动作掩码的第二个独立实现、以及一条「验证集每步复用 → 无 `num_trials` 折减 → 因子退化为 beta」的实证病因链；本文 §6 矩阵的补充行与 §8 P0/P1 清单的印证/修正均列于该报告 §3、§5。**⚠️ 本文 §0 / §7 / §8 记录的是 2026-11-14 调研当时的基线；同日 10:22–11:29，P0-1~P0-3、P0-5（统计闸门、holdout ledger、静态 AlphaPool、加密原生特征）已落地并接入主流程，故「我们没有任何统计闸门」的表述对当前代码已过时。** 校准后的完整对照见增量报告 §3.3 / §4；剩余待办为 P0-4（purge/embargo、多段 roll-forward）与 P0.5 正向验证。

> **交叉评估（2026-11-14 16:1x 补记）**：新增 **[框架横向评估：成熟度 × 加密货币适配度](framework_maturity_crypto_fit.md)**，对 20 个外部框架 + FactorMiner 做双维度评分。该报告带来两处需收窄的口径：① 本文 §0.2 发现 2 与 §4.1.2 第 16 项把 `crypto-factor-lab` 的 holdout ledger 描述为全链路强制，实测其 `latest_revealed_holdout_end` 仅被 `factor_lab/service.py:510,900-905` 调用，**CLI 主入口 `scripts/run_factor_mining.py` 不调用也不写 `holdout_end`**；机制设计仍值得借鉴，且我们 `core/miner/director.py` 的接入比它更彻底。② qlib 虽有 `scripts/data_collector/crypto/collector.py`，但只有 CoinGecko 日频价格/量/市值、无 OHLC，`crypto/README.md:12` 明示不支持回测，不应计为加密适配。另发现我们评价/显著性/组合层默认 `annualisation=252`（股票口径），与加密 7×24 不符，已列为该报告 P0-a。

### 1.3 口径与可信度约定

- 文中「算子数」按**可调用算子名**统计，不同仓库口径不同，已逐处说明（如 qlib 注册 52 名 / 定义 47 类；FactorMiner `BUILTIN_OPERATORS` 实测 **59** 个）。
- 所有 `路径:行号` 相对各仓库根目录；`D:\code\github\` 为公共前缀。
- 源码中未找到的能力一律写「未找到」，不以论文或 README 宣称代替实现。
- 部分新仓库存在「README 宏大、源码骨架」问题，第 5 章有专门的成熟度审查。

---

## 2. 框架源码层详解（一）：执行平台与公式化挖掘器

### 2.1 qlib —— 算子/数据/执行的事实标准

#### 2.1.1 核心抽象：五层表达式体系

`qlib/data/base.py:13` 的 `Expression(ABC)` 是根，通过运算符重载 `__add__/__sub__/__mul__/__truediv__/__gt__`（`:32-140`）直接构造表达式树；`load()`（`:142-203`）是模板方法，统一处理缓存与错误包装，子类只需实现 `_load_internal()`（`:205`）。

两个抽象方法构成 qlib 最重要的设计契约：

- `get_longest_back_rolling()`（`:209`）
- `get_extended_window_size()`（`:222`）

**每个算子自己声明"我需要往前多取多少根 K 线"**。`LocalExpressionProvider.expression()`（`data.py:845-881`）据此把查询区间扩成 `[start-lft, end+rght]`（`:855-856`），算完再切回目标区间（`:880`）。这从架构上杜绝了"在已切片的 DataFrame 上直接 rolling 导致边界 NaN / 隐式前视"。

继承体系（`qlib/data/ops.py`）：
`ElemOperator:37`（单目）→ `NpElemOperator:97`；`PairOperator:231`（双目）→ `NpPairOperator:279`；`Rolling:713`（时序窗口）；`PairRolling:1387`（双序列窗口）。

Provider 分层在 `data.py`：`FeatureProvider:306` / `ExpressionProvider:382` / `DatasetProvider:448`；`DataHandler`（`dataset/handler.py:25-39`）负责 loader → 内部表示 → 预处理；`DatasetH`（`dataset/__init__.py:72`）按 `train/valid/test` 切片（`:185-247`）。

**三层缓存**（值得单独强调）：
1. 表达式对象缓存 `expression_instance_cache`（`data.py:389,391-397`）
2. 内存结果缓存：`Expression.load()` 以 `(str(self), instrument, start, end, ...)` 为 key（`base.py:187`），`H = MemCache()`（`cache.py:1198`）——**递归表达式串天然实现公共子式复用**
3. 磁盘缓存 `ExpressionCache` / `DiskExpressionCache`（`cache.py:329,489`），`disk_cache=0/1/2` = skip/use/replace（`data.py:1177-1179`）

#### 2.1.2 算子清单（52 个注册名 / 47 个具体算子类）

`ops.py` 共 54 个 `class`（6 个中间基类 + 47 具体类 + `OpsWrapper` 注册器 `:1619`）；`OpsList:1566-1616` 注册 50 项，`register_all_ops():1675-1686` 追加 PIT 的 `P/PRef`，最终 **52 个可调用名**。

| 类别 | 算子 | 语义 / 参数 |
|---|---|---|
| 算术双目 | `Power` `Add` `Sub` `Mul` `Div`（`:338-418`） | 幂/加/减/乘/除；操作数可为 Expression 或数值常量 |
| 极值 / 比较 | `Greater` `Less` `:438,458`；`Gt` `Ge` `Lt` `Le` `Eq` `Ne` `:478-578` | `max/min` 逐元素；六个比较返回 bool |
| 逻辑 / 条件 | `And` `Or` `Not` `If`（`:598-671`） | 位运算；`If(cond,a,b)` = `np.where` |
| 逐点数学 | `Abs` `Sign` `Log`（`:122-167`） | 三个 |
| 换标的 | `ChangeInstrument` `Mask`（`:64,185`） | 切到指数等参考标的算 beta，忽略原 instrument |
| 时序索引 | `Ref(feature,N)` `:781` | `shift(N)`；`N<0` 取未来、`N=0` 取首日 |
| 时序统计 | `Mean` `Sum` `Std` `Var` `Skew` `Kurt` `Med` `Mad` `Rank` `Count` `Delta`（`:827-1191`） | 11 个，`(feature,N)`；`Skew` 要求 N≥3、`Kurt` N≥4 |
| 时序极值位置 | `Max` `Min` `IdxMax` `IdxMin`（`:951-1044`） | 最大/最小/距最高点天数/距最低点天数 |
| 时序分位 | `Quantile(feature,N,qscore)` `:1047` | 滚动分位 |
| 滚动回归 | `Slope` `Rsquare` `Resi`（`:1221-1286`） | 对 `[1..N]` 回归的斜率 / R² / 残差，Cython 加速 |
| 加权 | `WMA` `EMA`（`:1314,1349`） | 线性加权 / 指数加权；`EMA` 的 N 可为 int(span) 或 float(alpha) |
| 双序列滚动 | `Corr` `Cov`（`:1467,1501`） | 滚动相关/协方差 |
| 时间重采样 | `TResample(feature,freq,func)` `:1528` | 按频率 resample |
| 字段 / PIT | `Feature` `PFeature` `P` `PRef` | `$close` / `$$roewa_q` / PIT 转日频 |

**参数化窗口统一语义**（`ops.py:742-778`）：`N>0` → `rolling(N, min_periods=1)`；`N==0` → `expanding()`；`0<N<1` → `ewm(alpha=N)`。这一条比我们「每个算子各自 `window` 参数」更统一。

> **关键事实：qlib 的 `ops.py` 没有任何截面算子**。截面能力放在 `dataset/processor.py` 的 `CSZScoreNorm:300-323` 与 `CSRankNorm:326-357`（`groupby("datetime")`），且默认对 label 做截面 z-score（`contrib/data/handler.py:37-40`）。这是「时序表达式层 / 截面后处理层」分层的刻意设计。

#### 2.1.3 Alpha158 / Alpha360：声明式模板而非搜索产物

定义真身在 `qlib/contrib/data/loader.py`（`handler.py:48,98` 只是 DataHandlerLP 子类）：

- **Alpha360**（`loader.py:4-58`）：6 组字段 `CLOSE/OPEN/HIGH/LOW/VWAP/VOLUME` × 历史位移 `1..59` + 当期，全部除以最新 `$close`（成交量除以 `$volume+1e-12`），使 `CLOSE0` 恒为 1 → **60 × 6 = 360**。
- **Alpha158**（`loader.py:61-310`）：`kbar` 9 个 K 线形态（`KMID/KLEN/KUP/KSFT...` `:104-126`）+ `price` 4 个（`:127-133`）+ `rolling` 29 组派生 × 5 窗口 `[5,10,20,30,60]`（默认 `:139`） = **9 + 4 + 145 = 158**。29 组含 `ROC/MA/STD/BETA/RSQR/RESI/MAX/MIN/QTLU/QTLD/RANK/RSV/IMAX/IMIN/IMXD/CORR/CORD/CNTP/CNTN/CNTD/SUMP/SUMN/SUMD/VMA/VSTD/WVMA/VSUMP/VSUMN/VSUMD`。
- 标签 `Ref($close,-2)/Ref($close,-1)-1`（`handler.py:89-90`），即 T+1→T+2 收益。
- `get_feature_config(config)`（`:72-82`）接受 `kbar/price/volume/rolling` 四组与 `windows/include/exclude`——**可声明式消融**。

#### 2.1.4 表达式解析：AST 白名单（安全）

`parse_field()`（`qlib/utils/__init__.py:291-325`）把 `$x` → `Feature("x")`、`Foo(` → `Operators.Foo(`；随后 `parse_expression()`（`qlib/data/expression_parser.py:307-323`）用 `ast.parse(mode="eval")` + `_validate()` 白名单（`:50-75,97-124`，仅允许 `Operators.<name>(...)`、禁私有名、容器 4096 上限）构造树，docstring 明确 "without executing arbitrary Python code"。**当前 commit 未找到 `eval` 执行路径**。自定义算子需在文件顶层写 `"trusted": True`（`ops.py:1645`）。

#### 2.1.5 定位澄清与评价口径

**qlib 本体不含自动因子挖掘器**：全目录检索 `gplearn|genetic programming|factor mining|symbolic regression|deap|alphagen` 零命中。README 把自动挖掘明确外包给 RD-Agent（`README.md:35-39,45-50,67`），**未找到代码级集成 hook**。

评价：`calc_ic()`（`contrib/eva/alpha.py:160-183`）按 datetime 分组算 Pearson(IC) / Spearman(RankIC)；`SigAnaRecord` 汇总 **IC / ICIR / Rank IC / Rank ICIR**（`workflow/record_temp.py:270-275`）；`risk_analysis()`（`contrib/evaluate.py:26-93`）区分 `excess_return_without_cost` 与 `with_cost`（`report/analysis_position/risk_analysis.py:34-38`）；成本默认 `open_cost=0.0015, close_cost=0.0025`（`backtest/exchange.py:44-50`）。滚动重训有 `contrib/rolling/base.py:Rolling(step=20):58`。

**未找到**：OOS-aware fitness、简约惩罚、GP/RL/LLM 搜索循环（这些都在外部仓库）。

#### 2.1.6 对 FactorMiner 的可借鉴点

1. **【高】算子声明数据依赖**：给 59 个算子加 `lft_etd/rght_etd` 契约，数据源统一扩展取数。我们重算 OOS 因子时靠手写 `WARMUP_START="2024-07-01"`（`recompute_oos_factors.py:30`），这是**隐式魔法数**；改契约后可自动推导 warm-up，且杜绝边界 NaN。（`base.py:209-235`、`ops.py:764-778`、`data.py:855-880`）
2. **【高】子表达式级 DAG 缓存**：以规范化 AST 字符串为 key 缓存结果，GP 种群里共享子树只算一次。我们 `evaluate_ast`（`operator_runtime.py:594-629`）是纯递归解释、每个候选全量重算。（`base.py:187,202`）
3. **【中】AST 白名单解析**：LLM 产物若走代码执行，可移植 `expression_parser.py:50-124`。（我们目前用 `RestrictedSandbox`，路线不同但可对照）
4. **【中】截面/时序分层**：把截面标准化从算子层剥离为可插拔后处理，保证 IS/OOS 口径一致。（`processor.py:300-357`）
5. **【中】声明式模板 + 消融**：把因子集做成 `字段 × 算子 × 窗口` 配置，用于**过拟合归因**——逐组关闭某类窗口，看哪类在 OOS 上退化最严重。（`loader.py:72-310`）
6. **【中】多段滚动验证 + ICIR 稳定性**：`DatasetH.prepare()` + `Rolling(step=20)`；我们目前是单次 OOS 切分，有"挑到一段好 OOS"的选择偏差。并把 ICIR 与含/不含成本收益纳入 fitness。（`dataset/__init__.py:185-247`、`record_temp.py:272-275`）

---

### 2.2 AlphaGen —— RL 挖「协同」因子集合（KDD 2023）

#### 2.2.1 表达式与算子体系

`alphagen/data/tokens.py:12-60` 定义 Token：`ConstantToken` / `DeltaTimeToken`（窗口）/ `FeatureToken`（6 个字段 `$open...$vwap`，`alphagen_qlib/stock_data.py:8-14`）/ `OperatorToken` / `SequenceIndicatorToken(BEG,SEP)`。

`alphagen/data/expression.py` 的类层次：`Expression:18`（运算符重载构树）→ `Feature:43` / `Constant:62` / `DeltaTime:83`；`Operator:108` → `UnaryOperator:162`(arity 1) / `BinaryOperator:189`(2) / `RollingOperator:219`(2，第二操作数为窗口) / `PairRollingOperator:258`(3)。`validate_parameters`（`:121-152`）校验 arity、`is_featured`（至少一个操作数含特征）、`DeltaTime` 类型。

**28 个算子类**（`expression.py:487-497`）：

| 类别 | 算子 | 数量 |
|---|---|---|
| Unary | `Abs` `Sign` `Log` `CSRank` | 4（唯一截面算子是 `CSRank`） |
| Binary | `Add` `Sub` `Mul` `Div` `Pow` `Greater` `Less` | 7 |
| Rolling（带窗口） | `Ref` `Mean` `Sum` `Std` `Var` `Skew` `Kurt` `Max` `Min` `Med` `Mad` `Rank` `Delta` `WMA` `EMA` | 15 |
| PairRolling | `Cov` `Corr` | 2 |

RL 实际启用 **22 个**（`alphagen/config.py:8-21`，注释掉 `Sign/CSRank/Pow/Skew/Kurt/Rank`）；窗口 `DELTA_TIMES=[1,5,10,20,40]`（`:23`）、常量 14 个（`:25`）、`MAX_EXPR_LENGTH=15`（`:5`）。另 `alphagen_generic/operators.py:9-61` 是给 gplearn baseline 用的 **78 个字符串包装**（只是拼 `Mean(x,20)` 文本，不参与 RL 求值）。

求值全部走 `torch.Tensor`：`RollingOperator.evaluate` 用 `Tensor.unfold(0, delta_time, 1)` 得到 `(L,S,W)` 后向量化 `mean/std/max/...`（`:238-246`），数据常驻 GPU（`alphagen_qlib/stock_data.py:78-86`）。

#### 2.2.2 RPN 构造与动作掩码（最值得抄的一段）

`alphagen/data/tree.py:7-88` 的 `ExpressionBuilder` 维护表达式栈：`add_token` 遇算子弹出 `n_args` 个子表达式构树（`:19-37`），`is_valid` 要求栈恰 1 个且 `is_featured`（`:39-40`）。`validate_op`（`:54-79`）按算子类判定合法性：Unary 需栈顶 featured；Binary 需至少一个 featured 且无 `DeltaTime`；Rolling 需栈顶 `DeltaTime`、次栈顶 featured；PairRolling 需栈顶 `DeltaTime` 且下两个都 featured。

`rl/env/core.py:78-99` 汇总为 `valid_action_types()`，`rl/env/wrapper.py:60-83` 的 `action_masks()` 转成布尔掩码给 `MaskablePPO`；动作空间 `SIZE_ACTION = 22+6+5+14+1 = 48`（`wrapper.py:10-16`）。**结果：采样出的每个表达式语法与类型 100% 合法，没有"生成后校验再丢弃"的算力浪费。**

#### 2.2.3 reward 不是单因子 IC，是「组合增量」

这是 AlphaGen 与多数框架最本质的区别。`AlphaEnvCore._evaluate()`（`rl/env/core.py:67-76`）调用 `pool.try_new_expr(expr)`，把返回值作为 reward；`MseAlphaPool.try_new_expr`（`models/linear_alpha_pool.py:61-104`）返回「把该因子加入池并重新优化权重后的组合目标 `new_obj`」。单因子 IC/RankIC 只用于过滤与互相关计算（`:173-189`）。**搜索与组合是耦合的**。

策略：`sb3_contrib.ppo_mask.MaskablePPO`（`scripts/rl.py:252-270`，仓库内未找到自研 PPO），`gamma=1.0 / ent_coef=0.01 / batch_size=128`；网络 `LSTMSharedNet`（`rl/policy.py:70-106`，2 层 LSTM + token embedding + 位置编码）或 `TransformerSharedNet`（`:26-67,109-151`）。IC 用批量 `batch_pearsonr/batch_spearmanr`（`utils/correlation.py:33-54`）。

#### 2.2.4 AlphaPool：动态因子池

`alphagen/models/linear_alpha_pool.py`：

- **容量与淘汰**（`:61-104`）：`capacity` 取 10/20/50/100（`scripts/rl.py:300-304`）；新因子先算单因子 IC 与对池内所有因子的 mutual IC，任一 `>0.99` 直接拒收（`:173-189`）；超容量剔除 `|w|` 最小者，若新因子自己最差则回滚并写入 `_failure_cache`（`:74-80`）。
- **权重联合优化**（`:285-329`）：`MseAlphaPool.optimize` 最小化 `loss = wᵀMw − 2wᵀc + 1 + α‖w‖₁`（`M`=mutual IC 矩阵、`c`=single IC 向量、`α=5e-3`），Adam(lr 5e-4, 10000 步, patience 500)；`α≈0` 走 `np.linalg.lstsq(M,c)` 闭式解。**优化目标直接是组合 IC**，不是 MSE；L1 产生稀疏、可自动淘汰。
- **ICIR / LCB 目标**（`:336-404`）：`MeanStdAlphaPool` 用逐日 `batch_pearsonr` 直接优化 `ICIR = mean/std` 或 `LCB = mean − β·std`。
- **增量维护**：`_mutual_ics` 矩阵（`:28,205-206`）、`_extra_info` 缓存每因子值（`:356-357`）。

#### 2.2.5 数据切分与回测

训练 `2012-2021`，三段 OOS `2022H1/2022H2/2023H1`（`scripts/rl.py:206-213`）；`CustomCallback._on_rollout_end`（`:87-105`）每轮在各 OOS 段算 IC/RankIC 并存 checkpoint + `pool.json`。target = 20 日未来收益 `Ref(close,-20)/close-1`（`:196`）。回测用 qlib `TopkDropoutStrategy(top_k=50,n_drop=5)`（`backtest.py:60-147`）。

---

### 2.3 AlphaForge —— 生成-预测双阶段 + 逐期动态组合（AAAI 2025）

#### 2.3.1 主方法不是 MCTS/遗传，是 GAN

`README.md:16-43` 的三阶段：stage1 `train_AFF.py` 挖因子存 zoo（`zoo_size=100`）、stage2 `combine_AFF.py` 动态选因子加权、stage3 汇总；train/valid/test 按年 `2010-2020 / 2021 / 2022`（`:24`）。

**Generator = DCGAN**（主流程；`NetG_Lstm/NetG_CNN` 定义但未用）：

- `NetG_DCGAN`（`gan/network/generater.py:17-63`）：`z∈R¹⁰⁰` → Linear → ConvTranspose2d/Conv2d → `(bs,20,SIZE_ACTION)` logits
- `NetM`（`gan/network/masker.py:10-57`）：**无可训练参数**，逐步用 `Builders.get_valid_op()` 掩码把非法动作置 `-1e8` 并回传梯度；配 `F.gumbel_softmax(hard=True)`（`generater.py:230-234`）使采样可微
- 损失（`gan/network/loss.py:49-60`）：`−mean(pred)` + `loss_simi`（两组样本 token 相似度 >0.4 惩罚）+ `loss_potential`（predictor latent 余弦相似度 >0.4 惩罚），**显式多样性正则**
- **Predictor 代理网络**：`NetP`（`gan/network/predictor.py:13-49`）= Conv2d + fc，输入 one-hot 表达式序列，用真实 `metric` 分数作标签、`weighted_mse_loss`（近期样本权重 2，`train_AFF.py:39-58`）、`train_test_split(0.2)` + 早停。**作用：给候选打分，减少真实 evaluate 次数。**

`train_RL.py`（MaskablePPO+AlphaPool）、`train_GP.py`（gplearn）、`train_DSO.py` 都是**对照 baseline**，不是主方法。

#### 2.3.2 算子集（23 启用 / 31 定义）

`alphagen/config.py:8-39` 启用 23 个：Unary `Inv` `S_log1p`；Binary `Add/Sub/Mul/Div/Pow`；Rolling `Ref/ts_mean/ts_sum/ts_std/ts_var/ts_max/ts_min/ts_med/ts_mad/ts_div/ts_pctchange/ts_delta/ts_wma/ts_ema`；PairRolling `ts_cov/ts_corr`。文件内另定义 `ts_skew/ts_kurt/ts_rank/ts_ir/ts_min_max_diff/Abs/Sign/Log/CSRank/Greater/Less` 等共 31 类但多数未启用（`expression.py:490-522`）。窗口 `[1,5,10,20,30,40,50]`（`:41`），`SIZE_ACTION=51`。表达式改写为 qlib 风格 `ts_mean(x,20)`，便于 `combine_AFF.py:14` 用 `eval` 还原。

#### 2.3.3 逐期动态组合（与我们末层固定权重的直接对照）

`combine_AFF.py:139-254`：

1. 载入 zoo，按 `|score|` 排序；`exprs2tensor`（`builder.py:422-433`）把**全部因子一次性求值**成 `fct_tensor(n_days,n_stocks,n_exprs)` 并 `normalize_by_day`，预算每日 `ic/ric/ret` 序列（`:145-157`）。
2. 从验证集首日**逐日 walk-forward**（`:171`）：`shift=21`（`:160`），`begin = cur-window-shift`（`:175-178`），**只用 `begin:cur-shift` 的历史数据**算各因子 `icir/ricir/retir`（`:180-205`）。
3. **每期选因子**：按 `|ricir|` 排序，筛 `ric>0.02 & ricir>0.2`，取 top `n_factors=10`（`:206-215`）。
4. **每期重新加权**：对选中因子的历史窗口做 `torch.linalg.lstsq`（带常数列）求 `coef`，预测当日；权重逐日变化并保存（`:219-243`）。

> `shift=21` + 仅用历史窗口 = 明确的防前视设计。这比我们「样本内一次岭回归、之后固定」表达力强得多，且正好回答了用户此前提的「逐时间截面回归」——**业界已有成熟实现，但前提是先有统计闸门**。

#### 2.3.4 防过拟合与数据

`gan/utils/data.py:7-59` 的 `get_data_by_year` 按年切分，并额外构造 `data_valid_withhead` / `data_test_withhead`（**回带 2 年历史**供 rolling 算子回看，`:25-27`）；`StockData` 整体 pickle 缓存。`alphagen_qlib/stock_data.py:15-37` 用 `$factor` 把复权价还原为原始价，规避复权前视。

其他闸门：predictor `train_test_split(0.2)` + 早停、generator 早停（`generater.py:289-299`）、NaN 占比 >0.8 / 唯一值比率 <0.01 / 与 zoo 相关 >`corr_thresh` 置 0（`train_AFF.py:127-144`）、入 zoo 阈值 `ic>0.03 & icir>0.1`（`:159-160,203-206`）、每 5 轮 rebalance（`:339-348`）、多 seed。

**未找到** FDR / Holm / purge / 多重检验校正——它的防过拟合主要靠"多段切分 + 代理模型早停 + 相关性过滤"。

---

## 3. 框架源码层详解（二）：LLM / Agent 驱动挖掘

### 3.1 RD-Agent（微软）—— 结构化闭环的工业级范本

> 版本 v1.0.0（`chore(main): release 1.0.0 #1286`），因子场景主入口 `rdagent/app/qlib_rd_loop/factor.py:30-62`。

#### 3.1.1 两层嵌套进化循环

**外层 `RDLoop`**（`components/workflow/rd_loop.py`）由 `LoopMeta`（`utils/workflow/loop.py:34-75`）按方法定义顺序收集 step，执行链：

```
direct_exp_gen → coding → running → feedback → record
```

| 环节 | 实现类 | 源码 |
|---|---|---|
| proposal | `QlibFactorHypothesisGen.gen`（基类 `LLMHypothesisGen.gen`） | `rd_loop.py:184-191`、`components/proposal/__init__.py:29-65` |
| 假设→实验 | `QlibFactorHypothesis2Experiment.convert`（`@wait_retry(retry_n=5)`） | `rd_loop.py:193-196`、`components/proposal/__init__.py:93-121` |
| coding | `QlibFactorCoSTEER.develop` | `rd_loop.py:212-215`、`scenarios/qlib/developer/factor_coder.py:3` |
| running | `QlibFactorRunner.develop`（`@cache_with_pickle`） | `rd_loop.py:217-220`、`factor_runner.py:63-202` |
| feedback | `QlibFactorExperiment2Feedback.generate_feedback` | `rd_loop.py:222-236`、`feedback.py:55-118` |
| record | `Trace.sync_dag_parent_and_hist` 写 DAG | `rd_loop.py:238-241`、`core/proposal.py:256-284` |

**内层 CoSTEER**（`components/coder/CoSTEER/`）：单轮内对代码做最多 **10 次**「执行—评估—纠错」自愈（`config.py:15-18`：`max_loop=10, fail_task_trial_limit=20`）。`CoSTEER.develop`（`:93-155`）建 `RAGEvoAgent` 跑 `multistep_evolve`，`fallback` 保留最后一个**可接受**解而非最后一解（`:120-127,142-146`）。

#### 3.1.2 两段式 JSON 生成（我们 LLM 范式最该抄的一点）

RD-Agent 不让 LLM 直接吐代码，而是两次结构化调用：

1. **假设生成**：`components/proposal/prompts.yaml:2-39` 的 system 注入 `scenario / user_instruction / hypothesis_specification / hypothesis_output_format`，user 注入 `hypothesis_and_feedback`（全量历史）/ `last_hypothesis_and_feedback` / `sota...` / `RAG`；输出 JSON `{"hypothesis": "...", "reason": "..."}`（`scenarios/qlib/prompts.yaml:70-75`）。
2. **假设 → 结构化因子规格**：`LLMHypothesis2Experiment.convert`（`__init__.py:96-119`）用 `factor_experiment_output_format`（`scenarios/qlib/prompts.yaml:114-134`）约束输出 `{factor_name: {description, formulation(latex), variables}}`，`json_target_type=dict[str, dict[str,str|dict]]`；`convert_response`（`factor_proposal.py:94-132`）构造 `FactorTask`。规格要求「1-5 Factors per Generation」「Simple and Effective Factors First」「Avoid re-implementation」（`:95-112`）。
3. **规格 → 代码**：coder 通过 `QlibFactorScenario.get_scenario_all_desc`（`factor_experiment.py:82-99`）注入**硬接口约束**：必须写 `calculate_{function_name}`、`if __name__=="__main__"`、结果存 `result.h5`、索引为 `(datetime, instrument)` MultiIndex、**禁止写 try-except**（异常由框架捕获回灌）（`experiment/prompts.yaml:30-33`）。

#### 3.1.3 执行反馈闭环与结构化 critique

- `FactorFBWorkspace.execute`（`factor_coder/factor.py:107-210`）用 `subprocess.check_output` 跑因子脚本，捕获 `CalledProcessError` traceback（路径脱敏、超 2000 字符截断，`:171-192`），检查 `result.h5`（`:194-208`）。
- `FactorEvaluatorForCoder.evaluate`（`evaluators.py:31-120`）串联：execution → `FactorValueEvaluator` → `FactorCodeEvaluator`（LLM critic，输出 `critic 1: ...`，`eva_utils.py:67-117`）→ `FactorFinalDecisionEvaluator`（`:184-199`，JSON `final_decision/final_feedback`）。
- `implement_one_task`（`evolving_strategy.py:60-164`）把 `queried_similar_successful_knowledge` / `queried_similar_error_knowledge`（错误→成功代码配对）/ `error_summary_critics` 注入下一轮 prompt（模板 `prompts.yaml:40-105`），动态裁剪防超长（`:102-137`）。这是**动态 few-shot（RAG）而非静态示例**；检索见 `CoSTEER/knowledge_management.py:595-676,725-`。
- `error_summary` 的 system 明确写 **"When processing data, avoid time leakage"**（`prompts.yaml:122`）。

#### 3.1.4 SOTA 因子库与数值去重

- `convert_response`（`factor_proposal.py:112-114`）把 `[空基线] + [所有 feedback 为真的历史实验]` 设为 `exp.based_experiments`。
- `process_factor_data`（`developer/utils.py:131-177`）重新执行每个历史实验的 `factor.py` 并拼成大表；`factor_runner.py:87-113` 把 SOTA 表 + 新因子表拼接后回测，测**增量贡献**。
- **`deduplicate_new_factors`**（`factor_runner.py:46-61`）：按 `datetime` 分组算 SOTA 与新因子的横截面 Pearson 相关，取时间均值，`IC_max.unstack().max(axis=0)`，**与任一库内因子相关 ≥0.99 即剔除**；全被剔除抛 `FactorEmptyError`（`:107-110`）。
- 名称级去重较粗（`factor_proposal.py:116-131`）；研报场景有 embedding + KMeans + LLM 判重（`pdf_loader.py:453-564`），但 `load()` 中该调用**被注释**（`:587`），当前未启用。

#### 3.1.5 评价与它也没有的东西

指标：IC / RankIC 由 qlib `SigAnaRecord` 产出（`conf_combined_factors.yaml:87-91`）；反馈指标 `IMPORTANT_METRICS = [IC, 1day.excess_return_with_cost.annualized_return, ..., max_drawdown]`（`feedback.py:17-21`），拼成 "Current vs SOTA" 喂 LLM（`prompts.yaml:165-225`）。切分 CSI300 `train 2008-2014 / valid 2015-2016 / test 2017-2020`（`conf.py:77-93`）；`RobustZScoreNorm` 用 `fit_start_time/fit_end_time` 把拟合窗口锁在 train（`conf_baseline.yaml:26-32`）；`TopkDropoutStrategy` 含 `open_cost=0.0005 / close_cost=0.0015`。

**明确未找到**：多重检验校正（Deflated Sharpe / BH-FDR）、样本外二次留出、过拟合惩罚。甚至 `prompts.yaml:187-190` 鼓励 "Any small improvement ... set Replace Best Result yes"，**有过拟合倾向**；且 hypothesis 阶段能看到历史 test 指标，存在间接信息泄漏。

#### 3.1.6 与 FactorMiner LLM 范式对照

| 维度 | FactorMiner 现状 | RD-Agent(Q) |
|---|---|---|
| 候选生成 | 同一 system prompt 重复 `batch_size` 次 → 近乎重复候选 | 一次生成假设 → 一次生成 1-5 个具名结构化规格 |
| 回灌 | 累积 success/failed reflection 文本 | 全量 trace + 结构化字段（假设/任务/三项指标/Observation/Evaluation/Decision/New Hypothesis） |
| critique | 无 | 独立 LLM critic 链 + 最终决策 |
| 执行修复 | 无 | 单轮内最多 10 次执行—评估—纠错 |
| 去重/库 | 无 | SOTA 库 + 0.99 相关过滤 + 名称过滤 |
| 统计闸门 | 无 | **也无**（这是我们反超的机会） |

---

### 3.2 五个较新 LLM 挖掘框架：成熟度审查

> 这几个仓库 star 少、较新，且都在 2026 年出现。以下**如实评估**，不美化。

#### 3.2.1 `LLM-Alpha-Mining-Framework`（Aroesler1）—— 唯一有完整统计闸门与诚实审计

定位（README `:1-13`）：用 LLM + 遗传规划挖 S&P500 公式化因子，**明确点名三大可信度威胁**：多重检验、模型背诵已发表 alpha、窗口边界 look-ahead（`:8-10`）；对照臂为 GP / 随机语法公式 / Alpha101（`:10-13,66-68`）。

架构（`evo/runner.py:1-22`）：单轮 `propose → gate → score → admit → reflect → reshuffle`；`evo/operators.py:37-40` 四算子 `EXPLORE/MUTATE/CROSSOVER/SIMPLIFY` + `REFLECT`，每个是 prompt 模板 + JSON schema；3 个 island（价格趋势/流动性成交量/基本面），最多 8 轮、验证 IC 不再提升即早停。**文本 gate 先跑，~90% 拒绝发生在碰 DataFrame 之前**（`fitness.py:3-6`）——省算力设计。

独特机制：
- **18-niche MAP-Elites archive**（`evo/archive.py:1-25`）：`horizon(3) × data family(3) × turnover(2) = 18` niche，每个 8 成员、144 因子上限；满 niche 淘汰最弱（`:210-235`）；稀疏 niche 选择权重 ×2、陈旧 ×0.5（`:289-304`）；`niche_summary` 连空 niche 也列出以引导 EXPLORE（`:140-156`）。**专治"一个 idea 的 20 种改写"**。
- **anti-paraphrase gate**（`fitness.py:8-16,38-41`）：六层 = sanitizer / complexity / Alpha101 共享子树 / archive 共享子树 / coverage / rank correlation；阈值 `max_shared_subtree=5`、`max_abs_corr=0.7` 仅在 validation 上 sweep 后冻结（`config.py:69-105`）。
- **防泄漏纪律**（`runner.py:16-17`）："validation IC 从不写入 archive entry 的 metrics、从不传给 feedback"，**并用测试强制**；每条原始 prompt/reply 解析前先落盘（`:19-21`）。

**统计闸门全套（本报告最重要的一节）**：

| 闸门 | 实现 | 源码 |
|---|---|---|
| 三段窗口 + "Nothing is tuned on the holdout, ever." | fit 2000-2013 / validation 2014-2017 / holdout 2018-2025；`latest_visible_year` 限制 prompt 可见年份防 LLM 背诵 | `evo/config.py:9,27-54` |
| purged labels | `window()` 用 `labels_known_by` 保守剔除跨边界 label，"can over-purge, but cannot admit a later outcome"；`stat_dates` 防 holdout turnover 泄漏进 gate | `factors/ic_panel.py:195-208,240-249` |
| Newey-West HAC t | Bartlett 21 滞后；实测中位数 `|iid t|/|NW t|=1.03`，诚实记录"膨胀来自多重性而非自相关" | `factors/multiple_testing.py:10-17,66-90` |
| **四道多重检验** | HLZ `|t|>3.0`（`:49`）；`benjamini_hochberg` FDR（`:190-213`）；`romano_wolf` stepdown bootstrap FWER（`:216-249`）；`evaluate_pool` 用**随机语法池 max\|t\| 经验零分布**（`random_max_critical`）回答"这么大的搜索纯噪声能产生多大 \|t\|"（`:267-306`） | 同上 |
| 试验次数分母 | "trace is also the multiple-testing denominator. Counting only the survivors ..." —— **把全部尝试当分母，不只算幸存者** | `factors/experiment_trace.py:23,135` |
| Holm 比较校正 | `adjust_comparisons` 对 5 个比较做 Holm，输出 `holm_p` / `reject_holm_5pct`；默认 `evidence_status="historical_labels_not_boundary_purged"` | `evo/comparison_audit.py:7-26` |
| Deflated Sharpe + 8 道 promotion gates | PSR / `expected_max_sharpe` / `deflated_sharpe`（Bailey & López de Prado 2014）；`gate_1 DSR>0.95`、`gate_2 8 子期至少 6 个正 Sharpe`、`gate_8 净 Sharpe>0.4`，全通过才晋级 | `backtest/validation.py:44-96,130-140,166-280` |
| Walk-forward + 冻结协议 | `input_snapshot` 冻结所有输入 hash；`claim_evaluation` **在读 holdout 之前**以独占方式写 `consumed_before_read`，**失败评估也消耗窗口** | `evo/evaluation_protocol.py:24-46`、`docs/frozen_new_experiment_protocol.md:7-32` |
| 结果诚实 | "No significant search advantage after familywise correction"（最小 raw p=0.026 → Holm 后 0.13）；Romano-Wolf 仅保留 24% LLM 因子；主动写 Limitations（holdout 不干净、LLM 训练数据覆盖 2018-2025、信号弱 IC 0.0005~0.0101、无成本调整组合） | `README.md:33-37,44-46,88-105` |

> 这是 18 个仓库里**唯一不美化结果**、且把"搜索了多少次"完整折进显著性判断的实现。对 FactorMiner「同一 prompt 重复 batch_size 次」的痛点，最直接可移植的是 `evaluate_pool` 的 `passes_random_max`：用同等规模随机公式搜索产生的最大 `|t|` 作诚实门槛。

#### 3.2.2 `CosmosAlpha` —— 搜索算法最丰富，但无统计闸门

宣称多关系图（生成谱系 + 表达式结构相似 + 截面暴露相关）+ Graph Flow 效用传播 + Pareto 选择，接 LightGBM（`README.md:7-12`）。**算法是真实现的**：

- MCTS：`pipeline/mcts/controller.py:229-230,415,447`（UCT 选择 / 扩展 / 反向传播 + 动态预算 `:334-353`）
- Graph Flow：`pipeline/graph/flow.py:255-281` 确定性向量奖励扩散 `(1-γ)self + γ·neighbors` 迭代到收敛
- Pareto：`pipeline/graph/reward.py:42-53,108` 三维目标 `(|rank_ic_mean|, -rank_ic_std, top20_return)` + `pareto_dominates` + crowding distance
- 类型化 DSL：`factors/expression.py:21-39,97-103`（schema version、`OperatorScope` 分 `elementwise/time_series/cross_sectional/industry_group`、stable_sha256 ID）

**缺陷**：`evaluation/_statistics.py:11-57` 只有 `safe_pearson`；`_chronological_segment_means`（`evaluator.py:808`）只是单一样本内分段稳定性。四个挖因子配置 `start_date=2010-04-01, end_date=2019-05-31` 完全一致，**因子在同一窗口内评估/选择/扩散，无独立 holdout、无多重检验**；train/valid/test 只存在于 LightGBM 模型层配置。

#### 3.2.3 `EvoAlpha` —— 最接近"prompt 包装"

`factor_search/controller.py:145-237` 是 GA 式「生成—筛选—入池」；6 个硬编码 persona（`personas.py:11-54`）+ mutation/crossover 两种模式，**无真正角色分工**（无独立 critic/coder/evaluator）。`quality.py:4-30` 自述 "Placeholder for your real ... API"，`api/factor_eval_client.py:1-16` 自述 template，打 `localhost:19889`；**仓库内无回测、无数据、无算子白名单**。`validator.py:84-105` 只有绝对阈值 `|ic|≥0.02, |rank_ic|≥0.02, |icir|≥0.2`，无 p 值 / holdout / 多重检验。README 仅 13 行，源码未出现 "EvoAlpha"/"evolution"/论文引用，**"论文官方实现"未找到依据**。

#### 3.2.4 `WorldQuant-LLM` —— 工程运维闭环完整，统计检验为零

`run_alpha_miner.py:1585` 生产者—消费者并发，真实在线闭环（WQ Brain API + SQLite + 飞书 + cron + Docker）。"智能"在 prompt/启发式调度层：`log_minmax_softmax`（`:461-479`）、动态字段采样（`:481-539`）、失败模式指纹黑名单（`:245-267`）、参数 sweep（`:1347`）、精英杂交（`:808`）、挽救池、`Sharpe<-0.8` 自动取反。**筛选完全是平台返回的 `sharpe≥1.25 & fitness≥1.0`**（`:1132-1133`）。反复 sweep + rescue + 反向重测在同一平台样本上大量试错，**恰是 data snooping 温床，却无任何多重检验校正**。`data/` 目录为空，算子从 WQ API 运行时拉取。

#### 3.2.5 `alphaminingv2` —— Agent 编排最丰富，文档与代码漂移

`agent/direct/agent.py:37,168` + `llm_conversation.py:192-263` 工具 dispatch + `phase_manager.py:20-34,84-91` Creative/Improvement 互斥相位状态机（8 轮或连续 5 轮未破最佳 Sharpe 强制切回 Creative）+ 知识库 + 结构指纹去重。**但 `agent/config.py:63` `enable_critic: bool = False`（批判 agent 默认关闭）**；"14-tool protocol"在源码中**未找到依据**，实测 README 写 15/16、prompt 表 12、dispatch 表 17、方法注释编到 Tool 16 且编号重复。统计检验为零，收敛靠平台 Sharpe。`backtest_results/` 被 gitignore，无审计轨迹。

#### 3.2.6 横向小结：LLM 挖因子的共性架构与分水岭

| 环节 | 最强实现 |
|---|---|
| 假设生成 | CosmosAlpha 多角色 + Lesson；LLM-Framework 四算子 + 三 island |
| 实现 | CosmosAlpha 类型化 DSL；LLM-Framework sanitizer 白名单 |
| 执行 | LLM-Framework 本地 CRSP panel；其余多外包给 WQ Brain |
| 批判 | LLM-Framework `REFLECT`；RD-Agent 独立 critic 链 |
| 记忆/去重 | **RD-Agent SOTA 库 + 0.99 相关过滤** / LLM-Framework 18-niche MAP-Elites |
| 筛选 | LLM-Framework 6 gates + cheap-then-full screen |
| **统计闸门** | **仅 LLM-Alpha-Mining-Framework 与 crypto-factor-lab 两家有** |

一句话：前六个环节大家都在做（差别在工程精度），**第七环"统计闸门"是分水岭**。这正对应我们「重复采样 → OOS -1.14」：重复采样把试验次数放大，却没有任何机制折减显著性。

---

## 4. 框架源码层详解（三）：加密货币专用框架

### 4.1 `crypto-factor-lab` —— 防过拟合闸门最系统（重点）

> 定位（`README.md:3-5`）：可复现的 BTC 单资产因子挖掘闭环，研究/工程演示、不实盘。数据 Binance USD-M 1h，SHA-256 校验 manifest（`sources/binance.py:139-198,201-259`）。

#### 4.1.1 算子与加密特有字段

DSL 共 **21 个 op**（`expression.py:15-19`）：二元 4 `add/sub/mul/div`、一元 4 `abs/neg/log/sign`、窗口 10 `return/log_return/delta/shift/rolling_mean/rolling_std/rolling_sum/rolling_min/rolling_max/zscore`、结构 3 `field/constant/range`。字段 6：`open/high/low/close/volume/funding_rate`（`:20-27`）。

生成器 `GeneratorConfig(fields=("close","volume","funding_rate"), windows=(2,4,8,12,24,48,96), max_depth=3, max_complexity=6, max_candidates=5000)`（`generator.py:28-38`），按 5 族均衡抽样（`:152-167`）。**服务主入口改用 `fields=("close","open","high","low","volume")`，不含 funding_rate**（`service.py:676`）——一个值得注意的口径不一致。

手工 6 因子（`factors.py:63-84`）：`price_momentum / price_reversal / realized_volatility / relative_volume / trend_efficiency / funding_rate_zscore`。

加密特有设计：
- **`funding_rate_zscore` 用 `aligned.shift(1)` 保守延迟一根 K 线防未来函数**（`factors.py:45-60`）
- **funding 双重身份**：既是因子也是成本（`execution.py:96`）
- 7x24：`periods_per_year=24*365=8760`（`mining.py:239`、`suite.py:49`、`service.py:947`）
- 时间算子只回溯（`expression.py:258-294`）、`group_level` 避免面板跨资产串值（`:223-231`）、面板标签按 symbol 分组（`labels.py:8-21`）
- **point-in-time 资产池** `liquidity_universe`（`universe.py:8-23`），但只在 `pipeline.py:76` 的横截面实验调用
- funding 按 UTC 结算时间对齐、`round("1h")`、>1min 拒收、去重（`binance.py:349-365`）

挖掘算法：**确定性公式枚举** + 受约束 LLM 提议（Qwen 结构化 DSL AST，`README.md:294-322`），无 GP/RL/NN。

#### 4.1.2 防过拟合闸门逐条（本次亲自复核核心代码）

| # | 闸门 | 实现 | 源码 |
|---|---|---|---|
| 1 | 三段切分 | `TimeSplit(train_ratio=0.6, validation_ratio=0.2, holdout_ratio=0.2)` | `mining.py:21-34` |
| 2 | **purge 隔离** | `segments(observations, purge_bars)`：`train_end=validation_start-purge_bars`、`validation_end=holdout_start-purge_bars`，两处边界前挖空 | `mining.py:40-61` ✅已复核 |
| 3 | purged walk-forward | `walk_forward_factor_evaluation(factor,target,train_bars,test_bars,purge_bars)`，输出 `mean_test_rank_ic` / `positive_test_rank_ic_ratio` | `validation.py:18-72` |
| 4 | 实际 purge 值 | `purge_bars=forward_horizon`（默认 4h = 标签重叠长度） | `run_factor_mining.py:22,67`、`service.py:791` |
| 5 | 不读留出 | `train_slice, validation_slice, _ = split.segments(...)` 丢弃 holdout，holdout 指标不进排序/报告 | `mining.py:245,275` |
| 6 | 稳定性 | `_rolling_rank_ics(stability_windows=8)` + `validation_stability_directional_ratio` | `mining.py:147-164,316-334` |
| 7 | **HAC / Newey-West** | `_hac_mean_test(values, hac_lags=1)`，Bartlett 权重、长期方差、`p=erfc(|t|/√2)` | `mining.py:167-189,319` |
| 8 | **BH FDR** | 手写 `thresholds = fdr_q*arange(1,m+1)/m`，超阈标 `fdr_rejected`，`fdr_q=0.05` | `mining.py:378-394` |
| 9 | 二阶段显式门槛 | `SelectionPolicy(min_validation_abs_rank_ic, min_stability_directional_ratio, min_validation_sharpe, min_validation_net_return, min_validation_drawdown, max_validation_turnover, require_fdr_pass=True)` | `mining.py:74-117,437-482` |
| 10 | 复杂度惩罚 | `selection_score = abs(val_rank_ic) - 0.001*complexity` | `mining.py:351` |
| 11 | 相关性去重 | 验证期 Spearman `|corr|>=0.95` → `duplicate`，按 score 贪心留代表 | `mining.py:234,395-421` |
| 12 | **执行成本** | `CostModel(fee_bps=4, half_spread_bps=1, slippage_bps=1)`；`position=desired.shift(1)` 下一根执行；`trading_cost=turnover*rate`；`funding_cost=position*funding_rate` | `execution.py:12-26,66-102` |
| 13 | 阈值冻结 | `quantile_positions(dev,live,quantile=0.8)` 入场分位只在开发段拟合；`rebalance_every` 非重叠调仓 | `execution.py:40-63`、`service.py:940-943` |
| 14 | **随机零假设** | `random_rank_ic_benchmark(target,trials=100,seed=...)` 取 `absolute_rank_ic_95` | `benchmarks.py:19-47`、`service.py:915-919` |
| 15 | **最终留出确认三条件** | ①仅 `direction` 角色 ②`protocol_version=="2.3"` ③留出段与账本不重叠；入库 = `aligned_holdout_rank_ic > random_null_95` **且** `net_return>0` **且** `net_return - buy_hold.net_return>0` | `service.py:870-881,900-906,955-966` ✅已复核 |
| 16 | **一次性留出揭示账本** | `latest_revealed_holdout_end(dataset_id)` 查历史 `promotion_result_json.holdout_end` 取 max；重叠即 `raise ValueError("holdout period overlaps previously revealed data; add a non-overlapping unseen period before final confirmation")` | `catalog.py:282-299`、`service.py:900-906` ✅已复核 |
| 17 | Constrained LLM | 请求仅含字段/允许算子/窗口/复杂度（`:712-723`）；system 明令 "do not use future/validation/holdout/observation data"（`ai/client.py:205-209`）；本地 `validate_proposal_batch` 复校（`ai/proposals.py:45-102`）；**AI 与公式共享同一 200 候选总预算**（`service.py:756-777`）；只做 1 轮、不根据验证结果追问（`README.md:312`） | 见左 |

**缺口（如实标注）**：① 主入口 `run_factor_mining.py:62-72` / `service.py:786-796` 调 `mine_candidates` 时**未传 `cost_model`/`funding_rate`**，成本闸门只在 `suite.py` 与最终确认生效（这个坑我们要避开）；② `SelectionPolicy` 数值门槛默认全 `None`；③ Holm / Deflated Sharpe / 独立 embargo **未找到**。

#### 4.1.3 组合与回测

`suite.py:120-164` 等权组合 + 逐因子留一法边际贡献；`DEFAULT_SIGNAL_SEMANTICS` 把 volatility / volume / efficiency / **funding_rate_zscore** 标为 `neutral`（不做方向回测，等待专用协议），仅 momentum/reversal 做方向回测（`:30-37,84-94`）——**对"非方向型因子硬套多空回测"的清醒处理，值得我们借鉴**。

---

### 4.2 `AlphaForge-Crypto`（warren618）—— 数据层最全、因子层最空

> 流水线 `Factor Mining → Statistical Evaluation → Combo Search → Walk-Forward Backtest → Paper Trade → Live`（`README.md:18,27-35`）。

**数据层确实丰富**（`src/data_fetcher.py`，OKX V5，BTC/ETH/SOL-USDT-SWAP，5m/15m/1H）：`fetch_funding_rate_history:446`、`fetch_oi_history:601`、`fetch_ls_ratio_history:675`、`fetch_taker_volume_history:704`、`fetch_index_candles_batch:782`、`fetch_put_call_ratio:934`、`fetch_margin_loan_ratio:977`、`fetch_spot_taker_volume:1021`、`fetch_top_trader_ls_ratio:1066`、`fetch_contract_oi_volume:1110`、`fetch_mark_price_candles_batch:1276`、`fetch_liquidation_orders:1375`、`fetch_cross_asset_daily:1515`。README:61 声称的 FRED / CryptoPanic 未找到。

**但实现完整度严重不足**：
- 因子只有 3 个 OHLCV 示例（`src/factor_pipeline/factors/example.py:8-51`），README:124-133 的 8 大类因子族（Microstructure / Derivatives / On-Chain / Macro / basis…）**全是占位**
- 示例用 `@register_factor`（`example.py:5,8`）但该函数**全仓库无定义**；`src/factor_pipeline/__init__.py:9` `from .pipeline import FactorPipeline` 指向**不存在的 `pipeline.py`** → 直接 import 会 ImportError
- 主入口 `run_backtest.py:32-41` 回测逻辑全是 TODO，`--walk-forward` 解析后未调用
- `backtest.py:107` `exec_prices = df["open"]` 定义后从未使用，收益用 `df["close"].pct_change()`（`:110`）；README 声称的 "next-bar-open / 爆仓模拟 / fixed-fractional 仓位" 未落地，`leverage/maintenance_margin/position_fraction/max_drawdown_pct` 仅出现在 dataclass 定义
- 年化不一致：`backtest.py:163` 用 `sqrt(252)`（A 股习惯），`bootstrap.py:17` 用 365

**统计闸门（唯一可借鉴部分）**：
- 滚动 Rank IC（Spearman）4 horizons `[6,12,24,48]`（`evaluator.py:45,92-120`）、IC IR、IC 衰减 half-life 指数拟合（`:147-160,354-389`）
- **Newey-West t-stat**：`_newey_west_tstat`，`maxlags = n**(1/3)`，OLS + `cov_type="HAC"`（`:325-351`）
- **BH FDR**：`multipletests(..., method="fdr_bh", alpha=0.05)`（`:268-283`）——但**只对 horizon=12 的单一 p 值列、对因子个数校正**，未覆盖多 horizon 与组合搜索产生的多候选
- `TimeSeriesSplit(n_splits=5)`（`combo_search.py:304-309`）**无 purge/embargo**；`walk_forward(train_pct=0.7,n_splits=5)`（`backtest.py:194-240`）**无 purge/embargo** 且训练段因子算完即丢弃
- `bootstrap_sharpe(n_bootstrap=5000, annualization=365, qualified=p5>0)`（`bootstrap.py:14-73`）**主流程未调用**

**组合搜索**：`greedy_forward(max_corr=0.7,min_ic_ir=0.3,max_factors=15)`（`:183-190`）、`regularized_optimize`(LassoCV/RidgeCV `:259`)、`cluster_select`(层次聚类 `:340`)；网格 `max_corr∈[0.5,0.6,0.7,0.8] × min_ic_ir∈[0.2,0.3,0.5]` 产 12 组候选（`:553-562`）——**这些候选未做多重检验校正**。

---

### 4.3 `alpha-factor-mining-crypto`（tianchen95hk）—— 加密因子清单最实用，统计最弱

Binance USDT 永续**横截面**因子研究，4h/1h，ccxt 取 `exchangeInfo` 过滤 `quote=USDT & PERPETUAL` 后按**当下**成交额 top 30/40（`data_loader.py:32-55`）——**明确有幸存者偏差**。

**因子清单（最值得抄的一部分）**：核心 13 个（`factors.py:69-104`）：

`mom_24 / mom_72 / short_reversal_6 / volatility_adjusted_trend / intrabar_reversal / range_expansion / volume_shock / illiquidity_reversal(Amihud) / **funding_crowding_unwind** / **funding_zscore_revert** / vol_regime_reversion / downside_pressure_revert / trend_volume_confirmation`

扩展 40 个多窗口版本（`:120-169`），组合因子 `_build_combo_factors` 用 top-16 两两 × `add/sub/mul` 最多 40 个（`:174-204`），总预算 `FACTOR_MAX_COUNT=80`（`settings.py:45`），全部转横截面 pct rank（`:207-212`）。**加密特有**：2 类 funding 因子共 6+ 窗口变体、Amihud 用成交额、intrabar/range 用 high/low。

算法：**纯模板枚举 + 两两组合枚举**，无 GP/RL/LLM/NN；去冗余 Spearman `corr_threshold=0.75`（`evaluation.py:28-136`）。评价：横截面 IC/IC IR/hit_rate/`t=mean_ic/(std_ic/sqrt(obs))`（简单 iid，`:206-215`）、5 分位 spread（`:218-241`）、`score = 0.7*|spread_ir| + 0.3*|ic_ir|`（`:23`）。

**防过拟合最弱**：全 `src/` grep 未找到 FDR/Holm/DSR/purge/walk-forward/OOS 闸门；`pipeline.py:225-230` 用整份 `market_df` 评价、`:301` 用同一份回测 → **样本内选因子 + 样本内回测**；未来收益 `close.pct_change(h).shift(-h)` 无 purge（`:313,345`）。成本仅 `taker_fee(0.001)*turnover`（`backtest.py:236`）——**无 funding 成本、无滑点**，尽管因子用了 funding_rate。

---

### 4.4 `Alpha-Research-Discovery`（VedantUpasani46）—— 单个文件有亮点，整体无法运行

30 个 alpha（`run_all_alphas.py:41-72`），加密相关约 16 个：reversal×volume / VPIN / Amihud / OFI / realized skew / vol term structure / 跨所价差 / GBM 加密特征 / Google Trends / macro tilt / 链上 / **funding carry** / IV skew / VRP / News Velocity / PCR。

**加密原生特征清单**（`alpha_08_gbm_crypto_features.py:163-240`）：`funding_ema7 / oi_change_5d / OI加权价格偏离 / micro_basis_5d_ema / 链上活跃地址 / 交易所净流 / IV skew / PCR`——但后几项多为 proxy/stub（`:211-240`）。`Alpha16` 是真实 funding carry 实现（21×8h≈7d EMA、sqrt 阻尼、8h→daily 累加 3 期，`alpha_16_funding_rate_carry.py:99,175-206`），无数据回退**合成 funding**（`:192-202`）。

**最大亮点 `walk_forward.py`**：`WalkForwardValidator(train_window=504, test_window=63, step_size=21, min_train_size=100, embargo_days=5, purge_days=0, mode=ROLLING/EXPANDING)`（`:403-425`）；`split()` 同时实现 **embargo**（`test_start = train_end + embargo + 1d`，`:465`）与 **purge**（`effective_train_end = train_end - purge_td`，`:477-481`），引用 López de Prado (2018) §7.4；并实现 **Deflated Sharpe Ratio**（Bailey & López de Prado 2014，Euler-Mascheroni 期望最大 SR + Lo(2002) 非正态方差，`DSR>0.95`，`:230-289`）。

**但 30 个 alpha 实际并未用它**：每个用 `walk_forward_split(index, IS_FRACTION=0.70)` 朴素 70/30（`alpha_01:105,267`），`Fama-MacBeth` t-stat / `Kupiec` POF 在 `alpha_01:299-301,356-380`，`TC_BPS=5.0`（`:104`）。**致命一致性问题**：30 个 alpha 都 `from data_fetcher import (walk_forward_split, fama_macbeth_regression, information_coefficient, CRYPTO_UNIVERSE, ...)`（`alpha_01:69-84`），而 `data_fetcher.py` 只有 5 个类、**无这些符号**；`__init__.py:12-20` 引用的 `framework/` 目录不存在 → **当前无法运行**。

组合层 `alpha_combiner.py` 实装 5 种：`EqualWeight / ICWeighted / Ridge(L2) / LightGBM / OptimalShrinkage(Ledoit-Wolf)`（`:121,146,278,396,545`）；README 声称的 HMM regime-aware 与"单 alpha 上限 15%"源码未找到。

### 4.5 加密 vs A 股：源码里真实存在的特殊设计

| 议题 | 业界处理 | 我们现状 |
|---|---|---|
| **7x24 年化** | `periods_per_year=24*365`（`crypto-factor-lab/mining.py:239`）；AlphaForge 残留 `sqrt(252)` 不一致 | ml4t 回测按日频，未显式处理 8760 |
| **funding 双重身份** | 既是因子（`funding_rate_zscore` 且 `shift(1)` 防泄露）又是成本（`execution.py:96`、`AlphaForge/backtest.py:123-125`） | ✅ 成本侧已有 `build_funding()`；**因子侧完全没有 funding 字段** |
| **8h 结算对齐** | 按 UTC 结算时间对齐 + `round("1h")` + ffill（`binance.py:349-365`） | 简化为固定日频 `rate`，未用真实 funding 历史 |
| **无涨跌停 → 爆仓** | 4 个仓库**均未见真实爆仓模拟**，AlphaForge 仅声明参数未引用 | ml4t 有 `allow_leverage` 但无维持保证金/强平 |
| **高换手成本** | 三段式 `fee+half_spread+slippage`（`execution.py:16-18`）、渐进执行 `execution_alpha=0.20`、非重叠调仓 | ✅ 手续费+滑点+funding 已有 |
| **资产池变化 / 幸存者偏差** | 只有 `liquidity_universe` 是 point-in-time（`universe.py:8-23`）；`alpha-mining` 用当下 top_n 明确有偏差 | 固定 20 币宇宙，**未做 point-in-time** |
| **OI / 基差 / 清算 / PCR** | AlphaForge 数据层最全但因子层零实现；ARD 多为 proxy/stub | 无 |
| **非方向因子语义** | `DEFAULT_SIGNAL_SEMANTICS` 把 vol/funding 标 `neutral`，等专用协议（`suite.py:30-37`） | 所有因子一律套多空回测 |

---

## 5. 框架源码层详解（四）：评价层、GP 范本与计算引擎

### 5.1 alphalens —— 单因子"事后体检"，无过拟合防护

#### 5.1.1 point-in-time 对齐

`get_clean_factor_and_forward_returns`（`alphalens/utils.py:666-677`）默认 `quantiles=5, periods=(1,5,10), max_loss=0.35, cumulative_returns=True`，内部 `compute_forward_returns → get_clean_factor`（`:827-839`）。文档明确要求 `prices` 每个时间戳是"因子计算后下一个可用价格（买入价）"且不得用于该时刻因子计算（`utils.py:713-722`）。实现：`prices.pct_change(period).shift(-period).reindex(factor_dateindex)`（`:286-293`）把未来 N 期收益回贴到当期因子，再 `df.reindex(factor.index)`（`:335`）。

- **overlapping 持仓**由 `positions()`（`performance.py:407-448`）处理：`trades_idx ∪ (trades+period)` 并集，维护 `active_weights` 队列按 `expire_ts` 弹出、并存权重归一化。
- **分桶** `quantize_factor`（`utils.py:85-170`）按 `groupby([date])` 逐截面 `pd.qcut(...,labels=False)+1`；`zero_aware` 正负各分一半桶。
- **去重：未找到显式 `drop_duplicates/duplicated`**。清洗链是 `np.isfinite` → `dropna()` → `max_loss=0.35` 丢弃率阈值（`utils.py:595,627,643,647-661`）。`non_unique_bin_edges_error`（`:49-81`）是分桶失败兜底，非索引去重。

#### 5.1.2 指标算式（我们 evaluator 缺的部分）

| 指标 | 定义 | 位置 |
|---|---|---|
| IC | 按 date 分组 `stats.spearmanr(x,f)[0]` | `performance.py:56-72` |
| Risk-Adjusted IC | `IC mean / IC std` | `plotting.py:166-179` |
| `mean_return_by_quantile` | 先 `demean_forward_returns`（`utils.py:379-416`）再按分位聚合，`std_err = std/sqrt(count)` | `performance.py:453-517` |
| 分位价差 | `mean_ret(upper) − mean_ret(lower)`，联合标准误 `sqrt(std1²+std2²)` | `performance.py:520-565` |
| `factor_returns` | `factor_weights`（`:129-205`，多空去均值 / 等权 ±1 / 分组中性）乘前向收益按 date 求和 | `performance.py:208-255` |
| alpha/beta | `OLS(组合收益 ~ 常数 + 市场等权收益)` | `performance.py:258-330` |
| 换手 | **未找到 `factor_turnover` 函数**；实际是 `quantile_turnover`（`:568-598`）与 `factor_rank_autocorrelation`（`:601-639`） | `tears.py:150-172` |
| IC 汇总 | `Mean/Std/Risk-Adjusted/t-stat(ttest_1samp)/Skew/Kurtosis` | `plotting.py:166-179` |

**明确无过拟合防护**：全包未命中 Bonferroni / BH-FDR / White Reality Check / Deflated Sharpe / PBO / bootstrap / out-of-sample / purge / embargo；只有单因子 IC 序列的 t 检验。且 `filter_zscore` 去极值自带前视偏差警告（`utils.py:244,770`）。

### 5.2 WorldQuant Alpha101 —— 算子语义与我们真正缺的东西

`101Alpha_code_1.py` 是 pandas wrapper（`get_alpha` 实际只调用约 75 个 alpha，`:187-271`）；`code_2.py` 是 Quantopian CustomFactor 版，注释保留官方公式原文。

顶部 18 个 `def`（`code_1.py:9-184`）扣除 2 个回调得 **16 个封装算子**：`rank`（截面百分位）、`delay`/`delta`、`correlation`/`covariance`、`ts_rank`、`ts_sum`/`sma`/`stddev`/`product`、`ts_min`/`ts_max`、`ts_argmax`/`ts_argmin`、`scale`（`x*k/sum|x|`，截面）、`decay_linear`（线性加权，权重 `i/(period(period+1)/2)`）。公式内联原语：`SignedPower`、`adv{d}`（平均美元成交量）、`sign/abs/log`、幂 `^`、条件三元 `? :`、`||`、比较。**合计约 24 类算子**。

> 存疑：`code_1.py:131-132` 注释掉了 `rank(axis=1,pct=True)`，实际 `df.rank(pct=True)` 未按截面分组，**实现与 "Cross sectional rank" 注释不符**。我们的 `cs_rank` 用 `rank(axis=1,pct=True)`（`operator_runtime.py:124-128`），比它更正确。

**逐项对照我们 59 算子后的真实缺口**：

| 缺失 | 说明 | 影响 |
|---|---|---|
| **条件/分支算子** | 三元 `? :`、`if_then_else`、逻辑或；101 公式大量使用。我们只有 `greater/less`（返回指示值而非分支） | 无法表达 `x>0 ? a : b` 类非线性 |
| **通用二元幂 `pow(x,y)` / `^`** | 如 `(high*low)^0.5`、`open^5`、`x^rank(...)`（`code_2.py:1223,1597,2103`）；我们 `signed_power` 是**单参数标量指数**（GP 采 1/2/3，`gp_miner.py:47`） | 表达式空间受限 |
| **输入变量集** | `TERMINALS_DEFAULT=[close,open,high,low,volume]`（`gp_miner.py:18`）；缺 `vwap / adv / returns / amount / cap` | 多数 Alpha101 公式无法复现 |
| `ts_argmaxmin` 派生 | 次要 | — |

已有等价：`delay≈ts_ref`、`decay_linear≈ts_decay_linear`、`scale≈_scale`（`operator_runtime.py:315-320` 已注明即 Alpha101 scale）、`signed_power`、`rank≈cs_rank`、`corr/cov/ts_rank/ts_argmax/ts_argmin/ts_min/ts_max/ts_sum/ts_product/ts_std/delta` 全有。注意 `ts_max2/ts_min2` 是逐点二元（`:252-258`），与 Alpha101 `max(x,d)` 时序窗口语义不同。

### 5.3 gplearn —— 遗传算子的参考实现（GP 算法对照）

> **版本差异**：本地 gplearn 的 `_Program` 已拆到 `gplearn/_program.py`，遗传算子**不在 `genetic.py`**。

`genetic.py:_parallel_evolve`（`:39-155`）按累积概率五选一（`:80-116`），默认 `p_crossover=0.9, p_subtree=0.01, p_hoist=0.01, p_point=0.01`（`:183-186,380-389`）：

| 算子 | 实现 | 位置 |
|---|---|---|
| `crossover` | `get_subtree` 按 Koza 90% 函数/10% 终端选点，拼 `program[:start] + donor子树 + program[end:]` | `_program.py:509-514,530-561` |
| `subtree_mutation` | headless chicken：先 `build_program` 随机树再 `crossover` | `:563-587` |
| **`hoist_mutation`** | 子树内再取子树上提，注释明确 "helps to control bloat" | `:589-617` |
| `point_mutation` | `p_point_replace=0.05` 逐节点；函数按 **同 arity** 替换（字典 `genetic.py:357-362`）；终端换成变量或 `uniform(*const_range)` 常数 | `:619-660` |
| `reproduction` | `copy(self.program)` | `:526-528` |

**锦标赛**：`random_state.randint(0,len(parents),tournament_size=20)` 后按 `fitness_` 取 argmax/argmin（`genetic.py:59-67`）。

**关键默认值**（`genetic.py:168-195`）：`population_size=1000, generations=20, tournament_size=20, const_range=(-1,1), init_depth=(2,6), init_method='half and half', function_set=('add','sub','mul','div'), parsimony_coefficient=0.001, max_samples=1.0`。

- `build_program`（`_program.py:165-223`）用 `terminal_stack` 构造**前缀表达式**，half-and-half 随机 full/grow
- **ephemeral constants**：`const_range` 内随机 float 终端（`:204-213`），`execute` 用 `isinstance(node,float)` 判定并 `np.repeat` 广播（`:342-388`）
- `raw_fitness = metric(y, execute(X), sample_weight)`（`:441-467`）；**`fitness = raw_fitness − c*len(program)*metric.sign`**（`:469-487`）；`'auto'` 简约系数取 `Cov(length,fitness)/Var(length)`（`genetic.py:507-509,702-704`）
- `max_samples<1` 的 `oob_fitness_`（`genetic.py:141-151`）是**样本内子采样，不是时间切分**
- `SymbolicTransformer` 按 `|corrcoef|` 迭代剔除最相关对中较劣个体（`:563-593`）——**因子库去冗余可直接抄**
- `make_function`（`functions.py:49-121`）校验 arity、输出 shape、对全 0 / 全 −1 输入检查闭包性；内置 17 算子 + 5 个保护算子（`_protected_division/sqrt/log/inverse/sigmoid`，`:124-167`）

**未找到**子代树深硬截断，bloat 仅靠 hoist + parsimony 软控制。

### 5.4 gpquant —— 60 算子 + 回测型 fitness

`Function`（`gpquant/Function.py:9-22`）带 `arity/is_ts/fixed_params`；`is_ts>0` 表示节点需要时序窗口常数，`fixed_params` 强制绑定 H/L/C/V 列。`function_map` 实测 **60 项**（`:507-568`）：13 一元 + 7 二元 + 3 条件 + ~37 时序（含 `regression_beta/autocorr/dema/kama` 及 AROONOSC/WR/CCI/ATR/NATR/ADX/MFI 技术指标）。**滚动用 `np.lib.stride_tricks.sliding_window_view`（`:25-35`）而非 `pd.rolling.apply`**，`min_periods=int(d/2)`（`:236`）。

**适应度不是 IC，是回测净值指标**（`Fitness.py:14-61`）：`ann_return` / `sharpe_ratio` / MAE / MSE / `direction_accuracy`；`Backtester` 走 `factor→signal→asset`（含滑点 A/B、手续费、冲击成本，`Backtester.py:6-34`）。GP 参数（`test.py:12-40`）：`population_size=2000, tournament_size=20, generations=50, init_depth=(6,8), const_range=(1,20), metric='sharpe ratio', parsimony_coefficient=0.005`。

`SyntaxTree` 用 `Node(is_ts)` 区分时序常数、`ttl_shift` 记录 warm-up 并在 `execute` 前置 NaN（`SyntaxTree.py:103,190,226-236`）——**与 qlib 的 `get_extended_window_size` 是同一问题的两种解法**。

**过拟合防护同样薄弱**：`test.py:42-43` 仅 `[:400]/[400:800]` 固定单次切分，未找到 walk-forward / purge / 多重检验。

### 5.5 KunQuant-rs —— 编译执行层（据实说明）

> **本地仓库是 `kunquant_rs v0.3.0` 的 Rust FFI 绑定层**（`Cargo.toml`、`README.md:5-9`、`src/lib.rs:3-6`），依赖外部 `libKunRuntime.so`（`build.rs:25`）。表达式编译 / 算子定义 / 代码生成 / SIMD 实现的源码**不在本仓库**，属外部 KunQuant Python/C++ 库。

可引用证据：
- `generate_test_factor.py:15-24` 用 `KunQuant.Op.Builder/Input/Output` 构建 DAG、`Stage.Function` 封装，`cfake.compileit([...], KunCompilerConfig(input_layout="TS", output_layout="TS"/"STREAM"))` **编译成 .so**（`:82-84,102-104,117-120`）
- `output_layout="STREAM"`（`:119`）、`StreamContext`（`src/stream.rs:26-33`）、C API `kunCreateStream/kunStreamPushData/kunStreamRun/kunStreamGetCurrentBuffer`（`src/ffi.rs:41-55`）
- **SIMD 约束**：`src/batch.rs:20-26` 明确 "In STs memory layout, `num_stocks` must be a multiple of 8 to enable SIMD vectorization"；`buffer_handles` 缓存避免重复查表（`src/stream.rs:31-32,86-90`）；行主序连续 `f32` 零拷贝交给 C 层（`src/buffer.rs:17,187`）

**与我们 `evaluate_ast` 的结构性差距**：`core/miner/operator_runtime.py:594-629` 是递归解释执行——逐节点把 `window/p` 作 kwargs 传给 `spec["func"]`（`:610-621`），算子内部 `series.rolling(w).apply(...)`（如 `_ts_decay_linear :106-117`），强制返回 pandas 对象（`:625-628`）；每个候选全量重算，无编译、无公共子式复用、无 SIMD，仅顶层 `ThreadPoolExecutor` 并行候选（`core/evaluation/evaluator.py:169-174`）。

| 维度 | KunQuant | FactorMiner |
|---|---|---|
| 执行 | AOT 编译成机器码、module 复用 | 递归解释 |
| 计算 | 连续 `f32` 矩阵 + 8 股对齐 SIMD | pandas 对象 + `rolling.apply` |
| 复用 | STREAM 增量、buffer 复用 | 每候选全量重算 |
| 窗口 | 编译期固化 | 运行期传参 |

### 5.6 `trade-learn` —— 回测/研究平台（非挖掘器，作下游互补）

> 上游：`MuuYesen/trade-learn`（v0.3.x candidate，2026-11-14 经 GitHub API tarball 下载，本地路径 `D:\code\github\trade-learn`，Apache-2.0）。

**定位澄清（最关键）**：trade-learn **不是因子挖掘器**。其文档把「因子生成 / 挖掘」明确列为**待实现目标**（docs/guides/factor.md 原文："因子生成、单因子/多期限/多因子评价汇总、挖掘/筛选…须明确区分"）。当前只做**因子评估**（alphalens 风格 IC/分位收益）与**因果特征筛选**，外加高性能回测。它与 FactorMiner 的「挖因子」不在同一环节——它是**下游验证层**。

**核心能力（源码实证）**：
- **Rust 事件驱动回测内核**：对比 Backtrader Oracle 基准，单标的 28x、1000 标的大规模调仓 **119x** 加速（55 万 Bar 与 504 万 Bar 基准，最终权益/成交/闭环交易 EXACT 对齐，benchmark 脚本与数据均随仓库提供）。Engine（对齐 Backtrader 语义，Analyzer/Sizer/Signal 齐备）/ Lite（类 backtesting.py，`target_weights` 直连 ML 权重）双 API 共用同一 Runtime。
- **因果特征筛选 `CausalSelector`**：内置 PC / FCI（来自 causal-learn），用于剥离"伪相关"因子；但文档明确写"候选关系，不证明因果效应、收益或样本外有效性"——是筛选不是银弹。
- **因子评估**：`tradelearn.factor` 提供 alphalens 风格单/多因子 IC、分位收益、换手、多周期报告；并修正了 Alpha101 `alpha002/003` 在窗口不足时把未定义相关系数填零的旧实现（改为 NaN）。
- **研究基础设施**：内置 JupyterLab + MLflow，串起"特征→因果筛选→模型→权重→回测→报告"的可复现流水线；兼容 TA-Lib / TDX / TradingView / PyneCore 指标口径。
- **KunQuant 后端**：`KunQuant==0.1.12` 提供 Alpha101（实注 82 项，非全部 101）/ Alpha158（158 特征）预定义因子，走原生 `KunQuantBatch` / `KunQuantStream` 编译执行（与 5.5 编译执行层同源）。

**成熟度风险（据实）**：版本 0.3.x candidate，文档大量"未发布源码已移除""待实现"字样；多处引用仓库外的内部规范 `docs/系统规范/交易/TradeLearn框架规范.md`（不在开源包内），提示可能是某内部项目的部分开源。pip 包 `0.3.4` 与文档存在偏差，接入前需小样本跑通验证实际可用范围，勿把发行当能力证明。

**与 FactorMiner 的关系（结论）**：
- **不替代** FactorMiner 的挖掘引擎——因子生成在 trade-learn 仍待实现，而 FactorMiner 四大范式（GP/RL/LLM/NN）正是干这个的。
- **可作下游验证/回测层**：把 FactorMiner 挖出的候选因子喂给它，用 (a) Rust 快回测做大规模组合验证、(b) `CausalSelector` 因果筛选（直接补 FactorMiner 尚未实现的软去重/抗伪相关缺口）、(c) MLflow 实验追踪、(d) alphalens 多周期 IC 报告。
- 两者都引用 KunQuant，技术栈契合，可共用编译执行底座。

---

## 6. 横向对比矩阵

### 6.1 算子种类与数量

| 框架 | 可调用算子数 | 参数化窗口 | 截面算子 | 条件/分支 | 常量终端 | 加密专用字段 |
|---|---|---|---|---|---|---|
| **FactorMiner** | **59** | ✅ 30 个带参（`OPERATOR_PARAM_SPECS`） | ✅ 8（`cs_*`） | ❌ 仅 `greater/less` | ✅ ephemeral | ❌ 仅 OHLCV |
| qlib | 52 注册名 / 47 类 | ✅ 统一 `N` 语义 | ⚠️ 算子层无，Processor 层有 | ✅ `If/Gt/And/Or` | ✅ 数值常量 | ❌ |
| AlphaGen | 28 定义 / 22 启用 | ✅ `DeltaTime` token | ⚠️ 仅 `CSRank`（默认关闭） | ❌ | ✅ 14 常量 | ❌ |
| AlphaForge | 31 定义 / 23 启用 | ✅ 7 窗口 | ❌ | ❌ | ✅ | ❌ |
| gplearn | 17 + 5 保护 | ❌ | ❌ | ❌ | ✅ `const_range` | ❌ |
| gpquant | 60（`function_map`） | ✅ 时序常数 | ❌ | ✅ 3 个条件算子 | ✅ | ❌ |
| Alpha101 | ~24 类 | ✅ `d` | ✅ `rank/scale` | ✅ 三元 `? :` | ✅ | ❌ |
| crypto-factor-lab | 21 op | ✅ 7 窗口 | ❌ | ❌ | ✅ `constant` | ✅ `funding_rate` |
| alpha-mining-crypto | 13 核心 + 40 窗口 + 40 组合 | ✅ 多窗口 | ✅ 全转 pct rank | ❌ | ❌ | ✅ 6+ funding 变体 |
| AlphaForge-Crypto | 3 示例 | ❌ | ❌ | ❌ | ❌ | ⚠️ 数据层有、因子层无 |

**结论**：**算子数量我们已进入第一梯队（59，仅次于 gpquant 60）**，但有三类结构性缺口：① 条件/分支与通用二元幂；② 输入终端只有 OHLCV（缺 `vwap/adv/returns/amount` 与加密的 `funding/OI/basis/liquidations`）；③ 截面算子虽 8 个，但仅在 `cross_asset` 模式下生效，单标的退化为恒等，缺分组/行业中性化。

### 6.2 因子挖掘方法与目标函数

| 框架 | 搜索算法 | 优化目标 | 组合层 | 表达式表示 |
|---|---|---|---|---|
| **FactorMiner** | GP(子树交叉/锦标赛/变异/简约) + RL(REINFORCE) + LLM + NN | 样本内外 RankIC 几何混合 fitness | **样本内一次岭回归、固定权重** | AST dict |
| qlib | **无**（平台） | — | 模型层（LGBM/NN） | 字符串表达式树 |
| AlphaGen | MaskablePPO + LSTM/Transformer + 动作掩码 | **加入池后组合 IC 增量** | AlphaPool（容量/去冗余/联合优化/L1） | RPN 栈 + token |
| AlphaForge | DCGAN 生成器 + Conv 预测器 + 可微掩码 | predictor 打分 − 多样性惩罚 | **逐期 top-N + `lstsq` 重估权重**（shift=21） | token 序列 → one-hot |
| RD-Agent | LLM 假设→规格→代码 + CoSTEER 自纠错 | qlib 回测 IC / 年化 / 回撤 | SOTA 库合并回测 | Python 代码（`result.h5`） |
| LLM-Framework | 4 算子 LLM 进化 + 3 island | 文本 gate → validation IC → **统计闸门** | 等权/留一法 | DSL AST + sanitizer |
| CosmosAlpha | MCTS(UCT) + Graph Flow + Pareto | 三维 Pareto `(|ric|,-std,top20)` | LightGBM | 类型化 DSL |
| crypto-factor-lab | 确定性枚举 + 受约束 LLM 提议 | validation RankIC + FDR + 随机零假设 | 等权 + 留一法 | 21-op DSL |
| alpha-mining-crypto | 模板枚举 + 两两组合 | `0.7*|spread_ir|+0.3*|ic_ir|` | `score×direction` 加权 | 硬编码公式 |
| gplearn | 5 种遗传算子 + 锦标赛 | `metric` − `c*len(program)` | — | 前缀 program 列表 |
| gpquant | GP（同 gplearn） | **回测 Sharpe** | — | `SyntaxTree` |

**关键差异**：我们的搜索目标是**单因子 fitness**、组合是**事后独立**的一步；AlphaGen/AlphaForge 的搜索目标直接是**组合增量/代理打分**，搜索与组合耦合。这是"挖出一堆 IC 还行但组合起来亏钱"的结构性原因之一。

### 6.3 防过拟合闸门（本次调研的核心）

| 框架 | purge/embargo | 三段切分 | FDR/Holm | HAC/NW t | DSR | 随机零假设 | 多重检验分母 | 成本入 fitness | 一次性留出 | walk-forward |
|---|---|---|---|---|---|---|---|---|---|---|
| **FactorMiner** | ❌ | ⚠️ mine/test 两段 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌（仅回测末端） | ❌ | ❌ 单次切分 |
| qlib | ❌ | ✅ train/valid/test | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 含/不含成本收益 | ❌ | ✅ `Rolling(step=20)` |
| AlphaGen | ❌ | ✅ 三段 OOS | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 每轮评估多段 |
| AlphaForge | ⚠️ 回带 2 年 head | ✅ 按年 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `shift=21` |
| RD-Agent | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 反馈含 with_cost | ❌ | ❌ |
| **LLM-Framework** | ✅ | ✅ | ✅ BH/Holm/RW | ✅ 21 滞后 | ✅ | ✅ 随机语法池 | ✅ 全部尝试 | ⚠️ 承认无成本组合 | ✅ consumed-before-read | ✅ |
| **crypto-factor-lab** | ✅ `purge_bars=horizon` | ✅ | ✅ BH | ✅ `hac_lags=1` | ❌ | ✅ 100 次置换 | ⚠️ 候选预算 | ✅ 最终确认含成本 | ✅ **holdout ledger** | ✅ |
| AlphaForge-Crypto | ❌ | ❌ | ✅ 单列 BH | ✅ | ❌ | ⚠️ bootstrap 未接入 | ❌ | ❌ | ❌ | ⚠️ 无 purge |
| Alpha-Research-Discovery | ✅ 单文件实现 | ⚠️ 70/30 | ❌ | ❌ | ✅ 单文件实现 | ❌ | ❌ | ✅ `TC_BPS=5` | ❌ | ⚠️ 文件实现但未用 |
| alphalens | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| gplearn / gpquant | ❌ | ⚠️ 固定单次切分 | ❌ | ❌ | ❌ | ❌ | ❌ | gpquant ✅ 回测型 | ❌ | ❌ |

> **读法**：我们当前唯一领先业界平均的，是"OOS RankIC 参与 fitness"这一点（P0 已做）。但**没有任何显式的统计显著性闸门**，而且严格 OOS 已经被查看过一次。表中最右四列（随机零假设 / 试验次数分母 / 成本入 fitness / 一次性留出）是我们与最严谨两个框架的完整差距。

---

## 7. FactorMiner 现状基线（源码级，2026-11-14）

### 7.1 算子层

`core/miner/operator_runtime.py` 的 `BUILTIN_OPERATORS` 实测 **59 个**（此前文档写 58，本次核定更正）：

| 类别 | 数量 | 明细 |
|---|---|---|
| 时序 `ts_*` | 30 | `argmax argmin corr count_up cov decay_linear delta ema kurt ma_ratio mad max max2 mean median min min2 momentum product quantile rank realized_vol ref regression rsi skew std sum var zscore` |
| 截面 `cs_*` | 8 | `mad max mean min neutralize rank sum zscore` |
| 二元算术 | 4 | `add sub mul div` |
| 逐点/其它 | 17 | `abs clamp cos cumsum exp greater inv less log log1p_abs scale sigmoid sign signed_power sin sqrt tanh` |

`OPERATOR_PARAM_SPECS` 为 **30 个算子**提供统一参数采样（`window/p/n/q/lo/hi`），GP 变异/交叉走 `sample_operator_params()`。截面算子用 `axis=0` 显式对齐（修复过列爆炸 bug）。`evaluate_ast`（`:594-629`）是递归解释执行、节点级 kwargs 透传、强制 pandas 对象返回。

### 7.2 挖掘层

| 范式 | 文件 | 算法 |
|---|---|---|
| GP | `core/miner/paradigms/gp_miner.py` | 随机初始树（ephemeral 常数 + 随机窗口）→ 子树交叉 + 锦标赛 + 变异 + 精英保留 + 简约度惩罚 `base/(1+c·depth)`（`:84-87`） |
| RL | `core/miner/paradigms/rl_miner.py` | REINFORCE 策略梯度：softmax 策略采算子序列 → 折叠合法 AST → fitness 作 reward、批次均值基线、熵正则；`RLFactorExpression` 让 RL 因子可 compute |
| LLM | `core/miner/paradigms/llm_miner.py` | 同一 system prompt 重复 `batch_size` 次 + 累积 success/failed reflection；**无结构化 critique、无执行修复、无去重** |
| NN | `user_workspace/custom_miners/my_custom_nn.py` | MLP 训前向收益，隐层通道=候选因子，多样性正则 + OOS 选择 + 可移植 artifact |

终端集 `TERMINALS_DEFAULT=[close,open,high,low,volume]`（`gp_miner.py:18`）。

### 7.3 评价层

`core/evaluation/evaluator.py` 的 `_build_fitness()`（本次已逐行核对）：

```
fitness = 100 × blended × cons_mult × shrink × penalty × tp × parsimony
blended  = |RIC_in|^(1-λ) · |RIC_oos|^λ        λ = oos_exponent = 0.5
cons_mult= 1.0 或 0.3（OOS 符号翻转时）      shrink = sqrt((n-k)/n)，仅 use_oos 时生效
penalty  = (coverage/coverage_floor)^2        tp = 换手惩罚（默认 0.0 关闭）
```

`evaluate()` 会取 `get_test_data()/get_test_returns()`；**无 `test_period` 时数值与旧公式精确相等**（已验证）。指标字典含 `RankIC_oos / IC_oos / oos_consistent / oos_enabled / shrinkage / coverage / turnover_penalty`。

### 7.4 组合层与回测层

- `core/miner/expressions.py`：`LinearCombinedFactor` + `fit_linear_combination()`，**只在 in-sample 做岭回归 `w=(XᵀX+λI)⁻¹Xᵀy`，之后固定**；`BaseFactorMiner._finalize()` 挑 top-K 低相关子因子组合并前置（GP 默认开）。
- `core/backtest/backtest_engine.py`：ml4t 事件驱动、次日收盘成交、PERCENTAGE 手续费 + 滑点 + `funding_df`；`infer_orientation()` 从快照推断方向；本次新增 `--orientation-db-root` 支持**方向从样本内冻结**，`summary` 留痕 `strict_oos`。
- `user_workspace/experiment_tools/recompute_oos_factors.py`：`FactorCompiler` 重算 OOS 因子值到独立 `factor_db_oos`；warm-up 靠手写 `WARMUP_START="2024-07-01"`。

### 7.5 真样本外验证结果（本次调研的直接动机）

| 场景 | 总收益 | 零成本 | Sharpe |
|---|---|---|---|
| 样本内 2024 | -17.87% | -10.41% | -0.55 |
| OOS 2025H1（泄漏版方向） | -6.59% | -4.19% | -0.24 |
| **OOS 2025H1（严格，方向冻结自 2024）** | **-11.18%** | **-7.37%** | **-1.14** |

逐因子 `|RankIC|` 均值 0.0568 → 0.0280（衰减至 49%），**2/6 方向翻转**。零成本仍亏 → 因子无 alpha，非成本问题。报告见 `user_workspace/reports/oos_validation_2025H1.md`。

**结论：我们的问题不在算子数量、不在范式数量、也不在回测实现，而在于「搜索了大量候选却没有任何统计显著性折减」，以及「OOS 集可被反复查看」。**

---

## 8. 值得借鉴清单（按优先级，含具体落点）

### P0 —— 直接治过拟合，必须先做

#### P0-1｜一次性留出揭示账本（holdout ledger）

- **来源**：`crypto-factor-lab/factor_lab/catalog.py:282-299`（`latest_revealed_holdout_end`）、`service.py:900-906`（重叠即 `raise ValueError`）
- **为什么最紧迫**：我们的严格 OOS（2025H1）已被用于下结论；继续据其调参，它就退化成验证集，而 2025H2 之后的数据虽在盘上但会不断被"看一眼"。
- **落地建议**：`core/evaluation/` 新增 `holdout_ledger.py`，以 `(dataset_id, holdout_start, holdout_end, run_id, metrics_hash)` 持久化到 `user_workspace/state/holdout_ledger.json`；`core/commands/backtest.py` 在 `strict_oos=True` 时先查账本，重叠直接拒绝并提示"补一段未见过的新数据"；报告里固定打印"本 dataset 已揭示次数"。这是**半天可落地、收益最大**的一项。

#### P0-2｜多重检验校正与试验次数分母

- **来源**：`LLM-Alpha-Mining-Framework/factors/multiple_testing.py:66-90`（NW t）、`:190-213`（BH-FDR）、`:216-249`（Romano-Wolf）、`:267-306`（`evaluate_pool` + 随机公式零分布）、`factors/experiment_trace.py:23,135`（全部尝试当分母）、`evo/comparison_audit.py:7-26`（Holm）
- **落地建议**：`core/evaluation/evaluator.py` 增加 `significance.py`：
  1. `ExperimentTrace` 记录**每一次候选评估**（含被丢弃的），作为 `num_trials` 分母——当前我们完全没记这个数；
  2. 把 RankIC 序列的朴素 t 换成 **Newey-West HAC t**（重叠标签下必须）；
  3. 对一代候选做 **BH-FDR**（`fdr_q=0.05`），并给每个因子打 `fdr_rejected` 标记；
  4. **随机公式零分布**：用同等数量随机 AST  expr 的 `max|t|` 作门槛（`passes_random_max`），直接回答"这么搜纯噪声能有多显著"；
  5. 报告里写死 `num_trials` 与校正后 p 值，杜绝对照缺失。
- **对应我们的痛点**：LLM 范式「同一 prompt 重复 `batch_size` 次」把试验次数线性放大，却没有任何折减机制。

#### P0-3｜Deflated Sharpe Ratio + promotion gates

- **来源**：`backtest/validation.py:44-96`（PSR / `expected_max_sharpe` / DSR）、`:130-140,166-280`（8 道 gate：DSR>0.95、8 子期≥6 正、净 Sharpe>0.4）；`Alpha-Research-Discovery/walk_forward.py:230-289`（独立实现可交叉验证）
- **落地建议**：`core/backtest/backtest_engine.py` 的 `summary` 增加 `deflated_sharpe(num_trials, skew, kurt)` 与 `promotion_gates` 结果；把"OOS Sharpe 是单点数字"改成"带试验次数折减的概率闸门"。我们的 -1.14 若有 DSR，结论会更早、更硬。

#### P0-4｜purge + embargo + 多段 roll-forward

- **来源**：`crypto-factor-lab/factor_lab/mining.py:40-61`（`TimeSplit.segments`）、`validation.py:18-72`（`walk_forward_factor_evaluation(purge_bars=horizon)`）、`Alpha-Research-Discovery/walk_forward.py:403-481`（embargo + purge 双实现）
- **落地建议**：`core/data_feed/real_client.py` 的 `mine_period/test_period` 切片加 `purge_bars = forward_horizon`（我们的标签是隔日收益，至少 1~2 根）；`core/evaluation/` 支持多段 OOS 并报告**各段 RankIC 分布**（而非单值），消除"挑到一段好 OOS"的选择偏差。同时把 `recompute_oos_factors.py` 的手写 `WARMUP_START` 换成从算子数据依赖自动推导（见 P1-1）。

#### P0-5｜组合层升级：从固定岭回归到容量池 / 逐期权重

- **来源**：`AlphaGen/alphagen/models/linear_alpha_pool.py:61-104,173-189,285-404`；`AlphaForge/combine_AFF.py:160-243`
- **落地建议**（分两步，避免一次性引入时变权重的过拟合风险）：
  1. **先做静态版 AlphaPool**（`core/miner/expressions.py` 新增 `AlphaPoolFactor`）：容量上限、mutual-IC `>0.99` 拒收、每加入一个新因子重优化全部权重、目标改为 `wᵀMw − 2wᵀc + 1 + α‖w‖₁`（直接优化组合 IC + L1 稀疏），保留「权重只在 in-sample 拟合」纪律；
  2. 待 P0-1~P0-3 的统计闸门落地、且重新挖掘的因子在真 OOS 上成立后，**再**考虑每期 `lstsq` 重估权重的动态版（`combine_AFF.py` 的 `shift=21` 防前视必须照抄）。
- **这也正面回答了用户此前问的「逐时间截面回归还做吗」**：业界有成熟实现，但它的前置条件是统计闸门 + 已验证有效的因子池；当前阶段应先做 P0-1~P0-4。

### P1 —— 提升搜索质量与执行效率

| 项 | 来源 | 落点 |
|---|---|---|
| **RL 动作掩码 + PPO**：栈式 RPN + 分类 `validate_op`，保证采样表达式 100% 合法；`MaskablePPO` + LSTM 序列策略替换 REINFORCE | `AlphaGen/alphagen/data/tree.py:42-88`、`rl/env/wrapper.py:60-83`、`scripts/rl.py:252-270` | `core/miner/paradigms/rl_miner.py` |
| **搜索目标改为组合增量 reward**：`pool.try_new_expr(expr)` 的 `new_obj` 作 reward，让搜索与组合耦合 | `AlphaGen/alphagen/rl/env/core.py:67-76` | `rl_miner.py` + `gp_miner.py` |
| **LLM 两段式结构化生成**：假设 → `{name, description, formulation, variables}` 规格 → 代码；配硬接口约束与禁 try-except | `RD-Agent/components/proposal/__init__.py:93-121`、`scenarios/qlib/prompts.yaml:114-134`、`experiment/prompts.yaml:30-33` | `core/miner/paradigms/llm_miner.py` |
| **LLM 执行反馈闭环 + 结构化 critique**：捕获 traceback → critic → 下一轮纠错注入，单轮最多 10 次 | `RD-Agent/components/coder/factor_coder/factor.py:162-208`、`evaluators.py:31-120`、`evolving_strategy.py:95-137` | `llm_miner.py` |
| **SOTA 库 + 0.99 相关去重 + 18-niche MAP-Elites**：治"一个 idea 的 20 种改写" | `RD-Agent/scenarios/qlib/developer/factor_runner.py:46-61`、`LLM-Framework/evo/archive.py:1-25,210-304` | 新增 `core/miner/factor_library.py` |
| **候选筛选 cheap-then-full**：文本/结构 gate 先跑，~90% 拒绝发生在算 DataFrame 之前 | `LLM-Framework/evo/fitness.py:3-6,8-16` | `core/evaluation/` |
| **算子数据依赖契约 + 子表达式 DAG 缓存**：规范化 AST 字符串为 key，共享子树只算一次；warm-up 自动推导 | `qlib/qlib/data/base.py:187,202,209-235`、`ops.py:764-778` | `core/miner/operator_runtime.py`、`recompute_oos_factors.py` |
| **向量化求值**：`sliding_window_view` 替换 `rolling.apply`（尤其 `ts_decay_linear/ts_rank/ts_argmax`） | `gpquant/Function.py:25-35`、`KunQuant-rs/src/batch.rs:20-26` | `operator_runtime.py` |
| **ICIR / 分位价差 / 换手双指标 / 成本入 fitness**：把 `turnover_penalty` 默认开启（需先修 Turnover 归一化 bug） | `qlib/workflow/record_temp.py:272-275`、`alphalens/performance.py:453-639`、`qlib/contrib/report/.../risk_analysis.py:34-38` | `core/evaluation/evaluator.py` |
| **hoist mutation + `Cov(len,fit)/Var(len)` 自适应简约系数** | `gplearn/_program.py:589-617`、`genetic.py:507-509` | `gp_miner.py` |

### P2 —— 领域扩展

| 项 | 来源 | 落点 |
|---|---|---|
| 条件三元 / 通用二元幂 / `vwap,adv,returns,amount` 终端 | `WorldQuant_alpha101_code/code_2.py:200,1223,1597`、`gpquant/Function.py:427-434` | `operator_runtime.py`、`gp_miner.py:18` |
| **加密一等字段**：`funding_rate`（注意 `shift(1)` 防泄露）、`open_interest`、`basis`、`liquidations`、`PCR`、`taker_volume` | `crypto-factor-lab/factor_lab/factors.py:45-60`、`AlphaForge-Crypto/src/data_fetcher.py:446-1466`、`alpha_08_gbm_crypto_features.py:163-240` | `core/data_feed/` |
| **point-in-time 资产池**，消除永续上下架的幸存者偏差 | `crypto-factor-lab/factor_lab/universe.py:8-23` | `core/data_feed/real_client.py` |
| funding 真实 8h 结算历史替代固定 `rate` | `crypto-factor-lab/factor_lab/sources/binance.py:349-365` | `core/backtest/backtest_engine.py:build_funding()` |
| **非方向因子 `neutral` 语义**：vol / funding 类因子不硬套多空回测 | `crypto-factor-lab/factor_lab/suite.py:30-37` | `core/backtest/backtest_engine.py` |
| AST 白名单安全解析（若 LLM 产物改走表达式而非代码） | `qlib/qlib/data/expression_parser.py:50-124` | `core/evaluation/code_sandbox.py` |
| 维护保证金 / 强平模拟（加密无涨跌停） | 业界**均无实现**，属空白机会 | `core/backtest/` |
| 声明式 `字段×算子×窗口` 模板用于过拟合归因消融 | `qlib/qlib/contrib/data/loader.py:72-310` | `user_workspace/configs/` |

---

## 9. 建议的演进路线

```
阶段 0（已完成）  P0 升级：OOS fitness / 59 算子 / 真 GP / 真 RL / 末层岭回归 / 严格 OOS 验证
   ↓  结论：6 因子 OOS 全亏，瓶颈是统计闸门与因子质量，不是算子或范式
阶段 1（1~2 周） 只做「证伪基础设施」，不改挖掘算法
   ├─ P0-1 holdout ledger
   ├─ P0-2 ExperimentTrace(num_trials) + NW t + BH-FDR + 随机公式零分布
   ├─ P0-3 Deflated Sharpe + promotion gates
   └─ P0-4 purge/embargo + 多段 roll-forward
   ↓  验收：把现有 84 个因子重新过一遍闸门，能明确报出"多少个通过"
阶段 2（2~4 周） 用新闸门重新挖掘并再跑一次真 OOS（此时 OOS 必须是未揭示过的新区间）
   ├─ 静态 AlphaPool（容量 + mutual-IC 去冗余 + 组合 IC 目标）
   ├─ RL 动作掩码 + 组合增量 reward
   ├─ LLM 两段式结构化 + 执行反馈闭环 + 因子库去重
   └─ 加密一等字段 + point-in-time 资产池
   ↓  验收：新因子在**未揭示区间**通过 DSR + 随机零假设 + 成本后净收益>0
阶段 3（OOS 成立后） 才考虑表达力增强
   ├─ 逐时间截面回归（时变权重，照抄 shift=21 防前视）
   ├─ 编译执行 / CSE / SIMD
   └─ 多 timeframe、多 horizon、限价盘口数据
```

**纪律**：阶段 2 的每次评估都必须先过 P0-1 的账本；任何"再跑一次看看"都必须消耗一段新的未见数据。这条纪律比任何新算子都重要。

---

## 10. 可信度、局限与反面教训

### 10.1 本次调研自身的局限

- 18 个仓库中 10 个通过 API tarball 下载，**不含 `.git`**，无法核对提交历史与 CI。
- `qlib` / `RD-Agent` 等大仓库只读了与因子挖掘相关的核心路径，未逐行通读全部模块。
- 部分仓库的 README 声明与源码实现不符，本报告以源码为准并逐处标注；但"未找到"不等于"一定不存在"（可能在未扫描到的目录）。
- 未实际运行任何外部框架（需要 qlib 数据、WRDS/CRSP 订阅、WQ Brain 账号、GPU、MongoDB 等），**性能与结果声明均未验证**。
- LLM 组与加密组的仓库多为 2026 年新建、star 很少，其"论文官方实现"宣称多数**未在源码中找到依据**。

### 10.2 值得警惕的反面教训（源码实证）

| 现象 | 证据 |
|---|---|
| README 声称的流水线在源码里不存在 | `AlphaForge-Crypto/src/factor_pipeline/__init__.py:9` import 不存在的 `pipeline.py`；`run_backtest.py:32-41` 全是 TODO；`register_factor` 全仓库无定义 |
| 回测参数定义了但从未使用 | `AlphaForge-Crypto/backtest.py:107` `exec_prices` 定义后未用；`leverage/maintenance_margin/position_fraction` 仅出现在 dataclass |
| 30 个 alpha 引用不存在的符号 | `Alpha-Research-Discovery/alpha_01:69-84` 从 `data_fetcher` import 一堆未定义符号，`framework/` 目录不存在 → 无法运行 |
| 工具数量文档与代码互相矛盾 | `alphaminingv2` README 15 / 16、prompt 12、dispatch 17、注释编到 Tool 16 且编号重复 |
| 年化口径不一致 | `AlphaForge-Crypto/backtest.py:163` 用 `sqrt(252)`，`bootstrap.py:17` 用 365 |
| 样本内选因子 + 样本内回测 | `alpha-factor-mining-crypto/pipeline.py:225-230,301` 用同一份 `market_df` |
| 统计检验为零却在同一样本上反复 sweep/rescue/反向重测 | `WorldQuant-LLM/run_alpha_miner.py:1132-1133,1347` |
| 鼓励"任何微小改进都替换最佳结果"（过拟合倾向） | `RD-Agent/scenarios/qlib/prompts.yaml:187-190` |
| 作者主动承认结果不成立 | `LLM-Framework/README.md:33-37,88-105` —— **这是本次调研里最值得学习的态度** |

### 10.3 一条给 FactorMiner 的元建议

`LLM-Alpha-Mining-Framework` 在 README 里主动写下"经 familywise 校正后没有显著搜索优势"（raw p=0.026 → Holm 后 0.13），并把每次修正前后的数字记进 `docs/audit_history.md`。**建议把我们的 `OOS -11.18% / Sharpe -1.14 / 6 因子全部不通过` 与试验总次数写进一份固定基线报告，作为后续所有实验的分母。** 一个能诚实证伪自己的系统，比多 20 个算子更有价值。

---

## 附录 A：关键源码引用索引

| 主题 | 路径:行号 |
|---|---|
| qlib 表达式抽象 / 数据依赖声明 | `qlib/qlib/data/base.py:13,142-203,209-235` |
| qlib 完整算子 / 参数化窗口 | `qlib/qlib/data/ops.py:37-1616`、`:742-778` |
| qlib 三层缓存 | `qlib/qlib/data/base.py:187,202`、`cache.py:329,489,1198` |
| qlib AST 安全解析 | `qlib/qlib/data/expression_parser.py:50-124,228-323` |
| qlib Alpha158/360 模板 | `qlib/qlib/contrib/data/loader.py:4-310` |
| qlib 截面 Processor | `qlib/qlib/data/dataset/processor.py:300-357` |
| AlphaGen 算子 / RPN 掩码 | `AlphaGen/alphagen/data/expression.py:487-497`、`tree.py:42-88`、`rl/env/wrapper.py:60-83` |
| AlphaGen AlphaPool | `AlphaGen/alphagen/models/linear_alpha_pool.py:61-104,173-189,285-404` |
| AlphaForge 动态组合 | `AlphaForge/combine_AFF.py:139-254` |
| AlphaForge 可微掩码 / 损失 | `AlphaForge/gan/network/masker.py:10-57`、`loss.py:49-60` |
| RD-Agent 主循环 / 两段式生成 | `RD-Agent/components/workflow/rd_loop.py:184-241`、`components/proposal/__init__.py:29-121` |
| RD-Agent CoSTEER / 去重 | `RD-Agent/components/coder/CoSTEER/__init__.py:93-155`、`scenarios/qlib/developer/factor_runner.py:46-61` |
| 多重检验全套 | `LLM-Alpha-Mining-Framework/factors/multiple_testing.py:49-306` |
| DSR + 8 gates | `LLM-Alpha-Mining-Framework/backtest/validation.py:44-280` |
| 冻结评估协议 | `LLM-Alpha-Mining-Framework/evo/evaluation_protocol.py:24-46` |
| 18-niche 行为档案 | `LLM-Alpha-Mining-Framework/evo/archive.py:1-25,210-304` |
| **holdout ledger（已复核）** | `crypto-factor-lab/factor_lab/catalog.py:282-299`、`service.py:900-906,955-966` |
| purge 切片（已复核） | `crypto-factor-lab/factor_lab/mining.py:40-61` |
| purged walk-forward / FDR / HAC | `crypto-factor-lab/factor_lab/validation.py:18-72`、`mining.py:167-189,378-394` |
| 成本模型 / 随机零假设 | `crypto-factor-lab/factor_lab/execution.py:12-102`、`benchmarks.py:19-47` |
| gplearn 遗传算子 / 简约惩罚 | `gplearn/gplearn/_program.py:509-660,469-487`、`genetic.py:59-67,168-195` |
| gpquant 60 算子 / sliding_window | `gpquant/Function.py:25-35,507-568` |
| alphalens PIT / 指标 | `alphalens/utils.py:286-293,666-839`、`performance.py:453-639` |
| Alpha101 算子 | `WorldQuant_alpha101_code/101Alpha_code_1.py:9-184` |
| KunQuant SIMD 约束 | `KunQuant-rs/src/batch.rs:20-26`、`src/ffi.rs:41-55` |
| **FactorMiner 基线** | `core/miner/operator_runtime.py:377-444,594-629`、`core/evaluation/evaluator.py:_build_fitness`、`core/miner/expressions.py`、`core/backtest/backtest_engine.py` |

## 附录 B：本次调研产出与关联文件

- 源码：`D:\code\github\` 下 18 个仓库（清单见 §1.2）
- **增量报告（第 19 个竞品 AlphaMaster）**：`FactorMiner/docs/research/alphamaster_competitor_review.md`
- 本文档：`FactorMiner/docs/research/industry_factor_mining_survey.md`
- 关联报告：`FactorMiner/user_workspace/reports/oos_validation_2025H1.md` + `oos_validation_comparison.csv` + `oos_validation_factor_ic.csv`
- 关联工具：`user_workspace/experiment_tools/recompute_oos_factors.py`、`summarize_oos_validation.py`
- 工作日志：`.workbuddy/memory/2026-11-14.md`

---

*本报告由源码实读整理，所有"未找到"均为在指定路径下检索后的如实结论。若后续要落地 P0 清单，建议先从 §8 的 P0-1（holdout ledger）开始。*



