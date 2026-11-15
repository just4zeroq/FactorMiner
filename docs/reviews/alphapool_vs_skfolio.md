# AlphaPool vs skfolio 1.12：对位、实测与裁决

> 2026-11-16 · 依据：`core/miner/pool.py` 等源码逐行核查 + skfolio 官方文档/PyPI（1.12.16）
> + 本机真实 439 币面板只读实测（`user_workspace/reports/alphapool_vs_crosssection.py`）
> 本轮**未改动任何生产代码**；skfolio 未安装，所有 skfolio 侧结论来自官方文档，实测是其**核心机制的模拟**。

---

## 0. 结论

**它们不是竞品，是流水线上相邻的两层，不该二选一。**

```
候选因子表达式 (GP / LLM / NN)
   │
   ├─[AlphaPool] 去冗余 + 一次岭回归定权 ──→ (T×N) 合成因子分数   ← 本项目已有
   │
   └─[skfolio 1.12 CharacteristicsFactorModel]
         PIT 特征 → 逐日截面回归 → μ, Σ → [MeanRisk 带约束] ──→ (N,) 资产权重
   │
   ↓
[本项目] 分层成本 + apply_promotion_gates + 一次性 holdout       ← 唯一裁判
```

真正重叠的只有一小段：**「多个因子 → 一个预期收益/合成分数」的截面回归**。
AlphaPool 用**全期一次 pooled ridge**（固定权重），skfolio 1.12 用**逐日重估的横截面回归**（时变因子溢价 + 协方差）。
前者不产生权重、不做约束；后者不搜索因子、不做显著性检验。

**裁决（修正上一轮）**：AlphaPool 保留做「去冗余 + 候选筛选 + 固定权重合成」；
skfolio 作为**可选离线对照后端**，只借它两样本项目没有的东西 —— 带约束的资产权重
（换手/成本/流动性/多空阈值）和 Combinatorial Purged CV。
**两者输出都必须回 `apply_promotion_gates`，绝不让 IC 或预期收益当晋升判据。**

---

## 1. 逐项对位

| 维度 | `AlphaPool`（`core/miner/pool.py`） | skfolio 1.12.16 |
|---|---|---|
| **本质** | 因子**合成器**：K 个因子表达式 → 1 个 (T,N) 分数 | 组合**构造 + 因子模型**：收益/特征 → μ,Σ → (N,) 权重 |
| 输入 | 候选因子面板 + forward return (`pool.py:107-116`) | 资产收益 `X (T,N)`；`CharacteristicsFactorModel` 另需 PIT `AssetPanel`（2D 字段 + 3D exposure tensor `(T,N,K)`） |
| 输出 | `combined_panel()` → (T,N) (`pool.py:259-265`)；`report()` 字典 (`:299`) | `ReturnDistribution`（`mu (N,)`, `covariance (N,N)`, `returns` scenarios）+ `FactorModel` 容器 → 喂 `MeanRisk` 等优化器 |
| 权重 | **全期一组固定常数**：`X = column_stack([p.reshape(-1)])`, `y = target.reshape(-1)`，一次 `fit_scale_aware_ridge` (`:221-256`) | **每期重新求解**；`CharacteristicsFactorModel` 每个日期独立做横截面回归，因子溢价随时间变 |
| 去冗余 | IC **时间序列** Pearson \|corr\| > 0.99 拒绝 (`:137-160`)；满容量后按 pooled IC 做替换 (`:189-218`) | 无「因子去冗余」概念；有资产层 `DropCorrelated` / `SelectNonDominated` |
| 目标函数 | ridge 拟合 **raw forward return** 的 MSE（标准化空间 + 无惩罚截距，`ridge_weights.py:15-40`），L1 归一化 (`:250-256`) | 优化器目标可选：最小化风险 / 最大化收益 / 效用 / 比率；20+ 风险度量（CVaR、CDaR、MaxDD、EVaR…） |
| **N(t) 漂移** | 不感知。`combined_panel()` 直接对 (T,N) 加权求和，NaN 前缀被当 0 风险；`real_client` 用并集 reindex | **原生感知**：`active_mask` / `estimation_mask`，逐日回归，"newly listed assets joining the regression as soon as their exposures are available, delisted assets dropping out … No realignment or imputation is needed"；`min_regression_assets = max(2*n_factors, 30)`；不可投资资产在 `return_distribution_` 里以 NaN 表示 |
| 前视防护 | `weight_fit_shift`（默认 0，`:87`）；fit 只在传入的 mine 面板；`ridge_weights.py` 只在 fitting sample 算 mean/std | 统一 as-of 时间索引，`exposure_lag` 默认 1；文档明确要求 `AssetPanel` 与 universe membership 都是 PIT |
| 交易成本 / 换手 | **完全没有**。成本在下游 `portfolio_eval.py:206` 按 `turnover * 标量cost_rate` 扣 | 原生：Transaction Costs、Management Fees、**Turnover Constraints**、Weight / Group / Budget / Tracking Error / Cardinality / Threshold(多空) 约束、L1/L2 正则 |
| 多空组合 | 分位 0.2 等权，long/short 各 0.5，无约束 (`portfolio_eval.py:143-178`) | 约束优化求解，可指定 long/short 阈值与预算 |
| 交叉验证 | 无（依赖下游 walk-forward / holdout） | WalkForward、**Combinatorial Purged CV**、Multi Randomized CV、sklearn GridSearchCV 兼容 |
| **统计闸门** | 无（交给 `significance.py:485`） | **无**：没有 BH-FDR、没有 DSR、没有一次性 holdout ledger、没有净成本后可交易性判据 |
| 因子搜索 | 上游 GP/LLM/NN 产出候选，AlphaPool 只做筛选与合成 | **不搜索因子**；内置 46 descriptors / 17 家族是股票基本面（BookToPrice、LogMarketCap、SalesToPrice…），加密无财报/市值，需自定义 3D exposure |
| 持久化 / lineage | **`pool.py` 无 `to_dict`/`from_dict`/落盘**（全文检索 0 命中），只在内存 `report()` | sklearn estimator，可 pickle；`FactorModel` 容器带 DataFrame accessors 与 `summary()` |
| 依赖 / 许可 | 项目内建，numpy/pandas | BSD-3-Clause，Python ≥3.10；**cvxpy 是直接依赖**（本机未装） |

