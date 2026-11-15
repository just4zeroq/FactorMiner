# 几百币横截面组合多因子：现状盘点与路线

> 2026-11-15 · 方法：源码级核查（`file:line`）+ 本机真实数据实测 + `doctor` 实跑
> 背景：目标从「挖可交易单因子」改为「几百个币的横截面组合多因子」，需重新评估是否改用 Qlib 二次开发。

---

## 0. 结论

**目标变了，但「整体迁到 Qlib」仍然不是最优解；改的是借力层级，不是底座。**

新目标真正需要补的是四样东西：① 按日期可用的 point-in-time universe；② 分层交易成本；
③ N(t) 感知的横截面组合与评估；④ 一个截面模型层。Qlib 只能提供 ④ 的一部分，
而它自带的静态 `instruments`（当前指数成分）恰恰是 ① 的反面教材，A 股成本假设帮不上 ②，
IC 反馈当判据会让已有的闸门体系倒退。④ 可以作为**可选依赖/离线对照后端**引入，
不必替换数据层、成本层、闸门层。

**更重要的现状判断：本项目已经朝这个方向走了一半，现在换底座等于把刚搭好的部分推倒。**

---

## 1. 已经存在的能力（本轮核实，勿当成待办）

| 能力 | 位置 | 状态 |
|---|---|---|
| 全交易所挂牌快照（755 futures + `onboard_ms` 上市时间戳） | `user_workspace/universes/binance_futures_usdt_1d.json` | 已生成 2026-11-15 21:52 |
| 从快照筛 mining 配置（min_bars=250 + test 窗口有 bar） | `scripts/build_universe_config.py:88-108` | 755 listed → **439 usable**，316 个带 `dropped_detail` |
| 全量配置 | `user_workspace/configs/configAllUSDTFutures.json`（439 pairs）、`configAllUSDTSpot.json`（363 pairs） | 已生成，未纳入 git |
| 数据 | `data/binance/futures/*-1d-futures.feather`（755 OHLCV）、`crypto_streams/`（758 funding）、spot 505 | 439 个声明 pair **0 缺文件** |
| 大横截面 coverage 守卫 | `core/commands/doctor.py` `coverage_policy=panel` / `min_assets_per_bar` | 已允许 staggered listings，按「每根 bar 的最小截面宽度」判定而非逐 pair 全覆盖 |
| 组合层 | `core/miner/pool.py:75` `AlphaPool`（mutual-IC 0.99 去重 + ridge 权重 + `weight_fit_shift=0` 已修 horizon 错配）、`core/miner/expressions.py:246-276` `LinearCombinedFactor`（`from_dict` 已恢复 `member_factor_ids` lineage） | 已有，但为「少量单因子」规模设计 |
| 闸门 | `core/evaluation/significance.py:485` `apply_promotion_gates`（BH-FDR 跨 run + DSR 真实 `n_trials` + empirical-null）、`holdout_guard.py` | 完整，七轮修复成果 |

> 更正两处过时结论：`target_permutation_tstats` 的零分布结构已在 `8677e0f` 修为
> `mode="cross_section"`；`LinearCombinedFactor.from_dict` 丢成员 id 也已修复。
> 二者不再是未修缺陷。

---

## 2. 实测：439 币配置现在能不能跑

`venv/Scripts/python.exe -m core.cli doctor --config user_workspace/configs/configAllUSDTFutures.json --json`

**0 FATAL / 2 WARN，配置可跑。** 关键输出：

```
mine_period 2023-01-01..2026-06-30: 1277 bars,
  assets/bar min 105 / median 219 / max 439;
  334 pair(s) listed after window start (allowed by coverage_policy=panel)
test_period 2026-07-01..2026-09-13: 75 bars, assets/bar 439/439
crypto_streams: WARN — none declared: mining sees OHLCV only
```

只读审计 `user_workspace/reports/cross_section_audit.py`（结果 `cross_section_audit.json`），
以**固定窗口**为分母（不能用文件自身长度，否则上市前缺失被藏起来）：

