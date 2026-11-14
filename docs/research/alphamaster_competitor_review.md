# AlphaMaster 竞品源码层研究报告（第 19 个仓库 / 增量）

> **调研日期**：2026-11-14
> **调研对象**：`rosemarycox5334-debug/AlphaMaster`，源码落地 `D:\code\github\AlphaMaster`
> **版本**：`main` @ `3727daf2d2cb`（2026-11-07），**tarball 下载、不含 `.git`**；240 个文件 / 179 个 `.py` / 核心模块约 8,000 行
> **分析方式**：逐文件 `Read` / `Grep` / 关键片段复核计数；所有结论附 `路径:行号`；README 声明与源码实现不符处显式标注
> **对照基线**：`FactorMiner/docs/research/industry_factor_mining_survey.md`（下称「原报告」，18 个仓库）+ `D:\code\github\FactorMiner` 源码
> **本报告定位**：延续原报告口径的**增量**，新增一层产品化视角、一条实证病因链、以及若干实现级借鉴点

> **⚠️ 基线校准（2026-11-14 15:40 补记，重要）**：初稿沿用原报告 §7「统计闸门为零」的旧基线；复核当前源码后发现，**P0 全套闸门已于今日 10:22–11:29 落地并接入主流程**（提交 `a495e98` / `58b1779` / `c8befd6` / `4af6235` / `1f77b33`）：`core/evaluation/significance.py`（NW-HAC t、BH-FDR/Holm、随机公式零分布、DSR、promotion gates）、`core/evaluation/experiment_trace.py`（跨 run `num_trials` + 一次性 `HoldoutLedger`）、`core/miner/director.py`（累积 FDR + 假设预算）、静态 AlphaPool 与加密原生特征。闸门相关 5 个测试文件 **68 passed**（本次实测）。故 §3.3 / §4 / §5 中涉及 FactorMiner 的对比行已按**当前源码**改写。**结论随之改变：统计证伪能力现在是我们的核心优势，不是差距**；AlphaMaster 的借鉴价值集中在搜索机制与工程契约，而非闸门。验证材料见 `user_workspace/reports/gate_validation.md`、`factor_db_reaudit.md`、`p1_gate_upgrades.md`、`p0_holdout/README.md` 与 `TODO.md:47-66`。

> **⚠️ 二次校准（2026-11-14 17:5x 补记）**：本轮为 `platform_maturity_gap.md` 做全仓精确检索，本报告列举的「随机公式零分布 / 目标置换检验」需**收窄**：`core/evaluation/significance.py:147,169,208` 的三个函数（`target_permutation_tstats` / `random_formula_null` / `empirical_p`）**只有 `tests/test_significance.py:10-12,80-102` 引用，主流程零调用**，即「有实现 + 有单测、未接入 `apply_promotion_gates`」。其余闸门（NW-HAC t / BH-FDR / Holm / DSR / `num_trials` / 一次性 holdout ledger）均已接入主流程，§3.3 / §4 / §5 的对比结论不变，AlphaMaster 在该项仍是全仓零命中，我们仍领先，但优势小于本文初稿所述。另需澄清 `evaluator.py:398-470` 的 OOS RankIC 混合读取的是 **`test_period` 验证段**（`configOOSHoldout.json` = 2025-01-01..2025-06-30），最终 holdout（2025-07-01..2026-11-13）由独立脚本重算、不进 fitness，故**不是「holdout 被污染」**，而是「验证集反复复用、缺 gap/embargo 与逐折重拟合」。详见 `platform_maturity_gap.md` §4.1 / §4.2 / §3.1-A4。

---

## 0. 执行摘要

### 0.1 一句话结论

**AlphaMaster 是原报告 18 个框架里没有出现过的一类：把「单品种时序因子 + 单公式 tanh 仓位」做成了带 Web 控制台、断点续训、多数据源和实盘信号层的准产品；它的算子数量（62）、特征终端（65）和属性测试都领先我们，搜索机制（动作掩码）也有一处真值得抄。但在最关键的统计证伪层，它四条闸门全缺、验证集每步复用，而我们今早刚把这一层补齐并跑了 68 个测试。双方真正的差距是错位的：它强在「怎么搜、怎么交付」，我们强在「怎么证伪、怎么不骗自己」。作者自己在状态档案里承认「不是 alpha 而是 beta」，恰好演示了没有闸门时的结局。**

### 0.2 五个最重要的发现

| # | 发现 | 证据 |
|---|---|---|
| 1 | **验证集被每一步反复使用，且无任何多重检验折减**：`TRAIN_STEPS=9000 × BATCH_SIZE=192`（理论上界 172.8 万次候选），每个候选都在同一批 val 折上打分，每步用 `val_score` 刷新冠军、写盘、更新精英采样权重；全仓 `num_trials` / BH / Holm / DSR / 随机零分布 **0 命中** | `model_core/config.py:39-40`、`engine.py:876-912`、全仓 Grep |
| 2 | **作者主动证伪了自己的结果，且写进了仓库**：index 组因子因 `TS_RANK` 连续使用符号丢失，退化成 **91.8% 做多**的 beta 因子；metals 组年化 **+117.7%（虚高）**；等权组合 **+35.52% 被标注为「短数据放大」** | `model_core/config.py:147-148`、`strategies/STATUS_20250705.md:24,34,41-43` |
| 3 | **但产品话术层刻意淡化这一结论**：给 AI 分析器的 system prompt 明写「不要轻易下『过拟合』结论，尽量不要提过拟合」「不要因训练分远高于验证分就建议停止」——统计诚实与用户侧解说被拆成两套 | `web/ai_analyze.py:31,57` |
| 4 | **动作掩码的第二个独立实现，而且更接近我们的 RL 范式**：栈式 RPN + `ConstrainedSampler.valid_mask` 按 `delta=1-arity` 保证语法 100% 合法，叠加「恒正算子感染链」约束，非法位 logits 置 `-1e9`；我们原报告 P1 只列了 AlphaGen 一家 | `model_core/engine.py:145-215`、`vm.py:221-251`、`ops.py:699-701` |
| 5 | **Walk-Forward 名不副实，是一个值得警惕的实现陷阱**：5 折 rolling + `WF_GAP=20` 看似严谨，但**同一公式在全样本一次性算完，各折只做切片评分，没有逐折重拟合**；`test_walk_forward_gap.py` 只测索引算术，且断言与当前实现矛盾 | `engine.py:95-124,354,367-403`、`tests/unit/test_walk_forward_gap.py:45-51`、`model_core/config.py:140` |