---

## 2. 已确认的两个 AlphaPool 缺口（本轮新发现）

1. **组合 lineage 实际是空的。** `core/miner/paradigms/base.py:506` 构造
   `LinearCombinedFactor(sub_expressions=selected, weights=weights)` 时**没有传 `parent_ids`**；
   `expressions.py:231` 因此 `_parent_ids = []`，`:257` 的 `to_dict()` 会写出
   `"member_factor_ids": []`。上一轮我说「`from_dict` 丢 lineage 已修复」——读取侧确实修了
   （`:269-276` 兼容新键与 legacy 键），但**写入侧仍没填**，序列化后依然追不回成员因子。
   `expressions.py:364/372/383` 的 `fit_linear_combination` 分支同样未传。
2. **`pool.py` 零持久化**：`capacity=10`、`min_obs=200`、`mutual_ic_max=0.99` 全在内存，
   `report()` 只是个 dict，没有 `to_dict`/`from_dict`，也没有写进 run manifest。
   几百币 × 多因子的组合状态无法跨 run 复现 —— 这是 P1 里必须补的一项，且和接不接 skfolio 无关。

> 另注：`pool.py:236` 的 `min_obs=200` 判断的是 **flatten 后的 (T*N) 行数**，在 439 币面板上
> 恒为几十万行，永远不会触发等权 fallback；在小池上才有意义。口径不一致，建议改为按有效日期数。

---

## 3. 更正上一轮（survey / next_actions）三处不准确

| 上一轮说法 | 核实后 |
|---|---|
| 类名 `SelectCompleteAssets` / `SelectNonExpiringAssets` | 实际是 **`SelectComplete` / `SelectNonExpiring`**（无 `Assets` 后缀） |
| skfolio 1.12.5，「只是组合优化器」，46 descriptors | 当前 **1.12.16**，新增 `CharacteristicsFactorModel` / `TimeSeriesFactorModel`，已是**完整的 PIT 截面因子模型层**，与 AlphaPool 在数学上同构度远高于上一轮判断 |
| 「`SelectComplete` 正对本项目 N(t) 漂移」 | **不对**。`SelectComplete` 是把首尾有 NaN 的资产**整体删除**、把 N 固定成一个完整历史子集 —— 放在 439 币上会砍掉 305 个短历史币，只剩 134 个（正好等于 `cross_section_audit.json` 的 `assets_passing_coverage`）。真正对位 N(t) 的是 `AssetPanel` 的 `active_mask` + 逐日重估回归，两者是**相反的两条路线** |

因此上一轮「先自研扩容 AlphaPool、不接 skfolio」的**结论保留，但理由要换**：
不再是「链路不同构」（1.12 已经同构），而是下面的实测 —— 逐日重估在 1d 加密的低信噪比下
必须配强收缩，且 skfolio 不带任何防自欺闸门。

---

## 4. 本机实测：固定权重 vs 逐日重估（**机制模拟，非 skfolio 运行**）

脚本 `user_workspace/reports/alphapool_vs_crosssection.py`，结果同名 `.json`。
窗口 2023-04-01..2026-06-30，前 712 bar 训练 / 后 475 bar 测试，4 个朴素 PIT 因子
（mom20 / mom60 / −mom5 / realized_vol20，逐日截面 z-score 并 clip ±3），
分位 0.2 多空、5 bar 再平衡、18bp、√365，与生产 `long_short_returns` 同一套算术。

- **A** = AlphaPool 式：训练期一次 `fit_scale_aware_ridge`，权重全期冻结
  （实得 `[-0.152, 0.285, 0.390, -0.173]`）