| 指标 | 实测 |
|---|---|
| 声明 pairs / 磁盘 1d 文件 / 缺文件 | 439 / 755 / 0 |
| 满足 `min_coverage=0.9` 的资产 | **134 / 439**，覆盖率中位数 **0.537**（p10 0.248） |
| 面板密度（2023-04..2026-06） | **62.1%**（323,362 / 521,093 格） |
| 逐日可用资产（声明池） | 2023 均值 139.5 → 2024 204.8 → 2025 357.8 → 2026 439 |
| trailing 90d 成交额占比 | Top20 **89.8%**；21–60 5.8%；61–150 2.5%；151+ 1.8% |
| 各层中位成交额（USDT） | 2.62e8 / 3.85e7 / 6.77e6 / 1.69e6（首尾差 **155 倍**） |
| 成本模型 | `core/experiment/costs.py:17-21` 全局常数 18bp（4bp 手续费 + 5bp 滑点，双边） |

---

## 3. 三个必须先解决的统计问题

### 3.1 已确认：N(t) 漂移 + 幸存者偏差

`real_client.py` 用日期**并集** `reindex` → NaN（正确，无静默 ffill），但因此截面宽度
从 105 单调涨到 439。`cs_rank` / 分位组合 / 等权组合在 N=105 与 N=439 上不是同一个统计量，
RankIC 时间序列混合了不同 N 的噪声，组合收益里混入「universe 扩容」效应。

`panel_validator.py:176-177` 的低覆盖只是 **warning**（不是 critical），所以
`data_quality.strict=true` 拦不住 62.1% 密度的面板；`doctor` 的 `coverage_policy=panel`
是有意放行 staggered listings——这是**设计选择，不是 bug**。

但 439 个 pair 来自「2026-11-15 仍在挂牌」的快照，用今天还活着的合约回测 2023，
下架/退市合约完全缺席，这是横截面策略的系统性高估。`onboard_ms` 已存在，
但 `download_exchange_universe.py:364-368` 只用它定下载起点，
`build_universe_config.py` 的筛选只用 bar 数——**没有按日期的存续表**。

> **需确认意图**：是否接受「当前存活合约 + 上市后才有数据」这一研究口径。
> 若要可交易的结论，必须补 PIT 存续表（含退市日），或至少固定一个滚动可用的资产池。

### 3.2 已确认：成本不分层

Top20 之外的成交额小 1–2 个数量级，真实滑点/冲击成本远高于 5bp。统一 18bp 会
**系统性高估小币层净收益**。这至少部分解释了
`docs/reviews/mining_production_readiness.md` §L3.1 的实测
「同一 `mom20`，20 币净 SR 0.568 → 78 币 -0.016」——不能简单归结为「扩池稀释信号」。
（该数字目前只见于该报告一句，缺原始 run 明细，建议复核。）

### 3.3 能力缺口：组合层规模与评估口径

`AlphaPool` 默认 `capacity=10`、`min_obs=200`，且 `pool.py` 无 `to_dict` / 持久化
（只有 `combined_panel`）。几百币 × 多因子需要的是「因子暴露矩阵 → 截面中性化 →
带约束的权重」，并显式处理 N(t) 变化、流动性/beta 中性化、组合 lineage 落盘。
`windows.common_timeline()` 取**交集**（`windows.py:146`，"a window is clean only if
every asset has it"），在 439 币上可能把滚动/holdout 窗口压到最近期——
仅被 `windows.py:233` 内部调用，**对 mining 面板无影响，但对 holdout 就绪的影响需确认**。

---

## 4. Qlib 在新目标下的重估

上一轮的边界条件是「若目标变成大横截面 ML 组合预测，Qlib 有价值」——现在正是这个目标，
所以**模型层的价值确实上升**：`DataHandler` 处理器链（截面 z-score / MAD / 中性化）、
TopkDropout、模型 zoo、IC/ICIR 评估、以及 RD-Agent 的假设→实现→反馈闭环，
都是本项目组合层目前没有的。