### 0.3 对原报告结论的影响

| 原报告结论 | 本次结论 | 变化 |
|---|---|---|
| §0.1「缺统计显著性闸门与可复现的留出纪律」 | 对**业界**仍成立（第 19 个仓库四条全缺，并提供了「缺了会怎样」的完整病例）；但**对 FactorMiner 已过时**——今早 P0 已落地 | **基线校准** |
| §8 P0-1~P0-5 借鉴清单 | P0 闸门、静态 AlphaPool、加密原生特征均已实现并有验证报告；170 万级候选 / 0 分母成为我们 `num_trials` 机制价值的量级参照 | **已兑现** |
| §8 P1「RL 动作掩码，来源 AlphaGen」 | 新增第二个可抄实现（自回归 Transformer + logits mask + 感染链） | **补强** |
| §8 P0-4「多段 roll-forward」 | 新增实现陷阱：必须逐折**重拟合**，不能全样本算完再切片 | **修正** |
| §6.1「算子数量我们已进入第一梯队（59）」 | AlphaMaster 62 > gpquant 60 > 我们 59，但**窗口固化成 token、无参数化窗口**，数量第一 ≠ 表达力第一；真正的差距是它的 65 个特征终端 vs 我们 OHLCV 5 个 | **口径修正** |
| §1.1 全景图 L0–L4 | 需补 **L5 产品化 / 交易终端层**（Web 控制台、数据源、信号与风控），这是原 18 个框架基本没有的维度 | **新增维度** |

> ⚠️ 原报告 §0.3 末尾「不建议先做的」清单（扩算子、堆范式、逐时间截面时变权重）在**闸门已落地但正向 OOS 尚未验证**的当下**依然成立**：`TODO.md:52` 的 P0.5 正向验证仍等 2026-12 之后的新区间（当前 0/90 根）。AlphaMaster 恰好演示了「工程很完整、闸门为零」的结局，我们不该在正向证据到手前反向走回它的路线。

---

## 1. 仓库概况与定位

### 1.1 元数据与下载方式

| 项 | 值 |
|---|---|
| 上游 | `rosemarycox5334-debug/AlphaMaster` |
| 描述 | MT5 factor mining, backtest, and web training UI |
| 许可 | **AGPL-3.0**（若借鉴其代码到 FactorMiner，需注意传染性条款；本报告只做只读研究） |
| 语言 / 规模 | Python，`size=81536 KB`，240 文件 |
| star / fork / issue | 720 / 259 / 3 |
| 创建 / 最后推送 | 2026-07-10 / 2026-11-07 |
| 默认分支 | `main`，最新 commit `3727daf2d2cb`（"Update ai_providers.py"） |

**下载方式**：本机 `git clone` 走默认代理 `127.0.0.1:57642` 时 CONNECT 隧道 502；换 `127.0.0.1:7890` 后 HTTPS 网页可通但 `git` 协议仍 connection reset。最终沿用原报告 §1.2 记载的办法，用 `codeload.github.com/.../tar.gz/refs/heads/main` 下载（18 MB）解压，故**无 `.git`、无法核对提交历史与 CI**。commit 号由 GitHub API 的 `/commits/main` 单独取得。

### 1.2 在流水线里的位置（原报告 §1.1 的扩展）

```
L0 数据 / 表达式执行    qlib · KunQuant-rs · alphalens · 【AlphaMaster: 65 特征 + StackVM】
L1 公式化挖掘器          gplearn · gpquant · AlphaGen · AlphaForge · 【AlphaMaster: REINFORCE+Transformer】
L2 LLM / Agent 挖掘      RD-Agent · EvoAlpha · ...  ← AlphaMaster 的 LLM 只做解说，不在此层
L3 加密货币 / 多品种      crypto-factor-lab · ...  ← AlphaMaster 是 MT5 外汇/贵金属/指数为主
L4 评价 / 统计闸门        crypto-factor-lab ★★★ · LLM-Framework ★★★ · 【AlphaMaster ★（启发式）】
L5 产品化 / 交易终端      【AlphaMaster ★★★】Web 控制台 · 6 类数据源 · 信号/风控 · 断点续训
```

**关键定位差异**：原 18 个框架绝大多数是「横截面多因子 → 组合 → IC 评价」；AlphaMaster 是**单品种（或小组）时序 → 单条公式 → `tanh` 连续仓位 → 直接优化回测收益**。它没有组合层、没有因子池的权重优化，搜索目标直接是年化收益。`strategies/best_forex.json` 这类产物是 8 个 token 的一条公式，不是一个因子库。

### 1.3 目录与规模

| 目录 | 文件数 | 作用 |
|---|---|---|
| `model_core/` | 12 | 特征(1500行) / 算子(737) / VM(251) / 引擎(1362) / 评价(966) / 回测(665) / 生成器(287) |
| `web/` | 32 | FastAPI 单页控制台、AI 解说、6 类数据源、训练/回测/实时管理器 |
| `tests/` | 38 | 33 个 `test_*.py`、**235 个 `def test_`**（unit 21 / property 9 / smoke 3） |
| `docs/` | 21 | 1 个 docx 教程 + 20 张截图，**0 个 markdown** |
| `strategy_manager/` | 8 | 信号 / 风控 / 持仓 / runner（实盘执行层已被移除） |
| `data_pipeline/` | 7 | Parquet 缓存、K 线拉取、多品种时间轴对齐 |
| 根目录一次性脚本 | ~50 | `check_ckpt.py`→`check_ckpt5.py`、`analyze_*`、`backtest_*`、`deep_*`、`verify_*` 等 |

> 对照：FactorMiner `tests/` 25 个文件 / 287 个 `def test_`，**数量略多但未使用 Hypothesis 属性测试**；AlphaMaster 有 9 个 property 文件、约 30+ 属性、每属性 30–100 examples。这是它比我们强的一块工程。

---

## 2. 源码层详解

### 2.1 算子 / 表达式 / StackVM

**算子 62 个**（本次用正则对四张注册表逐条计数：`_INITIAL_OPERATORS` 44 + `_CROSS_SECTIONAL_OPERATORS` 3 + `_TASK33` 8 + `_TASK34` 7，`ops.py:365-423,431-435,545-557,690-703`，下界断言 `:734-737`）：

