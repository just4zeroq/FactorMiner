# 加密多币种多因子组合：成熟项目盘点（2026-11-15）

> 方法：跨源检索当前版本/维护状态 + 本机环境核实。目标回答「有没有一个成熟项目可以直接用」。
> 前置文档：`docs/reviews/cross_section_roadmap.md`（439 币面板实测与 P0 清单）。

---

## 0. 结论

**没有一个成熟开源项目端到端覆盖「加密 PIT universe → 横截面因子挖掘 → 多因子组合 → 统计验证」。**
成熟件都是分层件：数据层、执行层、向量化计算层、组合优化层、因子检验层各有可用产品；
「因子挖掘 + 防自欺闸门」这一层反而没有比本项目更严的成熟实现。

因此路线不是「换一个项目」，而是**按层引入成熟件，保留本项目的数据装配、挖掘与闸门**：
最值得接的是 `skfolio`（组合/因子模型层，原生处理 late inception、退市、purged CV），
其次是机构级 PIT 数据（解决存续偏差），再用 `vectorbt` 做横截面批计算加速。

---

## 1. 「成熟」的判定标准

活跃维护（近 12 个月有 release/commit）/ 可生产使用 / 加密原生（24/7、永续、funding）/
有 PIT 或 universe 元数据 / 许可证可商用 / 与 Windows + Python 3.12 兼容。
只发过论文、单人 alpha 阶段、star≈0 的一律归为「研究原型」，不计入成熟件。

---

## 2. 分层盘点

| 层 | 候选 | 状态（核实于 2026-11） | 关键能力 / 缺口 |
|---|---|---|---|
| 数据 / PIT universe | **Kaiko**（2026-07-07 收购 Amberdata）、**Coin Metrics**（被 Talos 收购）、Tardis | 商业、企业级，SOC-2 / BMR | 40–200+ 交易所，含 funding、OI、liquidations、合约级元数据；业界明确要求「保留上市/退市时间、合约变更、symbol 迁移，只存当前活跃合约会造成存续偏差」。**这是上一轮 P0 的正解** |
| 执行 / 资金费结算 | **NautilusTrader** v1.231.0 stable（2026-08-02）/ v2 rc（2026-09-15），LGPL-3.0 | 成熟，Rust 核心，19 adapters | Funding 按 venue 的 `FundingRateUpdate` 真实结算进 PnL、perpetual 保证金/mark price、多 venue；**但不是因子挖掘框架**，无数据、无 Universe Selection |
| 执行 / 交易机器人 | Freqtrade + FreqAI，GPL-3.0，~49k stars | 非常活跃 | 动态 pairlist、funding candle、自适应重训；**逐币对串行训练**，回测的 pairlist 与实盘不一致，不支持横截面多因子组合；futures 标记为 experimental |
| 横截面计算 / 参数扫描 | **vectorbt** v1.1.0（2026-07-05），Apache 2.0 + Commons Clause | 活跃，可选 Rust 后端 | 把参数网格变成一个数组维度，适合 439×N 面板批量回测；无数据、无 broker、无统计闸门。**PRO 版许可更严（禁止 commercial outcome）** |
| 组合 / 因子模型 | **skfolio**（BSD-3，sklearn API）、Riskfolio-Lib 7.3/7.4（BSD-3）、PyPortfolioOpt 1.6.0（MIT） | 均活跃 | skfolio 有 **46 descriptors / 17 families 的 characteristics-based 截面因子模型**、`SelectCompleteAssets` / `SelectNonExpiringAssets`（处理 late inception、delisting）、Combinatorial Purged CV / WalkForward、换手与成本约束；文档明确「当日不可投资资产带 NaN，优化器只在可投资子集上求解并给零权重」——**正对本项目 N(t) 漂移问题** |
| 因子检验 | **alphalens-reloaded** 0.5.1（2026-10-26） | 维护中 | IC / 分位收益 / 换手 tear sheet 的标准件；股票口径，**无多重检验校正、无 DSR、无 holdout ledger**，只能当对照分析层 |
| 因子库 / 基准 | HKUDS Vibe-Trading `alpha-zoo`（Kakushadze101 / GTJA191 / Qlib158 / FF）、CF Benchmarks 数字资产七因子（Springer 2026） | 前者 W2 scaffold（universe loader 未全接）；后者商业指数 | 前者可当基线动物园（自称支持 BTC-USDT）；后者给出加密原生七因子定义：market / size / value / momentum / growth / liquidity / downside beta，Fama–MacBeth 验证——**适合当因子字典与对照基准** |
| 因子挖掘 | Qlib + RD-Agent、AlphaGPT、本项目 GP/LLM/RL | 混合 | Qlib/RD-Agent 是 A 股口径且验证宽松；AlphaGPT 是 Solana meme 币的 Transformer+StackVM+RL 因子工厂（无依赖清单、无预训练策略、无多重检验、无组合层）；`Common_Risk_Factors_in_Cryptocurrency` 是 R 语言学术复现（Zenodo 2026-03），`orbiter-crypto` 是 0-star Alpha 阶段——均非生产件 |
| 验证 / 闸门 | 无成熟件 | — | **没有任何候选内置 BH-FDR 跨 run、DSR 用真实 `n_trials`、一次性 holdout ledger、净成本后可交易性判据**；本项目这一层是稀缺资产 |