但结论仍是不整体迁，理由随目标变化而更具体：

1. 需要补的四项里，Qlib 只覆盖 ④ 的一半；
2. ① 恰是 Qlib 的弱项——`instruments` 是静态指数成分列表（CSI300/500），
   Qlib 官方也承认 Yahoo 采集器漏掉退市标的会造成幸存者偏差；
3. ② 要重写 A 股 5/15bp、整手、涨跌停假设，加密的 8h 资金费、永续、T+0 无对应概念；
4. ③ 是本项目自建统计纪律的核心，换成 RD-Agent 的「IC 任意微增即更新 SOTA」
   是倒退（券商实测亦指出其「IC 与实盘收益脱钩」「对前视偏差把握不足」）；
5. ④ 可以只引入模型层：在 `core/miner/paradigms/` 加一个截面模型范式
   （LightGBM / 岭回归 + 中性化），输入现有 `pivot_dict` 面板，输出仍走
   `apply_promotion_gates`；或把 Qlib 作为 optional 离线对照后端跑
   Alpha158 + LightGBM 当 benchmark，不进主链路、不进晋升判据。

本机 Windows 环境下 RD-Agent 还需 WSL2 + Docker + CoSTEER 每因子独立 Conda；
把 755 个 feather 转成 Qlib 定长二进制格式是券商实测报告里「部署中工作量最大的一步」。

---

## 5. 最小修复集（按投入产出，不在本轮擅自实施）

**P0 — 不做则「几百币」是统计假象**

| 项 | 位置 | 动作 |
|---|---|---|
| A1 PIT universe | `user_workspace/universes/`、`build_universe_config.py` | 用 `onboard_ms` + 退市信息构造按日期存续表；或固定「滚动 12 个月全有数据」的资产池；把池定义写进 manifest 并作为 trial family 键 |
| A2 成本分层 | `core/experiment/costs.py`、`backtest_engine.py` | 按成交额 tier 给 commission/slippage（≥3 档），`gate_cost_rate` 由持仓权重动态推导，不再全局常数 |
| A3 覆盖约束 | `panel_validator.py:176` | 低覆盖从 warning 提升为可配置 critical；并改成「逐资产 coverage 下限」而非整面板平均（整面板平均会掩盖 305 个短历史币） |
| A4 N(t) 感知 | `core/miner/operator_runtime.py`、`portfolio_eval.py` | 有效 N < 阈值时标记/跳过截面算子；RankIC 与组合收益按 N 分层报告 |

**P1 — 让组合多因子真正成立**

- `pool.py`：`capacity` / `min_obs` 参数化，加 `to_dict` / 持久化与成员 lineage 落盘；
  因子暴露矩阵 + 截面中性化 + 权重约束。
- `budget.py`：trial family 分账（不同 universe / tier / horizon 独立计 `n_trials`），
  预注册后不可改。
- 评估分层漏斗（Stage A 42ms 粗筛 / B 4.6s 精算）、`evaluator.py:290` 线程池→进程池
  （实测线程并行 0.58x，比串行慢 72%）。

**P2 — 借力而不换底座**

- 把 758 个 symbol 的 funding/OI/basis 接进 `crypto_streams` 搜索空间
  （目前 `doctor` 明确 WARN：只用于回测成本，挖矿看不到）。
- 引入可选截面模型范式或 Qlib 离线对照后端；只借 RD-Agent 的双层 Trace 与
  CoSTEER 写码→执行→错误摘要→修正循环。

---

## 6. 一句话

**439 币的配置、数据、doctor 守卫和组合层都已经在了，现在换 Qlib 是把唯一已经建起来的
优势（数据因果卫生 + 闸门）换成一堆需要重写的 A 股假设；真正拦住「几百币横截面组合多因子」
的是存续偏差、成本分层和 N(t) 漂移，这三样 Qlib 一样都不解决。**