| 类别 | 数量 | 代表 |
|---|---|---|
| 时序 | 40 | `TS_MEAN_5/10/20`、`TS_CORR_10`、`TS_RANK`、`TS_ARG_MAX/MIN_5`、`EMA_5/20`、`DECAY_LINEAR_5`、`MOMENTUM_5/10`、`COVARIANCE_10`、`HURST` 相关 |
| 截面 | 3 | `CS_RANK` / `CS_SCALE` / `CS_NEUTRALIZE` |
| 算术 / 逐点 | 13 | `ADD SUB MUL DIV NEG ABS SIGN MIN MAX POWER SIGNED_POWER_2 SIGNED_LOG SQRT` |
| 条件 | 2 | `GATE`、`IF_GT`（arity 3） |
| 裁剪 / 激活 | 4 | `WINSORIZE`、`CLIP`、`SIGMOID`、`TANH_SQUASH` |

- **逻辑算子为 0**：`GT/LT/AND/OR` 因输出 0/1 被主动删除（`ops.py:699-701`）；`vm.py:127-130` 注释里的「66 算子 / 131 词表」已过时，实际词表 65 特征 + 62 算子 = **127**（`vocab.py:80-81`）。
- **无参数化窗口**：窗口被固化成独立 token（`TS_MEAN_5/10/20`），生成器只输出 `vocab_size` 个 logits、没有参数头（`alphagpt.py:154,258,283`），VM 只按 `arity` 弹栈、不接收窗口实参（`vm.py:232-238`）。**这是 62 > 59 却不能读作「表达力更强」的原因**：我们 `OPERATOR_PARAM_SPECS` 给 30 个算子连续/离散采样窗口，组合空间大得多。
- **表示是 RPN token 序列，不是 AST**：`vm.py:221-251` 特征压栈 `feat_tensor[:,token,:]`（`:226-229`），算子按 `arity_map` 弹栈、`args.reverse()` 保序（`:232-238`），末尾要求 `len(stack)==1`（`:244`）。无类型系统，值恒为 `Tensor[N,T]`；只有形状校验 `_with_shape_check`（`ops.py:340-357`）。
- **动作掩码是它最值得抄的一段**：`ConstrainedSampler.valid_mask` 用 `delta = 1 - arity` 维护栈深，配合「剩余步数能否归约到 1」的 `min_future/max_future` 剪枝（`engine.py:151-157,176-186`），再叠加「恒正算子感染链 ≥2 禁止传播算子 / ≥3 强制恢复」的符号约束（`:190-196`），非法位 `masked[b][~vmask] = -1e9`（`:214`）。这套约束正是为了修 §2.4 那个 beta 退化 bug 后加的（`config.py:146-154`）。
- **常量无独立 token**（`DIV` 内硬编码 `+1e-6` `ops.py:370`）；非法公式由 `vm.execute` 静默 `try/except` 返回 `None`（`vm.py:223,250-251`），engine 给 `-5.0/-2.0` 的负 reward（`engine.py:356-361,434-437`）。词表用 `sha256("\n".join(names))[:12]` 做版本戳（`vocab.py:48-58`），ckpt 加载时校验版本（`engine.py:1322-1329`）——**这个契约我们值得抄**，我们重算 OOS 因子时靠手写 `WARMUP_START`，没有算子集版本校验。
- 特征 **65 个**（`features.py:1385-1466`，`INPUT_DIM` 由注册表长度自动赋值 `:1500`）：8 类（trend / momentum / volatility / volume / reversal / channel / statistical / cross_sectional），含 `GK`/`PARKINSON`/`YANG_ZHANG`/`RS_VOL`/`HURST_50`/`AMIHUD_ILLIQ`/`CS_RANK_RET5` 等。**因果性做得干净**：全部左 zero-pad + `unfold`（`features.py:38,52,1031`）、前收盘用 `cat([:,:1],[:,:-1])`（`:59,707`）、`_robust_norm` 只用 `[t-199..t]` 的 median/MAD（`:158-168`）、warm-up 输出 0（`vm.py:216-217`）；数据层 **ffill 禁 bfill** 防未来值回填（`data_pipeline/data_manager.py:248`）。

### 2.2 搜索算法：REINFORCE + 自回归 Transformer（不是 PPO，也不是 LLM 挖因子）

| 项 | 实现 | 证据 |
|---|---|---|
| 生成器 | `AlphaGPT` = LoopedTransformer（`nn.MultiheadAttention` + causal mask），逐 token 自回归出长度 8 的公式 | `alphagpt.py:164-216,219-287`；`config.py:41` |
| 采样 | `Categorical(logits).sample()` + 上述动作掩码 | `engine.py:716-734` |
| 策略更新 | **标准 REINFORCE**：`policy_loss = Σ(−logπ · adv)`，`adv = (r − EMA_baseline)/(std+1e-5)`，`loss = policy_loss − ent_coeff·mean_ent + ent_floor_loss`，AdamW lr=1e-3、grad clip 1.0 | `engine.py:919-929,933-941,955-965,242,969` |
| 规模 | 9000 步 × 192 公式/步（其中精英回放占 0.25），无 epoch / 数据集迭代 | `config.py:39-40`、`engine.py:687-689,707` |
| 类遗传部分 | Top-60 精英最小堆、软温度采样、参数高斯扰动 / 全重置重启 | `engine.py:511-536,754-766,1084-1148` |
| Island | N 个独立引擎串行训练 + 每 500 步 Top-5 精英迁移；`N_ISLANDS=1` 默认关，**明确不是 multiprocessing** | `island_engine.py:8,34-40,47-81,94-117`、`config.py:126` |

> **死代码 / 名不副实**：`alphagpt.py:110-116,143-161,259-261` 的 critic、MTP 多任务头、QKNorm 均已简化或弃用；`config.py:156-168` 记录了线程池并行评估「实测慢 55%–69%」而关闭——**这段带基准数字的注释是很好的工程习惯**。