- **B** = 逐日截面 OLS + trailing-60 严格滞后因子溢价（裸重估）
- **C** = B 与训练期全局先验 50/50 收缩
- **D** = 只用训练期全局先验（时不变，近似 skfolio 的 `ShrunkMu` 极端情形）

| 池 | 方法 | RankIC | turnover | net/bar | 净 SR |
|---|---|---:|---:|---:|---:|
| pit_top60 (N=60 恒定) | **A 固定 ridge** | 0.0333 | 0.125 | +1.09e-4 | **+0.122** |
| | B 裸逐日 | -0.0038 | 0.097 | -2.39e-3 | -2.378 |
| | C 半收缩 | 0.0057 | 0.096 | -2.19e-3 | -2.105 |
| | D 固定先验 | **0.0737** | 0.052 | -6.59e-4 | -0.592 |
| pit_all (N=283→439) | **A 固定 ridge** | 0.0570 | 0.128 | -1.37e-4 | **-0.290** |
| | B 裸逐日 | 0.0130 | 0.102 | -4.46e-4 | -0.874 |
| | C 半收缩 | 0.0236 | 0.101 | -5.64e-4 | -1.096 |
| | D 固定先验 | **0.0679** | 0.058 | -1.22e-3 | -2.095 |

**诊断**：训练期逐日因子收益的 lag1 自相关为 `[-0.069, 0.034, -0.032, 0.000]`，
标准差却各约 0.01（与日收益同量级）—— **逐日截面回归估出来的因子溢价几乎全是噪声、
没有可预测的时间序列结构**。所以 B/C 拿 trailing 均值当信号是在追噪声。

**两个可读结论**：
1. 在 1d 加密、K=4、无收缩的条件下，逐日重估显著劣于一次固定 ridge。
   这不是「skfolio 更差」—— 我的模拟缺了它的 exposure winsorisation、market-cap 加权、
   `factor_prior_estimator`、alpha shrinkage、协方差构造与带约束优化器。它只说明
   **接入 skfolio 时收缩/先验配置是生死线，默认配置直接用在 1d 加密上会翻车**。
2. **D 的 RankIC 最高（0.074 / 0.068）却净 SR 最差之一（-0.59 / -2.10）**，
   而 A 的 RankIC 更低却净收益更好。这是 `readiness` 报告「IC 与实盘收益脱钩」
   的又一次独立实证 —— 若把 skfolio 的 `mu` / IC 反馈直接当晋升信号，会反向优化。

> 所有数字都**不是**晋升结论：无 BH-FDR、无 DSR、无 holdout、无资金费、统一成本。
> 只有 A-vs-B/C/D 的相对方向可解释。

---

## 5. 该怎么用（三步，按依赖顺序）

**第一步｜补 AlphaPool 自身的两个洞（与 skfolio 无关，今天就能做）**
- `base.py:506` 与各 `fit_linear_combination` 分支传入 `parent_ids=pool._ids`，
  让 `to_dict()` 的 `member_factor_ids` 真正落盘；补一条 round-trip 单测。
- `pool.py` 加 `to_dict` / `from_dict`，把 `capacity`、`weights`、`ids`、`ridge` 诊断
  写进 run manifest；`min_obs` 改为按有效日期数计。
- 参数化 `capacity` / `min_obs`（`base.py:453` 已读配置，但 `pool.py:89` 的 `min_obs` 未暴露）。

**第二步｜skfolio 只做离线对照后端，不进主链路**
- 安装：`venv/Scripts/python.exe -m pip install "skfolio>=1.12.16"`（会带 cvxpy；
  本机 Python 3.12.1 符合 ≥3.10，BSD-3 无许可风险）。
- 用现有 439 币面板构造 `AssetPanel`：`returns` + 自定义 3D numeric exposure
  （把挖出的因子当 exposures，**不要用内置股票 descriptors**），`active_mask` 由 PIT 池生成。
- 只跑两件事：`CharacteristicsFactorModel` + `MeanRisk(turnover / cost / long-short 约束)`，
  以及 `CombinatorialPurgedCV` 做稳健性对照。
- **验收**：把它产出的权重换算成同样的分位组合净收益，回灌 `apply_promotion_gates`；
  若它跑不赢第一步修好的 AlphaPool + 本项目分位组合，就不替换主链路。

**第三步｜组合层扩容（P1，仍自研）**
在 `AlphaPool` 上加因子暴露矩阵 → 截面中性化 → 流动性权重约束，并做跨 run 持久化。
这一步做完，才具备「几百币 × 多因子组合」的最小形态；skfolio 届时可作为权重求解器的可选实现。

---

## 6. 一句话

**AlphaPool 回答「哪些因子值得放在一起、各占多少」，skfolio 回答「这些因子该怎么变成带约束的持仓」；
1.12 之后它们确实在截面回归那一段重叠，但实测显示 1d 加密上逐日重估必须先配强收缩，
而 skfolio 恰恰不带任何防自欺闸门 —— 所以它是很好的可选后端，不是替换理由，更不是裁判。**
