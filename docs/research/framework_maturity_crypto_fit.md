# 因子挖掘框架横向评估：成熟度 × 加密货币适配度

> **评估日期**：2026-11-14（当日第三轮，基线已按 15:40 校准后的当前源码）
> **对比对象**：20 个外部框架（18 个原始调研对象 + 后补 `trade-learn` + 增量 `AlphaMaster`）＋ `FactorMiner` 自身 = **21 个**
> **源码位置**：`D:\code\github\` 下各仓库；`D:\code\github\FactorMiner`（本项目）
> **分析方式**：沿用 `[原报告](industry_factor_mining_survey.md)` 与 `[AlphaMaster 增量报告](alphamaster_competitor_review.md)` 的源码实读结论，本轮新增① 21 仓库工程指标的脚本化盘点；② 加密适配维度的逐仓复核；③ 对两处既有结论的口径修正。所有「未找到」均为精确检索无命中。
> **本报告回答**：哪个最成熟？哪个最适合加密货币？两者是不是同一个？以及我们该抄谁。

---

> **⚠️ 实施前复核补记（2026-11-14 16:5x）**：为落地 §5.2 的 P0-a / P0-b 做了源码级勘察，两处原文结论需收窄/改写，实施以本补记为准。
>
> **① P0-a 的影响面比 §0.2 发现 5 与 §5.2 所述小**：`annualisation` 只用于 `long_short_returns` / `timing_returns` 的 `sharpe` 展示字段（`portfolio_eval.py:171,267`）与 `multiple_testing_bar` 的 `bar_annualised_sharpe`（`significance.py:257`）。DSR 判定用的是 per-period `sr = cost_adj_return / std_return` 与 `1/sqrt(n_obs-1)` 尺度的 benchmark（`evaluator.py:572-579`、`significance.py:280-289`），BH-FDR / NW-HAC t / promotion gates 均不涉及年化。**因此这是「报告数字正确性」问题，不是「证伪闸门正确性」问题**：1d 加密数据下 252→365 会让年化 Sharpe 展示值 ×√365/252 ≈ **1.203**（`p1_gate_upgrades.md` 的「年化 2.35」应为 2.83），优先级从「闸门标尺」下调为「展示口径」。仍应先修，否则研究者按错误数字判断策略强度；但不能说它会改变任何因子的通过/拒绝结论。
>
> **② P0-b 的真问题不是「全入口是否经 director」**：`director.py:313-330` 的 `_enforce_holdout()` **仅在 `config.evaluation.gates.strict_holdout` 显式配置时生效**，默认静默跳过；账本路径随配置走，现有 `user_workspace/.holdout_ledger_p0.json` 记录了 `2025-07-01..2026-11-13`，而默认 `user_workspace/.holdout_ledger.json` 为空 → **账本分叉**。真正读取 holdout 收益的 `core/commands/backtest.py:151-190`、`core/commands/inspect.py:45` 及 `user_workspace/experiment_tools/` 下 6 个 OOS 脚本**完全不碰 ledger**。正确方案是「单一账本 + 全局揭示守卫 + 绑定 `user_workspace/experiment_window.json` 预注册窗口」，而不是把 backtest 硬绑到 director 上。

---

> **✅ 实施完成补记（2026-11-14 17:0x–17:3x）**：P0-a / P0-b 已按上述补记落地到生产代码，全量测试 **326 passed / 1 skipped**（原基线 286 / 1，新增 40 个测试）。
>
> **P0-a**：新增 `core/evaluation/annualisation.py`；`resolve_annualisation()` 优先级 = 显式 `fitness.annualisation` / `evaluation(.gates).annualisation` > `data_feeds.timeframe` > DatetimeIndex 中位间隔 > 365。改动 `evaluator.py:567,656`（传 `index=factor_values.index`）、`director.py:158`（budget_report 传年化）、`experiment_trace.py:351`、`significance.py:233`、`portfolio_eval.py:46,64,195`、`inspector/metrics.py:92,273` + `inspector/engine.py`。`tests/test_annualisation.py` 用面板数据锁定「HAC t / p / DSR / promotion gates 在 252↔365↔8760 下逐位相同，只有 `ls_sharpe` ×1.203」。**注意 `portfolio_eval.py:191` 的 `lookback=252` 与 `TODO.md` 里的「252 根滚动窗口」是窗口长度、不是年化，未改。**
>
> **P0-b**：新增 `core/evaluation/holdout_guard.py`（`guard` / `guard_periods` / `guard_config`；mode = `strict`(默认) / `audit` / `off`，可由 `evaluation.gates.holdout_mode` 或环境变量 `FACTORMINER_HOLDOUT_MODE` 切换）。唯一账本 `user_workspace/.holdout_ledger.json` 已 bootstrap 合并 `_p0` 的 2025-07-01..2026-11-13 记录，`HoldoutLedger` 默认路径改为项目根绝对路径；`configOOSHoldout.json` 的 `holdout_path` 已指向它并加 `holdout_mode: strict`。接入 `commands/backtest.py`（`--orientation-db-root` 视为 final）、`commands/inspect.py`、`director.run()` **开头** fail-fast（去掉静默跳过；未配 strict_holdout 时仍检查每个 `test_period` 是否命中已烧窗口）。`--allow-holdout-reuse` 写不可绕过的 `reports/holdout_overrides.jsonl`。`tests/test_holdout_guard.py` 含**入口接线断言**（monkeypatch 验证 3 个入口确实调用守卫 + 源码文本扫描），防止以后新增 CLI 漏接。
>
> **实施中新发现并修掉的一个致命点**：`.gitignore:140` 原本忽略 `user_workspace/.holdout_ledger.json`。若保持忽略，任何 fresh clone 都会从空账本起步，P0-b 守卫在别的机器上形同虚设。已移除该忽略规则并注释原因；`holdout_access.jsonl`（高频）仍忽略，`holdout_overrides.jsonl` 有意保持 untracked 可见以便 review。
>
> **与勘察方案的两处偏离，如实记录**：① 采用默认 `strict`（建议 B），但 `audit` / `off` 模式作为可配置逃生口保留，因此不需要「先跑几天 audit」的阶段切换；bootstrap 仅指账本合并，已完成。② `user_workspace/experiment_tools/` 下的 OOS 脚本**未逐个硬接守卫**：逐仓复核后发现多数是读取已发表结果做汇总（属重算而非新揭示），且 `validate_gates.py` / `validate_crypto_features.py` 刻意使用独立隔离账本，硬接会误伤自检。改为提供两行接入模板 `holdout_guard.guard_config(cfg, source=...)`；真正能**新开任意窗口做评价**的 3 个入口已全部覆盖。若后续要让脚本层也强制，按该模板逐个接入并补入口断言即可。
>
> **仍未解决**：`.fdr_ledger*.jsonl` 与 `.experiment_trace.jsonl` 仍被 `.gitignore` 忽略，`num_trials` 跨机器/clone 不同步（会低估试验次数分母）。这是独立于 P0-b 的遗留风险，见 §5.2。

> **⚠️ 二次校准（2026-11-14 17:5x 补记，为 `platform_maturity_gap.md` 而作）**：本轮精确检索发现两处需收窄，均不影响 §0.3 的三问答案。① §1.3 评分规则 M4 与 §4 对照中把「随机零分布」计入 FactorMiner 得分，实际 `core/evaluation/significance.py:147,169,208` 的三个函数**只有单元测试引用、主流程零调用**，应表述为「有实现、未接入晋升判定」；② §0.2 发现 5 与 §5.2 涉及的成本口径：回测侧 `core/backtest/backtest_engine.py:340-347` 的 `build_funding()` 是 `grid["rate"]=0.0001` 的**常数网格**，与 `evaluator.py:47` 的 `gate_cost_rate=0.0018` 不同源，且都没用已在盘上的真实 `funding_rate` 流 —— 「成本入判定」成立，「成本模型已闭合」不成立。另补一条平台层结论：本报告讨论的是「框架选型」，而「做成成熟平台」还差结果产出与运营可复现两层，见 `platform_maturity_gap.md`。

---

## 0. 执行摘要

### 0.1 一句话结论

**「最成熟」和「最适合加密货币」几乎不重合，而且接近负相关**：工程成熟度前 5 名里 4 个是股票日频框架（qlib、RD-Agent、LLM-Alpha-Mining-Framework、gplearn），4 个加密专用框架里 3 个连主流程都跑不通。**不存在一个可以直接搬来用的「成熟加密因子框架」**——最成熟的是 qlib（平台）与 RD-Agent（挖掘器），最适合加密货币且能直接跑的是 `crypto-factor-lab`，而数据与证伪能力最全、最适合作为长期主干的是 FactorMiner 自己（前提是补掉 §5.2 那几个加密口径缺口并拿到正向 OOS）。

### 0.2 五个关键发现

| # | 发现 | 证据 |
|---|---|---|
| 1 | **成熟度与加密适配度几乎负相关**：M≥8 的 5 家里 4 家硬编码股票资产池（CSI300 / S&P500 / CRSP）；C≥6 的 3 家里 2 家 M≤6 | `RD-Agent/.../conf_baseline.yaml:5` `market: csi300`；`LLM-Alpha-Mining-Framework/DATA.md:3-8` CRSP/WRDS；本轮 §4.1 评分表 |
| 2 | **四个加密专用框架里两个无法运行**：`AlphaForge-Crypto` 主入口 import 不存在的函数 + 回测全 TODO；`Alpha-Research-Discovery` 调用 `data_fetcher` 里不存在的方法、缺 ccxt、失败回退合成 funding | `AlphaForge-Crypto/run_backtest.py:9,32-41`；`Alpha-Research-Discovery/alpha_01:169`、`alpha_16:163,192-202` |
| 3 | **`crypto-factor-lab` 的 holdout ledger 只在 HTTP service 生效，CLI 主入口不调用也不写 `holdout_end`**——原报告 §0.2 发现 2 的绝对表述需要收窄；而我们自己的 `HoldoutLedger` 已接进 `director.py` 主流程，全链路反而更彻底 | `crypto-factor-lab/factor_lab/catalog.py:282-299`、`service.py:510,900-905`、`scripts/run_factor_mining.py:60-110`；`FactorMiner/core/miner/director.py:50-51,127-142,188,289` |
| 4 | **qlib 确实有 crypto 采集器，但只有 CoinGecko 日频价格/量/市值、无 OHLC，官方明写不支持回测**；`examples/` 全仓 crypto 零命中 | `qlib/scripts/data_collector/crypto/collector.py:17,38,138`、`crypto/README.md:12` |
| 5 | **我们自己最该先修的不是算子，是年化口径**：评价/显著性/组合层默认 `annualisation=252`（A 股习惯），而加密是 7×24。~~会让 Sharpe、DSR、多重检验门槛全部系统性偏斜~~ → **经 16:5x 复核收窄**：只影响展示用年化 Sharpe（×1.203），DSR / BH-FDR / HAC t 判定不受影响；**已由 P0-a 修复** | `core/evaluation/evaluator.py:567,656`、`significance.py:233,257`、`experiment_trace.py:351,373`、`portfolio_eval.py:46,171,267`；对照 `crypto-factor-lab/factor_lab/mining.py:239` 的 `24*365=8760`；修复见顶部实施补记 |

### 0.3 直接回答三个问题

| 问题 | 答案 | 限定条件 |
|---|---|---|
| **哪个最成熟？** | **qlib**（工程/生态/维护第一，9/10），但它是**数据+执行平台，不含挖掘器**；限定「挖掘器」则是 **RD-Agent**（微软 v1.0.0，8/10）；限定「方法论与证伪纪律」则是 **LLM-Alpha-Mining-Framework** 与 **FactorMiner**（并列 8–9/10） | qlib 需外接挖掘器；RD-Agent 无任何多重检验校正；LLM-Framework 需 WRDS 授权数据且是美股 |
| **哪个最适合加密货币？** | **可直接跑且最严谨：`crypto-factor-lab`（8/10）**；**作为长期研究主干：`FactorMiner`（7/10）** | 前者仅 BTC 单品种、无 OI/basis、ledger 只在 API 生效；后者正向 OOS 尚未到手，且年化/PIT/成本口径待修 |
| **该搬哪个现成框架？** | **都不要整体搬。** 抄 `crypto-factor-lab` 的 purge + 成本模型 + 非方向因子语义，抄 `LLM-Framework` 的统计闸门（已抄），抄 `AlphaMaster` 的动作掩码与 `drop_forming` 契约（工程项），主干继续用自己的 | 见 §5 |

---

## 1. 范围、口径与评分规则

### 1.1 对比对象与分类

| 类别 | 框架 | 说明 |
|---|---|---|
| **执行/评价平台（非挖掘器）** | `qlib` `alphalens` `KunQuant-rs` `trade-learn` `gplearn` `WorldQuant_alpha101_code` | 提供算子、执行、评价或算法范本，不含完整挖掘闭环 |
| **公式化 / RL / GAN 挖掘器** | `AlphaGen` `AlphaForge` `gpquant` `AlphaMaster` | 股票或外汇定位 |
| **LLM / Agent 挖掘器** | `RD-Agent` `LLM-Alpha-Mining-Framework` `CosmosAlpha` `EvoAlpha` `WorldQuant-LLM` `alphaminingv2` | 全部股票 / WQ Brain 定位 |
| **加密货币专用** | `crypto-factor-lab` `AlphaForge-Crypto` `alpha-factor-mining-crypto` `Alpha-Research-Discovery` | 本轮重点核查对象 |
| **本项目** | `FactorMiner` | 基线按 2026-11-14 当前源码（P0 闸门已落地） |

> **口径说明**：原报告标题写「18 个仓库」，但 §1.2 清单表实际 19 行（含 §5.6 后补的 `trade-learn`）；增量报告再加 `AlphaMaster` 称「第 19 个竞品」。本轮为避免歧义，统一按 **20 个外部 + 1 个自身** 计数，不改动历史报告的编号。

### 1.2 两个维度互不重叠

- **成熟度 M**：这个框架作为「软件 + 研究方法」是否可靠、可维护、可复现。**不含任何加密特有能力**。
- **加密适配度 C**：这个框架能否直接、正确地在永续合约市场上做因子研究。**不重复统计纪律**（那是 M 的一部分）。

这样分是为了让「成熟但不适用加密」与「适用加密但不成熟」能在同一张图上分开显示。

### 1.3 评分规则（可复算，非上游官方指标）

每项 0 / 1 / 2 分，5 项合计 10 分。**分数是本次基于硬指标的人为分档，用于相对定位，不是绝对评价**；判定依据全部可在 §2.1 与本轮检索中复核。

| 成熟度 M | 0 | 1 | 2 |
|---|---|---|---|
| M1 工程规范 | 无 CI/依赖清单 | 有其一 | CI + 依赖清单 + LICENSE + Docker 齐全 |
| M2 测试与文档 | 0 测试 | 1–49 用例 | ≥50 用例且有 markdown 文档 |
| M3 实现完整度 | 骨架 / ImportError / 主流程 TODO | 部分可跑或依赖外部平台/授权数据 | 主流程自洽可跑 |
| M4 统计证伪纪律 | 无任何闸门 | 有切分/成本/HAC 等中间层启发式 | FDR/DSR/零分布/num_trials/一次性留出 中 ≥3 项 |
| M5 维护与诚实度 | 停更/文档漂移/结果美化 | 有维护或主动记录失败 | 持续维护 + 主动证伪自己 + 明确 limitations |

| 加密适配 C | 0 | 1 | 2 |
|---|---|---|---|
| C1 数据可得性 | 硬编码股票池 | 市场无关、可接任意面板 | 交易所加密原生数据源且可拉取 |
| C2 加密原生字段 | 无 | 仅 funding 或「仅采集未用」 | funding + OI + basis + 清算 中 ≥2 类入因子 |
| C3 市场时钟与成本 | 股票日频日历 | 部分（如下一根成交但有 252 年化） | 7×24 年化 + 三段成本 + funding 成本 + 下一根 bar |
| C4 资产池与风险 | 静态池 / 幸存者偏差 | 有 PIT 模块但未接主入口 | PIT 资产池 + 杠杆/爆仓处理 |
| C5 可直接用于加密 | 无法运行 | 能跑但定位非加密 / 需重接数据 | 加密原生定位 + 主入口可跑 + 依赖齐全 |

---

## 2. 成熟度：硬指标盘点

### 2.1 原始指标（脚本化盘点，已排除 `venv/` `data/` `.git/` `__pycache__` 等）

| 框架 | 文件 | .py | Python 行 | 测试文件 | 用例 | md | CI | 依赖 | Docker | LICENSE | 最后提交 | TODO | NotImpl |
|---|---:|---:|---:|---:|---:|---:|:-:|:-:|:-:|:-:|---|---:|---:|
| qlib | 600 | 309 | 66,724 | 51 | 254 | 63 | ✅ | ✅ | ✅ | ✅ | 2026-10-08 | 94 | 255 |
| RD-Agent | 839 | 562 | 72,018 | 33 | 166 | 53 | ✅ | ✅ | ✅ | ✅ | 2026-09-23 | 125 | 39 |
| trade-learn | 776 | 610 | 105,930 | 301 | 1,690 | 61 | ✅ | ✅ | ❌ | ✅ | tarball | 2 | 68 |
| LLM-Alpha-Mining-Framework | 155 | 112 | 21,548 | 34 | 311 | 10 | ✅ | ✅ | ❌ | ❌ | tarball | 0 | 0 |
| AlphaMaster | 240 | 179 | 35,842 | 38 | 243 | 7 | ❌ | ✅ | ❌ | ✅ | tarball | 0 | 0 |
| **FactorMiner** | **795** | **119** | **26,721** | **25** | **287** | **37** | **✅** | **✅** | **✅** | **✅** | **2026-11-14** | **2** | **8** |
| CosmosAlpha | 122 | 95 | 43,689 | 9 | 71 | 6 | ✅ | ✅ | ❌ | ✅ | tarball | 0 | 0 |
| gplearn | 53 | 14 | 4,947 | 6 | 54 | 5 | ✅ | ✅ | ❌ | ✅ | 2026-01-11 | 0 | 0 |
| AlphaGen | 96 | 87 | 17,612 | 7 | 55 | 1 | ❌ | ✅ | ❌ | ❌ | 2026-06-04 | 1 | 14 |
| AlphaForge | 93 | 82 | 18,439 | 7 | 55 | 1 | ❌ | ✅ | ❌ | ❌ | 2024-09-01 | 0 | 15 |
| alphalens | 44 | 13 | 8,481 | 3 | 29 | 1 | ✅ | ✅ | ❌ | ✅ | 2020-04-27 | 0 | 0 |
| WorldQuant-LLM | 29 | 16 | 5,038 | 0 | 0 | 1 | ❌ | ✅ | ✅ | ❌ | tarball | 1 | 0 |
| crypto-factor-lab | 66 | 55 | 7,544 | 18 | 78 | 3 | ✅ | ✅ | ❌ | ✅ | tarball | 0 | 0 |
| alphaminingv2 | 80 | 65 | 13,409 | 2 | 18 | 2 | ❌ | ✅ | ❌ | ❌ | tarball | 0 | 0 |
| Alpha-Research-Discovery | 43 | 34 | 19,411 | 0 | 0 | 1 | ❌ | ✅ | ❌ | ❌ | tarball | 0 | 2 |
| AlphaForge-Crypto | 25 | 16 | 4,344 | 0 | 0 | 2 | ❌ | ✅ | ❌ | ✅ | tarball | 2 | 0 |
| alpha-factor-mining-crypto | 18 | 12 | 2,777 | 0 | 0 | 1 | ❌ | ✅ | ❌ | ❌ | tarball | 0 | 0 |
| EvoAlpha | 20 | 18 | 2,291 | 0 | 0 | 1 | ❌ | ✅ | ❌ | ❌ | tarball | 0 | 0 |
| gpquant | 13 | 8 | 1,299 | 0 | 0 | 1 | ❌ | ✅ | ❌ | ❌ | tarball | 0 | 0 |
| WorldQuant_alpha101_code | 3 | 2 | 3,417 | 0 | 0 | 0 | ❌ | ❌ | ❌ | ❌ | 2019-03-07 | 0 | 0 |
| KunQuant-rs | 21 | 1 | 129 | 0 | 0 | 1 | ❌ | ❌ | ❌ | ❌ | 2025-08-22 | 0 | 0 |

> **解读注意**：qlib 的 255 个 `NotImplementedError` 与 trade-learn 的 68 个主要来自抽象基类 / 数据采集器适配层 / 待实现扩展点，**不等于骨架**；反之 `EvoAlpha` 的 0 个 TODO 是因为它根本没有实现（`quality.py:4-30` 自述 "Placeholder for your real ... API"）。所以本轮没有把 TODO 计数直接折进评分，只作交叉验证。「最后提交」为 tarball 的仓库无 `.git`，无法核对历史与 CI。

### 2.2 三档归类

| 档 | 框架 | 判据 |
|---|---|---|
| **工业级（可依赖）** | `qlib` `RD-Agent` `gplearn` `alphalens` `FactorMiner` `LLM-Alpha-Mining-Framework` | CI + 依赖 + LICENSE + ≥50 测试 + 文档 + 主流程可跑 |
| **研究级（可读不可依赖）** | `AlphaGen` `AlphaForge` `CosmosAlpha` `AlphaMaster` `crypto-factor-lab` `trade-learn` `WorldQuant-LLM` | 有真实现，但或缺 CI、或缺 LICENSE、或依赖外部平台/授权数据、或有明显口径分叉 |
| **骨架级（只能当思路）** | `AlphaForge-Crypto` `Alpha-Research-Discovery` `EvoAlpha` `alpha-factor-mining-crypto` `alphaminingv2` `gpquant` `WorldQuant_alpha101_code` `KunQuant-rs` | 无测试 + 无法运行 / 仅公式范本 / 仅绑定层 |

### 2.3 「成熟」有三种，别混为一谈

1. **工程成熟**（qlib、RD-Agent、FactorMiner）：CI、Docker、LICENSE、测试、文档、持续提交。
2. **方法论成熟**（LLM-Alpha-Mining-Framework、crypto-factor-lab、FactorMiner）：把「搜索了多少次」折进显著性判断，并主动写下失败结果。
3. **生态成熟**（qlib、gplearn、alphalens）：被大量下游项目依赖、有长期维护记录。

**RD-Agent 是最能说明「三者不等价」的例子**：工程与生态都顶级（微软 v1.0.0、839 文件、166 测试、CI/Docker/53 篇文档），但方法论上 `prompts.yaml:187-190` 鼓励 "Any small improvement ... set Replace Best Result yes"，且明确无多重检验校正（原报告 §3.1.5）——**工程越成熟，越能高效地把验证集用穿**。这也是为什么本报告把 M4 单独列出来，而不是让 CI 和测试数掩盖它。

---

## 3. 加密货币适配度：逐项核查

### 3.1 九家重点框架的加密特有能力

| 框架 | 数据源 / 定位 | 7×24 年化 | funding 因子/成本 | OI·basis·清算 | PIT 资产池 | 下一根成交 | 三段成本 | 能直接跑 |
|---|---|---|---|---|---|---|---|---|
| **crypto-factor-lab** | Binance USD-M **BTCUSDT 1h** + fundingRate REST | ✅ `24*365` | ✅ / ✅ `shift(1)` 防泄露 | ❌ 仅 funding | ⚠️ 模块有、主入口未接 | ✅ `shift(1)` | ✅ fee+halfspread+slip | ✅ CLI + 合成冒烟 |
| **FactorMiner** | ccxt Binance，20 币 1d + **60 个原生流文件在盘** | ❌ **默认 252** | ✅ 模板 / ⚠️ 固定常数 0.0001 | ✅ 5 类流 + 特征模板 | ❌ 固定 20 币 | ✅ `NEXT_BAR/CLOSE` | ✅ 手续费+滑点 | ✅ CI + 主流程 |
| **alpha-factor-mining-crypto** | ccxt binanceusdm 1h，当下 top30 USDT 永续 | ✅ `365d/Δt` | ✅ / ❌ | ❌ | ❌ **快照选币→幸存者偏差** | ✅ | ⚠️ 仅 taker fee | ✅ 可跑 |
| **AlphaForge-Crypto** | OKX V5，13 类接口（funding/OI/多空比/清算/标记价…） | ❌ 365 与 5m 错配 | 仅采集 / ❌ | ⚠️ 仅采集、因子层零实现 | ❌ | ❌ | ❌ | ❌ **ImportError** |
| **Alpha-Research-Discovery** | 默认 **Yahoo / SP500**，`--crypto` 仅开关 | ❌ | ⚠️ 合成回退 | ⚠️ 多为 stub | ❌ | ❌ | ⚠️ `TC_BPS=5` | ❌ **方法缺失** |
| **AlphaMaster** | **MT5 外汇/贵金属/指数**硬编码 40 品种，另有 OKX 下载器 | ❌ 未找到 | ❌ | ❌ | ❌ | ✅ 下一根 open | ✅ 单边 3bps | ⚠️ 需 MT5 终端 |
| **qlib** | CoinGecko 1d（无 OHLC）；其余 CSI300 / Yahoo / 美股 | ❌ 交易日历 | ❌ | ❌ | ⚠️ 股票 PIT 采集器 | ❌ | ✅ 含/不含成本 | ⚠️ 加密不支持回测 |
| **RD-Agent** | qlib `market: csi300`, `freq="day"` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 反馈含 with_cost | ❌ 无加密入口 |
| **LLM-Alpha-Mining-Framework** | CRSP / WRDS 美股日频 + SP500 PIT 成分 | ❌ | ❌ | ❌ | ⚠️ 股票 PIT | ❌ | ⚠️ 自承无成本组合 | ❌ 需授权数据 |

关键源码：
- `crypto-factor-lab/factor_lab/mining.py:239` 的 `periods_per_year: int = 24 * 365`；`execution.py:16-18,96-101,88` 三段成本 + funding 成本 + 下一根执行；`factors.py:45-60` funding z-score 延迟一根。
- `FactorMiner/core/data_feed/crypto_streams.py:47-55` 五类流 + 原生 8h + 仅 ffill 不 bfill；`core/miner/crypto_features.py` 的 funding/OI/basis/清算模板；`core/backtest/backtest_engine.py:362-369,389,437` 下一根收盘成交、固定 funding、手续费+滑点。
- `alpha-factor-mining-crypto/src/alpha_mining/data_loader.py:36-55` 当下成交额 top30 → 明确的幸存者偏差；`backtest.py:88-93` 数据驱动年化。
- `AlphaForge-Crypto/run_backtest.py:9` 导入不存在的 `fetch_ohlcv`；`:32-41` 回测全 TODO。

### 3.2 能直接用于加密的只有三家，且各有硬伤

1. **`crypto-factor-lab`**：唯一「能跑 + 统计纪律完整 + 加密原生」的组合。17 道闸门（原报告 §4.1.2），0 TODO / 0 `NotImplementedError`，18 个测试文件 78 用例，CI + LICENSE。**硬伤**：BTC 单品种、21-op DSL 只有 funding 一个加密字段、无 OI/basis、`liquidity_universe` 只在横截面实验调用、成本未进 `mine_candidates` 主评分、以及 §3.3 的 ledger 覆盖不全。
2. **`FactorMiner`**：数据面最宽（20 币 + 60 个原生流 + 5 类加密特征模板已作为 GP/LLM 种子接入）、证伪闸门最全、工程规范齐全。**硬伤**：§3.4 的半接入状态与正向 OOS 未验证。
3. **`alpha-factor-mining-crypto`**：13 核心因子 + 40 窗口 + 40 组合，加密因子清单最实用，可跑。**硬伤**：样本内选因子 + 样本内回测（`pipeline.py:225-230,301`）、无 FDR/purge/OOS、快照选币幸存者偏差、funding 只做因子不做成本、无测试、无 LICENSE。

### 3.3 两处修正原报告口径的新证据

**（1）`crypto-factor-lab` 的 holdout ledger 只在 HTTP 链路生效**

原报告 §0.2 发现 2 写「用账本记录每个 dataset 已揭示过的 holdout 区间，重叠即抛异常」，并据此立了我们的 P0-1。本轮全仓检索 `latest_revealed_holdout_end|holdout_end` 只命中：

```
factor_lab/catalog.py:282-299        定义
factor_lab/service.py:510, 900-905   唯一两处调用（HTTP service）
factor_lab/service.py:974, 992       写 promotion_result 与 holdout_end
tests/test_service.py:253            测试
scripts/run_factor_mining.py        ❌ 不调用，也不写 holdout_end
```

即 **CLI 主入口 `run_factor_mining.py:60-110` 只做 purge 切分 + FDR 筛选并 `save_result`，完全没有一次性揭示账本**。机制本身仍是 20 个外部框架里最好的设计，但「全链路强制」这一层它自己没做到。反观我们：`core/miner/director.py:50-51` 实例化 `ExperimentTrace` / `HoldoutLedger` / `FdrLedger`，`:127-142` 每次运行重算累积 FDR，`:188` 记录每一次尝试，`:289` 落盘——**闸门是主流程的一部分，不是可选 API**。这处口径修正应当回写原报告 §0.2 / §4.1.2。

**（2）qlib 有 crypto 采集器，但不能用于加密因子研究**

`qlib/scripts/data_collector/crypto/collector.py:17,38,138` 走 CoinGecko，字段只有 `prices / total_volumes / market_caps`；`crypto/README.md:12` 原文 "only support Data retrieval function but not support backtest function due to the lack of OHLC data"；`examples/` 全仓 crypto 零命中。qlib 的算子与缓存契约仍然值得抄（原报告 §2.1），但**不能把「qlib 支持 crypto」算作适配度**。这也是本次 C1 只给 1 分的原因。

### 3.4 FactorMiner 的加密适配是「半接入」状态

| 层 | 现状 | 证据 |
|---|---|---|
| 数据 | ✅ 20 币日线 + `crypto_streams/` 60 个文件（basis / funding_rate / open_interest 各 20），原生频率保留、加载时 past-only ffill | `core/data_feed/crypto_streams.py:22,47-55`；`data/binance/futures/crypto_streams/` |
| 特征模板 | ✅ funding / OI / basis / 清算四类共几十个 AST 模板，按实际存在的流过滤 | `core/miner/crypto_features.py:152-363,390` |
| 挖掘接入 | ✅ 作为 **seed** 接入 GP 与 LLM | `core/miner/paradigms/gp_miner.py:15-17,62`；`llm_miner.py:13` |
| **终端集** | ❌ 仍是 `["close","open","high","low","volume"]`，加密流**不是一等终端** | `gp_miner.py:23`、`rl_miner.py:19` |
| **年化口径** | ❌ 评价 / 显著性 / 组合层默认 `252.0`，与 7×24 不符 | `evaluator.py:567,656`、`significance.py:233,257`、`experiment_trace.py:351,373`、`portfolio_eval.py:46,171,267` |
| **funding 成本** | ⚠️ 固定 `funding_rate=0.0001` 由 `build_funding()` 生成，未用真实 8h 历史 | `backtest_engine.py:389,437` |
| **资产池** | ❌ 固定 20 币，`point-in-time` 全仓未找到，存在幸存者偏差风险 | `core/data_feed/` 全仓检索 |
| 成交 | ✅ 事件驱动 `ExecutionMode.NEXT_BAR` / `ExecutionPrice.CLOSE` | `backtest_engine.py:362-364` |

> **上一轮说「60 个流未接进终端」需要更精确**：它们已通过 seed 模板间接进入搜索，但没有进入 `TERMINALS_DEFAULT`，因此 RL/NN 范式与自由组合的表达式空间里仍然看不到 funding/OI/basis。这不改变结论（表达力差距仍在），但改变了修法的落点。

---

## 4. 双维度定位与排名

### 4.1 总评分表

分解列按 §1.3 顺序书写（M = M1+…+M5，C = C1+…+C5）。

| 框架 | M 分解 | **M** | C 分解 | **C** | 定位一句话 |
|---|---|:-:|---|:-:|---|
| qlib | 2+2+2+1+2 | **9** | 1+0+0+0+1 | 2 | 工程/生态第一，但股票日频、无挖掘器 |
| **FactorMiner** | 2+2+2+2+1 | **9** | 2+2+1+0+2 | **7** | 证伪 + 数据 + 工程最均衡，正向 OOS 未到手 |
| RD-Agent | 2+2+2+0+2 | **8** | 0+0+0+0+0 | 0 | 挖掘器工程第一，CSI300 硬编码、无统计闸门 |
| LLM-Alpha-Mining-Framework | 1+2+1+2+2 | **8** | 0+0+0+0+0 | 0 | 统计纪律与诚实度第一，美股 + WRDS 授权 |
| crypto-factor-lab | 2+1+2+2+1 | **8** | 2+1+2+1+2 | **8** | **加密象限唯一双高**，但仅 BTC 单品种 |
| gplearn | 2+1+2+0+2 | **7** | 1+0+0+0+1 | 2 | 成熟 GP 范本，市场无关 |
| trade-learn | 2+2+1+0+1 | **6** | 0+0+0+0+1 | 1 | 测试最多，但 0.3.x candidate、因子生成待实现 |
| CosmosAlpha | 2+1+2+0+1 | **6** | 1+0+0+0+1 | 2 | 搜索算法最丰富，同窗口评估无 holdout |
| alphalens | 2+1+2+0+1 | **6** | 0+0+0+0+1 | 1 | 单因子体检标准，2020 停更 |
| AlphaMaster | 1+2+1+0+1 | **5** | 1+0+0+0+1 | 2 | 产品化最强、证伪为零，外汇/贵金属定位 |
| AlphaGen | 1+1+2+0+1 | **5** | 1+0+0+0+1 | 2 | AlphaPool + 动作掩码的源头，股票 |
| AlphaForge | 1+1+2+0+1 | **5** | 1+0+0+0+1 | 2 | 逐期动态组合，股票 |
| WorldQuant-LLM | 1+0+2+0+1 | **4** | 0+0+0+0+0 | 0 | 在线闭环运维完整，依赖 WQ Brain |
| alpha-factor-mining-crypto | 0+0+2+0+1 | **3** | 2+1+1+0+2 | **6** | 加密因子清单实用，样本内评价 + 幸存者偏差 |
| gpquant | 0+0+2+0+0 | **2** | 1+0+0+0+1 | 2 | 60 算子范本，固定单次切分 |
| AlphaForge-Crypto | 1+0+0+1+0 | **2** | 2+1+0+0+0 | 3 | 数据层最全、因子层最空、跑不通 |
| Alpha-Research-Discovery | 1+0+0+1+0 | **2** | 0+1+0+0+0 | 1 | 单文件有 WF/DSR 亮点，整体无法运行 |
| alphaminingv2 | 0+1+1+0+0 | **2** | 0+0+0+0+0 | 0 | Agent 编排丰富，文档与代码漂移 |
| WorldQuant_alpha101_code | 0+0+2+0+0 | **2** | 0+0+0+0+1 | 1 | 公式参考实现 |
| KunQuant-rs | 0+0+1+0+0 | **1** | 0+0+0+0+1 | 1 | 仅 Rust FFI 绑定层 |
| EvoAlpha | 0+0+0+0+0 | **0** | 0+0+0+0+0 | 0 | prompt 包装骨架 |

### 4.2 象限解读

- **右上（双高）只有 `crypto-factor-lab`（8,8）与 `FactorMiner`（9,7）**，而且这两家的共同点不是「算法强」，是**把证伪机制写进了主流程**。
- **左上（高 M / 低 C）挤满了股票框架**：qlib、RD-Agent、LLM-Framework、gplearn、alphalens。**把 qlib 或 RD-Agent 改造成加密框架的成本，主要不在代码，而在数据契约（交易日历、7×24、funding、PIT 资产池）和统计口径**——这些恰恰是它们最硬编码的部分。
- **右下（低 M / 高 C）只有 `alpha-factor-mining-crypto`（3,6）**：能跑、加密原生、因子清单好，但零测试、零统计闸门、样本内自证。
- **左下**：加密专用框架里 `AlphaForge-Crypto`（2,3）与 `Alpha-Research-Discovery`（2,1）都在这里——**「README 写得最像加密货币系统」的两家，恰恰是跑不通的两家**。

### 4.3 排名结论

| 榜单 | 第一 | 第二 | 第三 |
|---|---|---|---|
| 工程/生态成熟度 | **qlib** (9) | FactorMiner (9) / RD-Agent (8) | LLM-Framework (8) |
| 挖掘器方法论成熟度 | **LLM-Framework** (8) / **FactorMiner** (9) | crypto-factor-lab (8) | RD-Agent 仅工程 (8/M4=0) |
| 加密货币适配度 | **crypto-factor-lab** (8) | **FactorMiner** (7) | alpha-factor-mining-crypto (6) |
| 加密 + 成熟的综合可用 | **FactorMiner** | crypto-factor-lab | 无可搬第三方 |

---

## 5. 选型建议

### 5.1 别找「现成的成熟加密因子框架」，这条路上没有货

20 个外部框架里，加密原生 4 家的平均 M 只有 **3.3**，M≥8 的 5 家平均 C 只有 **0.8**。这不是巧合：成熟框架几乎都长在股票市场的日频数据契约上（交易日历、复权、PIT 成分股、涨跌停），而加密的 alpha 来源恰恰在永续合约的**第二层数据流**（funding / OI / basis / 清算）和 7×24 时钟里。把股票框架移植过来，要改的是最底层的假设，不是加几个算子。

因此正确策略是：**主干用自己的，机制按点抄**。

### 5.2 对 FactorMiner 的补齐清单（按优先级）

| 优先级 | 事项 | 现状 | 落点 | 来源 |
|---|---|---|---|---|
| **P0-a** | **年化口径改数据驱动**：按 timeframe 推 `periods_per_year`（1h→8760、4h→2190、1d→365），禁止默认 252 | 6 处硬编码 252 | `core/evaluation/{evaluator,significance,experiment_trace,portfolio_eval}.py` | `crypto-factor-lab/mining.py:239` |
| **P0-b** | **一次性 holdout ledger 全入口生效**：确认所有 CLI 入口都经 `director.py`，不留「只在某个 API 生效」的分叉 | 已接入 director，但未做全入口审计 | `core/miner/director.py` + `core/cli.py` + 各 command | `crypto-factor-lab` 的反面（§3.3） |
| **P1-a** | **把 60 个流接进终端集**：让 funding/OI/basis 成为 GP/RL/NN 的一等终端，seed 模板保留为冷启动 | 只有 seed，`TERMINALS_DEFAULT` 仍 5 个 | `gp_miner.py:23`、`rl_miner.py:19`、`operator_runtime.py` | `AlphaMaster/features.py:1385-1466` |
| **P1-b** | **funding 成本改用真实 8h 历史**，与因子侧共用同一份数据，按 UTC 结算对齐 | 固定 `0.0001` | `backtest_engine.py:389,437`、`core/data_feed/` | `crypto-factor-lab/execution.py:96`、`binance.py:349-365` |
| **P1-c** | **point-in-time 资产池**：按每个历史时点的可得币种与流动性构造宇宙，消除快照幸存者偏差 | 固定 20 币 | `core/data_feed/` | `crypto-factor-lab/universe.py:8-23`（它自己也只在横截面实验用，别抄这个缺点） |
| **P1-d** | **非方向因子语义**：vol / funding / OI 类因子不硬套多空回测，标 `neutral` 等专用协议 | 一律套多空 | `core/evaluation/` + 报告层 | `crypto-factor-lab/suite.py:30-37` |
| **P2** | purge/embargo + 多段 roll-forward（逐折重拟合）、RL 动作掩码、`drop_forming` 契约、Hypothesis 属性测试 | 见 AlphaMaster 增量报告 §5 | — | `crypto-factor-lab` / `AlphaMaster` / `AlphaGen` |

> **P0-a 为什么排在前面**：它不改任何搜索逻辑，却同时影响 Sharpe、DSR、多重检验门槛与 promotion gates 的判定（`significance.py:233-257` 用 `sqrt(annualisation)` 把门槛换算成年化 Sharpe）。在 7×24 数据上用 252，等于把证伪标尺本身调歪——这类错误不会被闸门自己发现。

### 5.3 若一定要引入外部框架

- **作为下游回测/评价验证层**：`trade-learn`（Rust 事件驱动、1690 测试、MLflow、`CausalSelector`）与 `alphalens`（单因子体检）是唯二值得接的**组件**，但它们不是挖掘器，且 `trade-learn` 仍处 0.3.x candidate、68 处 `NotImplementedError`，接入前必须小样本跑通（原报告 §5.6）。
- **不要引入**：`AlphaForge-Crypto`（跑不通）、`Alpha-Research-Discovery`（跑不通 + 合成数据）、`EvoAlpha`（占位）、`AlphaMaster`（AGPL-3.0 传染性 + 实盘层已删 + 证伪为零）。
- **许可提示**：`LLM-Alpha-Mining-Framework` 与 `WorldQuant_alpha101_code` 未检出 LICENSE 文件，抄代码前需确认授权；只有 `qlib`(MIT)、`crypto-factor-lab`、`FactorMiner`(MIT) 等有明确许可的可直接借鉴实现。

---

## 6. 可信度与局限

- 本轮**未运行任何外部框架的挖掘或回测**。可运行性判断基于「主入口 import 链、依赖声明、TODO/占位、方法是否存在」的静态核查，不是实际执行结果；「能跑」指结构自洽，不等于数值结果可用。
- 全部绩效数字仍只引用作者自述或既有报告，**未做独立验证**。
- 21 个仓库中 13 个为 tarball 下载、**无 `.git`**，无法核对提交历史、CI 实际运行记录与 issue 讨论；「最后提交」对这些仓库标为 tarball。
- M / C 评分是本次人为分档（规则见 §1.3），**不同权重会改变名次但不会改变 §4.2 的象限结构**；请把它当相对定位工具，不是绝对度量。
- FactorMiner 的基线按 2026-11-14 当前源码（P0 闸门已落地、68 个闸门测试通过）；正向 OOS 证据仍缺（`TODO.md:52`，等 2026-12 之后的未揭示区间，当前 0/90 根）。
- 本次两处口径修正（§3.3）应回写原报告 §0.2 发现 2、§4.1.2 与 qlib 定位；本报告不直接改动历史报告的正文，以交叉引用方式保留当时的判断。

---

## 附录 A：关键源码引用索引

| 主题 | 路径:行号 |
|---|---|
| qlib crypto 采集器 / 不支持回测 | `qlib/scripts/data_collector/crypto/collector.py:17,38,138`、`crypto/README.md:12` |
| RD-Agent 硬编码 CSI300 / 日频 | `RD-Agent/rdagent/scenarios/qlib/factor_template/conf_baseline.yaml:5`、`.../factor_data_template/generate.py:7-9` |
| LLM-Framework 美股数据 / 诚实结论 | `LLM-Alpha-Mining-Framework/DATA.md:3-8`、`README.md:33-37,44-46,88-105` |
| crypto-factor-lab 年化 / 成本 / purge | `crypto-factor-lab/factor_lab/mining.py:40-61,239,378-394`、`execution.py:16-18,88,96-101`、`scripts/run_factor_mining.py:60-110` |
| **holdout ledger 仅 API 生效** | `crypto-factor-lab/factor_lab/catalog.py:282-299`、`service.py:510,900-905,974,992`；CLI 无命中 |
| alpha-mining-crypto 幸存者偏差 / 样本内 | `alpha-factor-mining-crypto/src/alpha_mining/data_loader.py:36-55`、`pipeline.py:225-230,301`、`backtest.py:88-93` |
| AlphaForge-Crypto 数据层 / 跑不通 | `AlphaForge-Crypto/src/data_fetcher.py:446,601,675,1375`、`run_backtest.py:9,32-41` |
| Alpha-Research-Discovery 跑不通 / WF+DSR 单文件 | `Alpha-Research-Discovery/alpha_01:169`、`alpha_16:163,192-202`、`walk_forward.py:403-425,465,477-481,230-289` |
| AlphaMaster 外汇定位 / 动作掩码 | `AlphaMaster/config.py:82-112`、`model_core/engine.py:145-215` |
| **FactorMiner 年化 252 六处** | `core/evaluation/evaluator.py:567,656`、`significance.py:233,257`、`experiment_trace.py:351,373`、`portfolio_eval.py:46,171,267` |
| FactorMiner 加密流 / 特征 / seed 接入 | `core/data_feed/crypto_streams.py:22,47-55`、`core/miner/crypto_features.py:152-363,390`、`gp_miner.py:15-17,62` |
| FactorMiner 终端集 / 成交 / funding 常数 | `gp_miner.py:23`、`rl_miner.py:19`、`core/backtest/backtest_engine.py:362-369,389,437` |
| FactorMiner 闸门接入主流程 | `core/miner/director.py:50-51,127-142,188,289` |

## 附录 B：关联文件

- 上游总报告（18+1 仓库）：`FactorMiner/docs/research/industry_factor_mining_survey.md`
- 增量报告（AlphaMaster）：`FactorMiner/docs/research/alphamaster_competitor_review.md`
- 闸门验证材料：`user_workspace/reports/gate_validation.md`、`p1_gate_upgrades.md`、`p0_holdout/README.md`、`TODO.md:47-66`
- 本报告：`FactorMiner/docs/research/framework_maturity_crypto_fit.md`

---

*本报告由源码实读 + 脚本化盘点整理。核心判断：最成熟的是股票框架，最适合加密的是小而专的研究闭环；两者都不构成可直接搬用的成品，FactorMiner 应继续走「主干自有 + 机制按点抄」的路线，且先修年化口径再谈扩表达力。*