**LLM 的位置必须澄清**：全仓 `/chat/completions` 调用只有 2 处（`web/ai_providers.py:367`、`web/ai_fallback.py:108`），`model_core/` 与 `train_file.py` **没有任何 LLM 调用**。`web/ai_analyze.py:18-66` 的 prompt 是「基于训练快照用中文给出分析」，`build_training_snapshot` 只读进度 / 公式 / 曲线（`:148-194`），结果写进 `ai_analysis_history.json` 并 SSE 回显（`:303-335`）。**所以它是 L2 层之外的「AI 解说员」，不是 LLM 挖掘框架**——原报告第 3 章的 LLM 组不因它扩容。5 层 fallback 链（primary → 方舟备用池 → Kilo 免费池 → 配置兜底 → 本地 openclaw，`ai_fallback.py:192-318`）是产品韧性设计，与因子质量无关。

### 2.3 评价与回测：**有两套评分器，别搞混**

- `model_core/evaluator.py`（966 行）是因子描述性评估器：`score = 0.6·|RankIC| + 0.4·MI`（`:307-354`），`score_all` 再做跨候选秩归一（`:357-411`），有 `_align_causal` 裁末端 h 步防前视（`:91-118`）。**但全仓只被 `prune_features.py:78` 与测试引用，不进入训练主循环**。
- 真正驱动搜索的是 `model_core/backtest.py` 的 `MT5Backtest._multi_objective`（`:503-626`）。默认（N>1）权重：

```
score = 0.60·年化收益 + 0.10·Sortino + 0.05·Calmar + 0.10·时序IC
      + 0.05·品种一致性 + 0.05·成本压力 + 0.05·换手质量
      + 稀疏惩罚 + beta 中性惩罚 + 前后半一致性奖惩          # backtest.py:615-626
FTMO 模式：0.75·年化收益 + 0.10·Calmar + ...                  # backtest.py:600-613
```

文件头 `:5-13` 注释写的「0.35 sortino + 0.20 calmar + … − complexity」**是过时文档，与代码不符**；复杂度惩罚实际不存在，只有长度上限 8、相邻重复 −0.3（`engine.py:127-138`）、相关性 >0.85 时 reward ×0.8（`engine.py:569-589`）。

- **成本入 fitness 做得比我们好**：`pnl = pos·ret − turnover·cost_rate`（`backtest.py:429,644`），单边 `COST_RATE=0.0003` 与生产回测口径统一（`config.py:139-145`），另有 2× 成本压力测试（`:269-285`）、换手惩罚（`:394-402`）。原报告 P1 里「把 `turnover_penalty` 默认开启」的建议，这里是一个已跑通的参照实现。
- **但主目标是「回测年化收益」而非 IC**：搜索直接攻击回测口径，等于把过拟合的自由度开到最大。结合 §2.4 作者自述的 beta 退化，这条路线本身就是病因的一部分。我们的 RankIC 几何混合 fitness 虽然也过拟合，至少不直接对收益曲线做梯度上升。
- **OOS 门控是启发式、不是统计检验**：`evaluate()` 按 80/20 时序切分，前 80% 打分，后 20% 只用一个 OOS Sortino 把分数乘 `0.1~1.2`（`backtest.py:646-662`）；`evaluate_fold()` 同理（`:408-462`）；`engine.py:880-887` 用 `val/train < 0.5` 判过拟合并「跳过冠军」。三者都是阈值规则，没有 p 值、没有分母、没有置信度。

### 2.4 防过拟合：最核心的一节

#### 2.4.1 逐条核查（对齐原报告 §6.3 表头）

| 机制 | AlphaMaster | 证据 |
|---|---|---|
| purge | ❌ **未找到** | `config.py:140` 自注「CPCV 模式（**未实现**）的 purge gap 应等于 WF_GAP」 |
| embargo | ❌ **未找到** | 同上；val 之后无隔离期 |
| gap | ✅ `WF_GAP=20` | `engine.py:115`、`config.py:137-144`（并注明 gap 需按 `target_horizon=2` 标定） |
| walk-forward 多段 | ⚠️ **5 折但名不副实** | `engine.py:95-124` rolling + gap；但 `:354` 同一公式全样本一次性算完，`:367-403` 各折只切片评分，**无逐折重拟合** |
| BH / Holm / Bonferroni / FDR | ❌ **未找到** | 全仓 `--include=*.py` 精确 Grep 0 命中 |
| Deflated Sharpe / PSR | ❌ **未找到** | 同上 |
| 随机公式零分布 / 置换检验 | ❌ **未找到** | 同上；`lord/experiment.py` 的 `random.shuffle` 是训练数据打乱，不是检验 |
| `num_trials` 试验次数分母 | ❌ **未找到** | `training_history` 只按 step 记（`engine.py:273-275,1019-1036`） |
| 一次性 holdout 揭示账本 | ❌ **未找到** | 反而 val **每步**被选优 + 写盘（`engine.py:879-912,905`） |
| 成本入 fitness | ✅ | `backtest.py:644`、2× 压力 `:269-285` |
| 防未来函数 | ✅ 特征/数据/成交三层 | `features.py:158-168`、`data_manager.py:248`、`backtest_viz/engine.py:144-149,218-235` |
| 一致性 / beta 启发式 | ✅ 但非统计 | 前后半同号 +0.5 异号 −1.0（`backtest.py:361-379`）、同向 >85% 重罚（`:341,378`） |

> **全仓精确检索**（`bonferroni|holm|fdr|deflated|num_trials|n_trials|permutation_test|reality_check|pbo|embargo|purge`）只命中 4 处：两行「CPCV 未实现」注释、`times.py:376` 一个不属于主流程的 `final_reality_check`、以及一个 zipfile 变量。四条业界最严谨闸门**全缺**。

#### 2.4.2 病因链（本次调研最有价值的实证）

```
9000 步 × 192 公式/步            ← 理论 172.8 万次候选，无 num_trials 记录
        ↓
每个候选在【同一批 val 折】打分，且 val 与 train 只隔 20 bar、无 purge/embargo
        ↓
每步 val_score 刷新冠军 → 写盘 → 更新精英回放采样权重  ← engine.py:876-912
        ↓
无 BH-FDR / 无 DSR / 无随机零分布 / 无一次性 holdout
        ↓
max(170 万个噪声) 被当成 alpha   →  搜索直接优化 0.60~0.75 权重的回测年化收益
        ↓
结果：index 组退化成 91.8% 做多的 beta；metals 年化 +117.7% 虚高；组合 +35.52% 靠 1.37 年短数据放大
        ↓
作者自己在 STATUS_20250705.md 标注「❌ 不建议单独使用」「不是 alpha 而是 beta」
```