> 另有 QuantConnect LEAN（多资产、Universe Selection、Alpha/Portfolio/Execution 分层、加密 24/7），
> 但加密数据需自备、云侧按节点收费、主语言 C#，且同样没有多重检验校正；
> QuantMind 是 Qlib+RD-Agent+TradingAgents 的 AGPL-3.0 拼装台，加密只是附带市场。两者都不改变结论。

---

## 3. 三个最值得接的（按性价比）

**① skfolio — 组合层，直接补本项目最缺的一块。**
`AlphaPool`（`core/miner/pool.py:75`）容量 10、`min_obs=200`、无持久化，是为少量单因子设计的；
几百币 × 多因子需要因子暴露矩阵 + 截面中性化 + 带约束权重。skfolio 的
`SelectCompleteAssets`（late inception / delisting）、purged CV、NaN 可投资子集求解，
恰好对位 `cross_section_roadmap.md` 的 P0-A1 与 P0-A4。BSD-3、sklearn API，接进来不动闸门。

**② 机构级 PIT 数据，或自建 `onboard/delist` 表 — 解决存续偏差。**
439 pairs 来自「今天仍挂牌」快照。Kaiko / Coin Metrics 提供合约级上市退市元数据、
funding / OI / liquidations 历史；若暂不采购，最低成本是用现有 `onboard_ms`
（`user_workspace/universes/*.json`）+ 交易所公告补一张退市表，把池定义写进 manifest。

**③ vectorbt（或纯 numpy）— 只做批计算，不进主链路。**
实测 `target_permutation_tstats(n_perm=60)` 占单候选 4.6s 里的 98%
（`docs/reviews/mining_production_readiness.md` §1.1）。分层漏斗 Stage A/B/C
比换框架更能提吞吐；vectorbt 只是可选加速器，注意 Commons Clause 非 OSI。

---

## 4. 明确不推荐

| 候选 | 原因 |
|---|---|
| Qlib 作为主链路 | 静态 `instruments` 是存续偏差反面教材；日历/A 股成本/IC 判据详见 `cross_section_roadmap.md` §4 |
| Freqtrade / FreqAI | 逐 pair 训练、无横截面组合、回测与实盘 universe 选法不一致 |
| vectorbt PRO | 许可禁止「commercial outcome」，比免费版更严；免费版已有 Rust 后端 |
| AlphaGPT / orbiter-crypto / Common_Risk_Factors / Vibe-Trading | 研究原型或 alpha 阶段，无验证闭环，不能当生产底座 |
| Riskfolio-Lib | 功能全，但 `install_requires` 强依赖 vectorbt，会把 Commons Clause 带进环境（许可证扫描风险）；单人维护 |

---

## 5. 本机环境与许可证（实测）

`venv/Scripts/python.exe` → **Python 3.12.1**；已装 `numpy / pandas 3.1.1 / scikit-learn / lightgbm / pyarrow`；
**未装** `skfolio / vectorbt / cvxpy / alphalens / riskfolio / qlib / nautilus_trader`。

- skfolio 要求 Python ≥3.10，需补 `cvxpy`（本机缺）→ 装得上。
- vectorbt 1.1.0 要求 Python 3.11–3.14、pandas 3 / NumPy 2.4+ → 本机符合。
- NautilusTrader 支持 Python 3.12–3.14，但 Windows 原生部署成本高于前两者，建议作为**最终成交口径的离线校验**而非研究主链路。
- 许可证：skfolio / Riskfolio-Lib BSD-3，PyPortfolioOpt MIT，NautilusTrader LGPL-3.0，
  Freqtrade GPL-3.0，vectorbt Apache-2.0 + Commons Clause（非 OSI，对外提供收费服务受限）。

---

## 6. 最小拼装路线

```
[保留] 数据装配 + 因子挖掘 + 闸门 + CLI 闭环   ← 本项目（勿替换）
   ↓ 面板 (T, N)
[引入] skfolio        因子暴露/中性化/带约束权重 + purged CV + 退市感知
[引入] vectorbt       可选：横截面批回测与分层漏斗加速
[引入] alphalens-reloaded / alpha-zoo   只读对照基线，不参与晋升
   ↓ 组合权重
[可选] NautilusTrader  资金费结算、滑点、成交口径校核
   ↓
[保留] apply_promotion_gates → holdout ledger → promote
```

**验收条件**：任何外部件的输出都必须回到 `core/evaluation/significance.py:485` 的闸门；
绝不把 IC / Sharpe 的外部反馈直接当晋升判据。

---

## 7. 需确认

1. 是否接受机构数据源采购（Kaiko / Coin Metrics 起步约 $1k/月量级，需询价），
   还是先用 `onboard_ms` + 公告自建退市表？
2. 组合层是「接 skfolio」还是「在 `AlphaPool` 上自研扩容」？前者快但引入 cvxpy，
   后者可控但要自己实现 purged CV 与退市感知。
3. vectorbt 的 Commons Clause 是否影响后续商业化（对外提供服务会受限）。