这与原报告 §7.5 我们的 `OOS -11.18% / Sharpe -1.14` 是**同一个失败模式**：搜索强度足够、证伪机制为零。差别只是我们做了严格 OOS 并当场发现，AlphaMaster 是在策略状态档案里事后诊断。

#### 2.4.3 值得记下的一处反差

- `strategies/STATUS_20250705.md:23-26,34-37,41-43` 与 `config.py:147-148`：作者**主动写下**「虚高」「不是 alpha 而是 beta」「短数据放大」「不建议单独使用」，并据此加了 beta 中性惩罚、前后一致性奖惩、算子链感染约束。**这是原报告 §10.3 所说「能诚实证伪自己」的第二个案例，态度值得学。**
- 但 `web/ai_analyze.py:31,57` 给面向用户的 AI 分析器写的是「**不要轻易下『过拟合』结论，尽量不要提过拟合**」「**不要因训练分远高于验证分就建议停止**」。内部诊断诚实、对外解说淡化——这种分裂本身比技术缺陷更值得警惕：**如果我们的报告层也允许「选择性呈现」，P0 的统计闸门会被话术架空。**

#### 2.4.4 测试与审计工具的真实覆盖

- 33 个测试文件 / 235 个 `def test_`，其中 9 个 Hypothesis 属性测试文件，覆盖特征形状与有界性、IC 算术、WF 索引、采样器可执行性、快照 round-trip 等。**但「扰动未来 bar 后历史因子不变」这类因果断言未找到**；`tests/property/test_runner_props.py:185` 出现 `if False`、`:192-194` 出现 `or True`，属恒真空断言。
- `tests/unit/test_walk_forward_gap.py` 只测 `_build_walk_forward_folds` 的索引算术（`:19-28,40-60,102-177`），`:10` 还硬编码了外部路径 `d:\cl\MT5_AlphaGPT`，且 `:45-51` 断言 `train_start` 恒为 0，与当前 `engine.py:113` 的 rolling window **互相矛盾**——测试已与实现脱节。
- `rigorous_backtest_audit.py`（395 行）自称「不复用训练 reward，从第一性原理计算 PnL」（`:4`），含 WF 4 折、1/2/3/5× 成本压力、前后半段、VALID/SUSPICIOUS/INVALID 判定（`:117-198`），是本次 19 个仓库里少见的「独立审计器」思路。**但它是手动 CLI，不被主流程调用**（`:394-395`）。这个「审计器与训练器解耦」的架构值得我们抄，只是要让它自动跑、并加上显著性判定。
- 指标口径有分叉：索提诺下行 std 地板在 `backtest.py:153-159`（全序列 std×20%）、`backtest_viz/engine.py:295`（`abs(mean)`）、`run_backtest.py:69`（`max(ds,abs(m))`）三处不同；年化 `ppy` 在 `backtest.py:33-120` 数据驱动，而 `backtest_detailed.py:27`、`rigorous_backtest_audit.py:26` 写死 6240。

### 2.5 产品化层（原 18 框架基本空白的一维）

| 能力 | 实现 | 证据 |
|---|---|---|
| Web 控制台 | FastAPI + 单页 SPA（Chart.js），约 39 个唯一 URL；训练/回测用 `subprocess.Popen` 独立进程，实时用 daemon 线程 + `ThreadPoolExecutor(4)`；无 WebSocket，前端轮询日志、正则解析 `[n/N]` 步数 | `web/app.py:60,1123-1128`、`training_manager.py:105-134,157-178`、`realtime_manager.py:203,331` |
| 断点续训 | 每 20 步原子写 ckpt，存模型+优化器+两个池+best_snapshot+history；词表版本戳校验；`--from-scratch` 时把旧策略分数**注入为 `best_score` 下限** | `engine.py:1053-1055,1286-1352`、`train_file.py:67-84,112-131,138-162` |
| 训练包导入导出 | zip 打包 `.pt` + 策略 + 曲线 + manifest，导入校验 `vocab_version` 与品种一致性 | `web/training_package.py:50-93,104-216` |
| 数据源 | MT5 / TradingView / OKX / A股 / 通达信 / 国内期货 6 类；时间轴交集对齐，不足时降级「并集 + 仅 ffill」；A股 `qfq`、期货前复权；**`drop_forming=True` 贯穿所有源，只用已收盘 K 线** | `web/data_sources/factory.py:18-37`、`base.py:55-60`、`data_manager.py:201-265`、`realtime_manager.py:93-110,367` |
| 信号口径统一 | `position = tanh(factor)` + `|pos|<0.05` 死区；`REBALANCE_ON_BAR_CLOSE=True` / `EXECUTION_LAG_BARS=1`；成交在下一根 open（`target_ret[t]=log(open[t+2]/open[t+1])`） | `signal.py:24,48,50-51`、`config.py:196-197`、`backtest_viz/engine.py:144-149,218-235` |

**两个可直接搬的契约**：① `drop_forming=True` 作为数据源基类的强制行为，从根上消除「未收盘 bar 参与信号」这类回测/实盘不一致；② 训练、回测、实时三端共用同一个 `compute_target_positions_stateless`，口径由配置单点控制（虽然 `backtest_viz/engine.py:129-130` 漏了 0.05 死区，是没守住契约的反例）。

**但链路是断的**：`execution/` 只有 `config.py` 与 `price_feed.py`，真正下单的 `trader.py` 已移除；`strategy_manager/runner.py:43-84` 用占位类，注释明写「本项目不需要自动交易功能」，所有 buy/sell/connect 抛 `RuntimeError`，`run()` 连接即 `sys.exit(1)`。而 `README.md:80` 仍把 `execution/` 描述为「MT5 下单接口」。Web 的「实时分析」只算方向和强度、方向转折推飞书，不下单。

### 2.6 工程成熟度与风险

- **强项**：235 个测试含属性测试、pydantic 请求模型、`from __future__ import annotations`、全局异常中间件、子进程独立日志、设置损坏回退（`web/settings.py:109-165`）、带基准数字的并行评估决策注释。
- **弱项**：**无 CI**（全仓无 `*.yml/*.yaml/pyproject.toml/pytest.ini`）；`docs/` 只有 1 个 docx + 20 张截图、0 篇 markdown；根目录约 50 个一次性脚本（`check_ckpt.py` 连号到 5）；`.git-rewrite/` 历史重写残留与 `fetch_all_log.txt` 被提交；时间戳修复笔记入库。
- **安全项（脱敏提示，不展开）**：`web/settings.py:30-31` 明文硬编码某数据源账号口令；`web/training_package.py:164-166` 解压 zip 时直接 `PROJECT_ROOT / member` 未做 zip-slip 校验，且 `:37` 使用 `torch.load(weights_only=False)`；`web/app.py:164` 把 traceback 回传客户端；CORS 全开（`app.py:61-66`）。**若我们照搬「训练包导入」功能，这三点必须一起修。**
- **整体判断**：研究型框架 + 个人实验脚本集合 + 一个做得相当完整的单机 Web 工具，**不是产品化交易系统**（绑定 127.0.0.1、无鉴权、无部署配置、实盘层主动删除）。

---

## 3. 横向矩阵增量（接原报告 §6）

### 3.1 §6.1 算子表新增一行

| 框架 | 可调用算子数 | 参数化窗口 | 截面算子 | 条件/分支 | 常量终端 | 加密专用字段 |
|---|---|---|---|---|---|---|
| **AlphaMaster** | **62** | ❌ 窗口固化成独立 token | ✅ 3 | ⚠️ `GATE` / `IF_GT` 2 个 | ❌ 常量硬编码在算子内 | ❌ |
| **FactorMiner** | 59 | ✅ 30 个带参 | ✅ 8 | ❌ 仅 `greater/less` | ✅ ephemeral | ❌ 仅 OHLCV |
| gpquant | 60 | ✅ 时序常数 | ❌ | ✅ 3 | ✅ | ❌ |

> **口径修正**：按「可调用算子名」AlphaMaster 62 居首，但按「表达式组合空间」它受限于 8 token 定长 + 无参数头，实际远小于我们。原报告 §6.1 的结论「算子数量已进入第一梯队」不变，但今后应同时标注「窗口是否参数化」，否则会被数字误导。另：**65 个预计算特征终端** vs 我们 `TERMINALS_DEFAULT` 只有 OHLCV 5 个——这是真正的表达力差距，值得进 P1 清单（见 §5）。

### 3.2 §6.2 挖掘方法与目标函数新增一行

| 框架 | 搜索算法 | 优化目标 | 组合层 | 表达式表示 |
|---|---|---|---|---|
| **AlphaMaster** | REINFORCE + 自回归 Transformer + 动作掩码 + 精英回放 | **0.60~0.75 权重的回测年化收益**（+ Sortino/Calmar/IC/成本/一致性） | **无**（单公式 → `tanh` 仓位） | 定长 8 的 RPN token 序列 |
| **FactorMiner** | GP + RL + LLM + NN | 样本内外 RankIC 几何混合 fitness | 样本内一次岭回归、固定权重 | AST dict |

**关键差异**：它把「搜索」和「交易」直接焊死（reward 就是回测收益），跳过了「挖因子 → 评价 → 组合」的分层。好处是目标端到端一致；代价是**搜索可以直接攻击回测口径的自由度**，而 β 退化正是这么来的。我们保留分层是对的，而且分层之上的证伪闸门今早已经补齐。

### 3.3 §6.3 防过拟合闸门新增一行

| 框架 | purge/embargo | 三段切分 | FDR/Holm | HAC t | DSR | 随机零假设 | 试验次数分母 | 成本入判定 | 一次性留出 | walk-forward |
|---|---|---|---|---|---|---|---|---|---|---|
| **AlphaMaster** | ⚠️ 仅 gap=20 | ✅ 80/20 + 5 折 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 单边 3bps + 2× 压力 | ❌ | ⚠️ 有折无重拟合 |
| **FactorMiner（11-14 当前）** | ❌ | ✅ IS / 验证 / 严格 holdout | ✅ BH + Holm + **累积 FDR** | ✅ NW-HAC | ✅ 折减净 Sharpe | ✅ `random_formula_null` | ✅ 跨 run `ExperimentTrace` + `FdrLedger` + 假设预算 | ✅ 收益口径成本闸门（非 fitness 内） | ✅ `HoldoutLedger` 重叠即抛异常 | ⚠️ 单次切分 + 一次性 holdout，无多段 roll-forward |

> **校准后的读法**：AlphaMaster 比我们多出 gap、多段折、成本入 fitness、OOS 门控、beta 惩罚、因果特征契约和独立审计 CLI；**但决定性的统计校正四列（随机零假设 / 试验次数分母 / 一次性留出 / FDR+DSR）现在是我们的优势，它一条都没有。** 我们真正还没补的是 **purge/embargo 与多段 roll-forward**（原报告 P0-4）。这也说明：中间层启发式做得再全，替代不了顶层的证伪机制；反过来，顶层闸门也替代不了干净的时间隔离——后者是下一步该补的。

---

## 4. 与 FactorMiner 的逐项对照

| 维度 | FactorMiner（11-14 当前） | AlphaMaster | 谁领先 |
|---|---|---|---|
| 算子 | 59，30 个参数化窗口 | 62，窗口固化 | 我们（组合空间） |
| 输入终端 | OHLCV 5 个（**另有 60 个 funding/basis/OI 流已在盘上、未接进终端**） | **65 个预计算特征** | **它** |
| 挖掘范式 | GP / RL / LLM / NN 四种 | 单一种（REINFORCE+Transformer） | 我们 |
| 表达式表示 | AST dict | 定长 RPN + StackVM + 词表版本戳 | 各有取舍 |
| 采样合法性 | 折叠合法 AST，无掩码 | **动作掩码 100% 合法 + 感染链约束** | **它** |
| 搜索目标 | 样本内外 RankIC 混合 + 收益口径闸门 | 回测年化收益（0.60~0.75） | 我们（不直接攻击回测口径） |
| 组合层 | **静态 AlphaPool 已落地** + 岭回归，权重贯通回测 | 无组合（单公式 → tanh 仓位） | **我们** |
| 时间隔离 / 启发式 | 一次性 holdout、预注册窗口；**缺 purge/embargo 与多段 roll-forward** | gap=20 / 5 折（无重拟合）/ OOS 门控 / beta 惩罚 / 成本入 fitness | **它（中间层）** |
| **统计证伪** | **NW-HAC t + BH/Holm + 累积 FDR + 随机零分布 + DSR + promotion gates + `num_trials` + holdout ledger**（68 测试通过） | **全部为零** | **我们，且是最大差距** |
| 回测成交 | 次日收盘 | **下一根 open** + 2 步前瞻标签 | 它更保守 |
| 数据/实盘契约 | 事件驱动、funding、数据质量前置校验、新鲜度视图 | `drop_forming=True` 全源强制、三端共用信号函数 | **它（收盘 bar 契约）** |
| Web / 产品化 | Web UI + 组合回测页 + `/api/gates/budget` | 39 端点 SPA、断点续训、训练包导入导出 | **它（训练工作流）** |
| 测试 | 25 文件 / 287 例（含 5 个闸门测试文件） | 33 文件 / 235 例 + Hypothesis 属性测试 | 我们数量、它性质覆盖 |
| CI / 部署 | `.github/workflows/ci.yml` + Dockerfile + compose | 无 CI、无部署配置 | **我们** |
| 许可 | MIT | AGPL-3.0 | 我们（借鉴它代码受限） |

> **一句话**：它在「搜索效率 + 交付体验」这条线上领先，我们在「证伪纪律 + 组合层 + 工程规范」这条线上领先；**而决定因子能不能用的那条线，现在是我们领先**。

---

## 5. 对原报告 P0 / P1 清单的影响

### 5.1 已落地，本次提供的是反例验证（不是待办）

- **P0-1 / P0-2 / P0-3 已于今早实现**：`HoldoutLedger`（重叠即抛 `HoldoutReuseError`）、跨 run `ExperimentTrace.num_trials` + `FdrLedger`、NW-HAC t / BH-FDR / Holm / 随机公式零分布 / DSR / promotion gates，且 `director.py` 每次运行都重算 `n_trials` 并输出假设预算告警。**AlphaMaster 的 170 万候选 / 0 分母正好是这套机制的必要性证明**：`gate_validation.md` 显示 6 个旧因子在 `n_trials=787` 下被 DSR 全部拦下，而用 `n_trials=1` 时有 5 个能过——分母就是结论。
- **仍可补的一个计数**：`ExperimentTrace` 记录了候选总数，但没记「**val 被读取次数**」。AlphaMaster 每步读 val、每步写盘，这个数字若也纳入报告，能更早暴露「验证集已被当成训练集用」。
- **P0-5 AlphaPool 也已落地**（`4af6235` 权重贯通回测），因此原清单里「从固定岭回归升级」这项不再是差距。

### 5.2 被修正的

- **P0-4 多段 roll-forward**：落地时必须**逐折重新生成/拟合因子**，AlphaMaster 的 `_build_walk_forward_folds` 只切同一份预计算结果，是明确的反面样板。另可把它 `config.py:137-144` 那条「gap 应按 `target_horizon` 标定」的注释写进我们的实现说明。
- **§6.1 算子口径**：表格增加「窗口参数化」列（已在 §3.1 做了）。

### 5.3 新增建议（原清单未覆盖）

| 优先级 | 建议 | 来源 | 落点 |
|---|---|---|---|
| **P1** | **RL 动作掩码第二实现**：栈深 `delta=1-arity` + 剩余步归约剪枝 + logits `-1e9`；可选抄「恒正算子感染链」防止公式退化为单边 beta | `engine.py:145-215` | `core/miner/paradigms/rl_miner.py`（原 P1 只列 AlphaGen，现可对照两版取舍：AlphaGen 是分类式 `validate_op`，AlphaMaster 是可微 logits mask，后者更适合我们若把策略网络升级） |
| **P1** | **词表 / 算子集版本戳**：`sha256(算子名序列)[:12]`，检查点加载时校验不匹配即拒载 | `vocab.py:48-58`、`engine.py:1322-1329` | `core/miner/operator_runtime.py` + `recompute_oos_factors.py`（替换手写 `WARMUP_START` 的前置校验） |
| **P1** | **扩充输入终端**：从 OHLCV 5 个扩到「预计算特征面板」（波动率估计量、Amihud 非流动性、Hurst、AROON、通道类），并保持全部因果 left-pad | `features.py:1385-1466` | `core/miner/` 新增 `feature_panel.py`（比再堆算子更能提升表达力） |
| **P1** | **独立审计器与训练器解耦**：单独 CLI 从第一性原理重算 PnL、做 1/2/3/5× 成本压力与前后半段一致性，输出 VALID/SUSPICIOUS/INVALID | `rigorous_backtest_audit.py:4,117-198` | `user_workspace/experiment_tools/` 新增 `audit_backtest.py`，**但必须自动接入报告流程**（它本仓库是手动跑的，别抄这个缺点） |
| **P1** | **成本入 fitness 单点来源**：`COST_RATE` 与生产回测的 commission+slippage 共用常量，避免训练/回测两套成本 | `config.py:139-145`、`backtest.py:644` | `core/evaluation/evaluator.py` |
| **P2** | **`drop_forming=True` 作为数据源基类强制契约**；训练/回测/实时共用同一 `compute_positions` | `web/data_sources/base.py:55-60`、`signal.py:48` | `core/data_feed/` + `core/backtest/` |
| **P2** | **属性测试（Hypothesis）**：对算子输出有界性、无 NaN、时间轴对齐、`factor[:,:-1]` vs `target[:,1:]` 错位生成随机样本断言 | `tests/property/*` | `tests/`（我们 287 例但无属性测试） |
| **纪律** | **报告层禁止话术降级**：不得出现「尽量不要提过拟合」这类面向展示层的抑制性指令；所有显著性结论必须原样出现在用户可见报告里 | `web/ai_analyze.py:31,57`（反面） | 报告生成模板 + review 规范 |

> **仍然不建议先做**：扩算子数量、堆范式、逐时间截面时变权重。**当前真正该做的两件事**：① `TODO.md:52` 的 P0.5 正向验证（等 2026-12 之后的未揭示区间），② 补 purge/embargo 与多段 roll-forward（§5.2）。AlphaMaster 把「工程很完整 + 中间层启发式很全 + 顶层证伪为零」的结局演示得足够清楚，而我们闸门虽已补齐，**正向证据尚未到手**，此时反向去堆表达力等于把搜索预算重新放大。

---

## 6. 反面教训（补入原报告 §10.2）

| 现象 | 证据 |
|---|---|
| **验证集每步反复使用、每步写盘**，无独立测试集、无一次性揭示 | `engine.py:879-912,905,1038-1049` |
| **Walk-Forward 有折无重拟合**，且单测断言与实现互相矛盾 | `engine.py:354,367-403`；`tests/unit/test_walk_forward_gap.py:45-51` |
| **fitness 权重文档与代码不符**（文件头 0.35 sortino vs 实际 0.60 年化） | `backtest.py:5-13` vs `:615-626` |
| **同一指标三套口径**（索提诺下行 std 地板、年化 ppy 写死 6240） | `backtest.py:153-159`、`backtest_viz/engine.py:295`、`run_backtest.py:69`、`backtest_detailed.py:27` |
| **README 称 `execution/` 是 MT5 下单接口，实际下单模块已删、runner 抛异常** | `README.md:80` vs `strategy_manager/runner.py:43-84` |
| **README 称「按验证集表现选优」，未披露每步复用 val 且无统计校正** | `README.md:19` vs `engine.py:876-912` |
| **测试含恒真空断言**（`if False` / `or True`） | `tests/property/test_runner_props.py:185,192-194` |
| **对外解说刻意淡化过拟合，与内部诊断结论相反** | `web/ai_analyze.py:31,57` vs `strategies/STATUS_20250705.md:34` |
| **作者主动承认结果不成立**（正面案例，第二次出现） | `strategies/STATUS_20250705.md:23-26,34-37,41-43`、`config.py:147-148` |
| 无 CI、50+ 一次性脚本、`.git-rewrite/` 与日志被提交、凭据明文与 zip-slip | 见 §2.6 |

---

## 7. 可信度与局限

- 本仓库通过 tarball 下载，**无 `.git`**，无法核对提交历史、CI 与 issue 讨论；commit 号来自 API 的 `main` 分支头。
- 核心模块（`engine.py` 1362 行、`features.py` 1500 行、`evaluator.py` 966 行、`backtest.py` 665 行）为逐行/分块实读 + 关键片段亲自复核；`web/` 与 `tests/` 为抽样实读 + 全仓 Grep 交叉验证。算子 62 / 特征 65 / 测试 235 三个数字均由脚本重新计数确认。
- 「未找到」均指在 `D:\code\github\AlphaMaster` 全仓 `--include=*.py` 精确检索无命中，**不等于上游绝对不存在**。
- **未运行任何训练或回测**（需 MT5 终端、Parquet 数据、PyTorch 环境）；仓库内所有绩效数字均为作者自述，本报告只做引用、不做验证。
- star 720 / fork 259 为 API 返回值，未做真实性判断。
- 涉及安全问题的凭据已脱敏，仅记录文件位置，不复现内容。

---

## 附录 A：AlphaMaster 关键源码引用索引

| 主题 | 路径:行号（相对 `D:\code\github\AlphaMaster\`） |
|---|---|
| 62 算子四张注册表 / 下界断言 | `model_core/ops.py:365-423,431-435,545-557,690-703,734-737` |
| 65 特征定义 / 因果滚动归一 | `model_core/features.py:1385-1466,1500`、`:38,52,59,158-168,707,1031` |
| StackVM 执行 / 结构校验 / 出口 zscore | `model_core/vm.py:221-251`、`:64-121`、`:182-219` |
| 词表版本戳 | `model_core/vocab.py:48-58,80-100` |
| **动作掩码** | `model_core/engine.py:145-215`（`valid_mask` `:172-201`、`-1e9` `:214`） |
| REINFORCE 损失 / EMA baseline | `model_core/engine.py:919-929,933-941,955-965` |
| Walk-Forward 折 / gap | `model_core/engine.py:95-124`、`model_core/config.py:137-144` |
| 验证集每步选优 + 写盘 | `model_core/engine.py:876-912,905` |
| 精英池 / 因子池 / 相关性软惩罚 | `model_core/engine.py:498-548,569-589` |
| 检查点存/取、词表校验 | `model_core/engine.py:1053-1055,1286-1352` |
| LoopedTransformer 自回归生成 | `model_core/alphagpt.py:164-216,219-287` |
| **实际 fitness 权重** | `model_core/backtest.py:600-626`（文件头 `:5-13` 已过时） |
| 80/20 OOS 门控 / 成本 / 一致性 | `model_core/backtest.py:646-662`、`:429,644`、`:269-285,361-379` |
| 描述性评估器（不进主循环） | `model_core/evaluator.py:91-118,307-354,357-411` |
| `--from-scratch` 分数下限 | `train_file.py:67-84,112-131,138-162` |
| 独立审计 CLI | `rigorous_backtest_audit.py:4,117-198,394-395` |
| 过拟合自述 / 绩效数字 | `model_core/config.py:146-154`、`strategies/STATUS_20250705.md:13,23-26,34-37,39-44` |
| AI 只做解说 / 淡化过拟合 | `web/ai_analyze.py:18-66,31,57,148-194`、`:303-335` |
| 5 层 LLM fallback | `web/ai_fallback.py:192-318` |
| `drop_forming` 契约 / 时间轴对齐 | `web/data_sources/base.py:55-60`、`data_pipeline/data_manager.py:201-265` |
| 实盘执行层被移除 | `strategy_manager/runner.py:43-84` |
| 下一根 open 成交 / 2 步标签 | `backtest_viz/engine.py:144-149,218-235` |
| 训练包导入导出（含安全项） | `web/training_package.py:37,50-93,104-216` |
| 测试 / 属性测试 / 恒真断言 | `tests/unit/test_walk_forward_gap.py:10,19-177`、`tests/property/test_runner_props.py:185,192-194` |

## 附录 B：关联文件

- 源码：`D:\code\github\AlphaMaster`（tarball，`main` @ `3727daf2d2cb`）
- 本报告：`FactorMiner/docs/research/alphamaster_competitor_review.md`
- 上游总报告：`FactorMiner/docs/research/industry_factor_mining_survey.md`（18 仓库，§6 矩阵 / §8 P0 清单 / §10 反面教训）
- 对照基线报告：`FactorMiner/user_workspace/reports/oos_validation_2025H1.md`

---

*本报告由源码实读整理，所有「未找到」均为全仓检索后的如实结论。§0 / §3.3 / §4 / §5 已于 15:40 按当前源码做过基线校准：原报告 §8 的 P0-1~P0-3、P0-5 已落地，剩 P0-4（purge/embargo + 多段 roll-forward）与 P0.5 正向验证。下一步不是补闸门，是等一段没被看过的新数据。*
