# 附录

## 附录 A　关键常量与默认值速查

> 全部取自 2026-11-14 工作区源码。行号为当时快照，重构后请以源码为准。

### A.1 表达式与算子

| 项 | 值 | 位置 |
| --- | --- | --- |
| 内置算子数 | 59（时序 30 / 截面 8 / 二元算术 4 / 逐点与其它 17） | `core/miner/operator_runtime.py:377-444` |
| 参数化窗口算子 | 30 个；`window` 3–60（ts_delta / ts_momentum 为 1–60） | `:460-491` |
| `PARAM_KEYS` | `window / p / n / q / lo / hi` | `:499` |
| 滚动窗口 `min_periods` | `max(2, window // 2)` | `:42-43` |
| 默认终端 | `close / open / high / low / volume` | `core/miner/paradigms/gp_miner.py:18` |
| 算子 arity | 只允许 1 或 2，注册时用 `inspect.signature().bind` 校验 | `core/miner/registry.py:20-34` |

### A.2 评估与 fitness

| 项 | 值 | 位置 |
| --- | --- | --- |
| `DEFAULT_FITNESS_CFG` | `oos_exponent 0.5` / `shrinkage_k 30` / `consistency_penalty 0.3` / `turnover_penalty 0.0` / `turnover_cap 0.5` / `coverage_floor 0.20` / `gate_cost_rate 0.0018` / `gate_ls_quantile 0.2` | `core/evaluation/evaluator.py:37-51` |
| `DEFAULT_LS_CFG` | `quantile 0.2` / `cost_rate 0.0018` / `annualisation 365` / `rebalance_bars 1` / `temporal_lookback 252` | `core/evaluation/portfolio_eval.py:46-54` |
| 评估并行度 | `max_workers 8`（限 1–64）、`timeout_seconds 120` | `evaluator.py:237-282` |
| 指标契约 | `metrics.IC / RankIC / Turnover / fitness_score` | README §指标与重启约定 |
| 年化 | `MINUTES_PER_YEAR 525600`；1d→365 / 4h→2190 / 1h→8760 / 5m→105120 | `core/evaluation/annualisation.py:41-106` |

### A.3 统计闸门

| 项 | 值 | 位置 |
| --- | --- | --- |
| `DEFAULT_GATE_CFG` | `hac_t_min 2.0` / `fdr_max 0.05` / `dsr_min 0.90` / `require_oos_consistent True` / `require_cost_adj_positive True` / `require_oos_strength True` / `oos_rankic_min 0.02` / `ls_t_min 0.0` / `empirical_p_max None` | `core/evaluation/significance.py:301-332` |
| NW 滞后阶 | `L = floor(4·(T/100)^(2/9))`，Bartlett 核 | `:79-102` |
| 目标置换 | `n_perm 100, seed 0`；Director 生成 60 个零样本 | `:147-166`、`core/miner/director.py` |
| 假设预算 | `MAX_PVALUES 10000`、单调 `max`、`.hypothesis_budget.json` | `core/experiment/budget.py:21-97` |

### A.4 数据与质量

| 项 | 值 | 位置 |
| --- | --- | --- |
| `DEFAULT_QUALITY_CFG` | `min_coverage 0.90` / `max_duplicate_ratio 0.0` / `max_abs_return 1.0` / `max_gap_ratio 0.05` / `strict False` | `core/data_feed/panel_validator.py:23-30` |
| 默认 target | `entry current_close / exit close / horizon 1 / simple` | `core/evaluation/targets.py:16-95` |
| 加密流 | 5 类（funding_rate / open_interest / basis / long_liquidations / short_liquidations） | `core/data_feed/crypto_streams.py:47-65` |
| 加密特征模板 | 29 个（funding 7 / OI 7 / basis 6 / liquidation 5 / interaction 4），窗口 (6,12,24,48) | `core/miner/crypto_features.py:152-369` |
| 流对齐 | `reindex(method="ffill")`，**绝不 backfill** | `crypto_streams.py:522-529` |

### A.5 四范式

| 范式 | 关键参数 | 位置 |
| --- | --- | --- |
| GP | population 20 / max_depth 4 / tournament_k 3 / crossover 0.8 / mutation 0.3 / parsimony 0.05 / elite 2 / survival 10 / windows [5,10,20,60] / `crypto_seed_ratio 0.3` | `core/miner/paradigms/gp_miner.py:26-261` |
| RL | lr 0.05 / traj_len 4 / entropy_coef 0.01 / batch 10 / logits clip ±20 | `core/miner/paradigms/rl_miner.py:47-196` |
| LLM | batch 5 / 沙盒 timeout 5s / cpu 3s / memory 1024MB / 常量上限 1e6 / pow 上限 6 / 反思池各 100 条 / few-shot 5+5 | `core/miner/paradigms/llm_miner.py`、`core/evaluation/code_sandbox.py:30-107` |
| NN | hidden_dim 8 / lr 0.01 / epochs 50 / l2 1e-4 / diversity 1e-3 / clip 5.0 / patience 20 / top_k 5 / max_corr 0.95 | `user_workspace/custom_miners/my_custom_nn.py` |
| NN temporal | `ic_loss_weight 0.7` / `prediction_horizon 5` / 20 个过去式特征 | `user_workspace/custom_miners/my_custom_nn_temporal.py` |

### A.6 去重、池与回测

| 项 | 值 | 位置 |
| --- | --- | --- |
| 硬去重 | `md5(str(get_source()))`，全库历史 hash 作初始 archive | `core/miner/paradigms/base.py:120-159` |
| 软去重 | `dedup_value_threshold 0.99` / archive 40 / 共同有限值 <50 不判 | `base.py:41-55,266-277` |
| AlphaPool | capacity 10 / mutual_ic_max 0.99 / weight_fit_shift 21 / reg_lambda 5.0 / min_obs 200 / L1 归一 | `core/miner/pool.py:77-98` |
| 线性组合 | `reg_lambda 1.0` / `min_obs 60` / 异常退化等权 | `core/miner/expressions.py:309-370` |
| 默认成本 | commission 0.0004 / slippage 0.0005 / funding 0.0001；`gate_cost_rate = 2×(0.0004+0.0005) = 0.0018` | `core/experiment/costs.py:17-45`、`backtest_engine.py:417-423` |
| 回测 | initial_cash 1e6 / n_long 5 / n_short 5 / rebalance 5 / min_assets 10 / NEXT_BAR+CLOSE | `backtest_engine.py:65-79,417-439` |

### A.7 实验流水线

| 项 | 值 | 位置 |
| --- | --- | --- |
| `STAGES` | 8 个：data_checked / mining / gated / validated / combined / oos_prepared / backtested / completed | `core/experiment/pipeline.py:35-44` |
| `DEFAULT_WF_CFG` | n_folds 5 / embargo_bars 5 / min_train_frac 0.40 / min_train_bars 60 / ridge_alpha 5.0 / cost_rate 0.0018 / quantile 0.20 | `core/experiment/walk_forward.py:24-32` |
| 滚动窗口 | `window_bars 30` / `embargo_bars 5` / `MAX_WINDOWS 36` / stride 35 | `core/experiment/windows.py:52-54` |
| 校准 | `DEFAULT_SIGNALS (0, .02, .04, .06, .10, .15, .25)` / `DEFAULT_TRIALS (1,20,100,182,787)` / `n_repeats 12` / `n_perm 30` / `power_target 0.80` | `core/experiment/gate_calibration.py:285-308` |
| Manifest | `config_hash / git_sha / operator_set_hash / data_snapshot_hash / seed / run_id` | `core/experiment/manifest.py:139-198` |
| 全量测试 | 431 passed / 1 skipped / 56 subtests（432 collected，唯一 skip 为缺 httpx） | `TODO.md` 2026-11-14 |

---

## 附录 B　术语表

| 术语 | 英文 / 缩写 | 含义 |
| --- | --- | --- |
| 因子 / Alpha | factor / alpha | 把当期可观测信息映射为未来收益排序的函数；alpha 特指剔除风险暴露后的超额预测力 |
| 算子 / 终端 | operator / terminal | 表达式中的函数节点 / 叶子节点（数据列或常量） |
| 语法树 | AST | 以字典或对象表示的树形表达式；FactorMiner 用 `{"op","left","right",...}` |
| 逆波兰式 | RPN | 栈式 token 序列；AlphaGen / AlphaMaster 使用 |
| 面板 | panel | `T × N × D` 的时间 × 资产 × 字段数据结构 |
| 前瞻收益 | forward return | 从未来可成交时点入场、持有 h 根 bar 的收益标签 |
| 净化 / 禁运期 | purge / embargo | 训练段末端按标签 horizon 回退 / 验证段之后的隔离期 |
| 信息系数 | IC | 每期截面上因子值与前瞻收益的 Pearson 相关 |
| 秩信息系数 | RankIC | 同上但用 Spearman 秩相关，对极值与单调变换稳健 |
| 信息比率 | ICIR | IC 序列均值 / 标准差；年化时需写明 `A` |
| 覆盖率 | Coverage | 有效观测占理论观测的比例 |
| 换手率 | Turnover | 需区分原始因子变化、排序变化、组合权重换手三种语义 |
| 适应度 | fitness | 驱动搜索的标量评分，不是显著性判定 |
| 晋升闸门 | promotion gate | 决定因子能否从 `DISCOVERED_UNVERIFIED` 升到 `INSPECTED` 的判定集合 |
| 试验次数 | num_trials | 累计评估过的候选总数，多重检验分母 |
| 族错误率 / 错误发现率 | FWER / FDR | 至少一次假阳性的概率 / 被拒中假阳性的期望比例 |
| HAC | Newey–West HAC | 异方差自相关稳健的标准误与 t 统计量 |
| DSR | Deflated Sharpe Ratio | 按试验次数、样本长度、偏度峰度折减后的 Sharpe 显著性 |
| 经验 p 值 | empirical p | 相对随机零分布（置换 / 随机公式 max|t|）的经验分位 |
| 留出账本 | holdout ledger | 一次性揭示记录，重叠即抛异常 |
| 预注册 | pre-registration | 在看结果前冻结假设、窗口、阈值与成本 |
| 阳性对照 / MDE | positive control / minimum detectable effect | 注入已知真实信号验证闸门能放行 / 80% 检出率所需的最小真实 IC |
| 互信息系数 | mutual-IC | 两因子 IC 时间序列的相关，用于因子池去冗余 |
| 通道物化 | channel materialization | 把 NN 隐藏通道冻结成可独立评分与保存的因子 |
| 数据血缘 | data lineage | 因子的数据来源、样本区间、target 公式与运行身份字段 |
| 四维评分 | A / D / R / O | 能力覆盖 / 纪律可信 / 结果产出 / 运营可复现 |

---

## 附录 C　公式速查

**因子与 IC**

```
f_t = F(X_{≤t}; θ) ∈ R^N
IC_t      = corr_pearson(f_t, r_{t+1})
RankIC_t  = corr_spearman(f_t, r_{t+1})
IC        = (1/T)·Σ_t IC_t
ICIR      = mean(IC_t) / std(IC_t)
```

**标签**

```
current_close 入场:  r_t = close[t+h] / close[t] − 1
next_open    入场:  r_t = close[t+h] / open[t+1] − 1
log 口径:           r_t = log( close[t+h] / entry[t] )
```

**fitness**

```
blended = rankic_in^(1−λ) · rankic_oos^λ                λ = 0.5
shrink  = √( max(n_obs − k, 0) / n_obs )                  k = 30
penalty = (coverage / 0.20)²   若 coverage < 0.20，否则 1
fitness = 100 · blended · cons_mult · shrink · tp · penalty
```

**NW-HAC t**

```
L = floor( 4·(T/100)^(2/9) )
s = γ_0 + 2·Σ_{l=1..L} (1 − l/(L+1))·γ_l
t = mean(ic_t) / √(s/T)
```

**多重检验**

```
FWER         = 1 − (1 − α)^K
BH  adj_(i)  = min_{j≥i} ( n·p_(j) / j )
Holm adj_(i) = max_{j≤i} ( (n−j+1)·p_(j) )
E[max SR]    ≈ σ_SR · √( 2·ln K )
```

**DSR**

```
σ_SR         = 1 / √(n_obs − 1)
sr_benchmark = sr_0 + σ_SR·√(2·ln n_trials)
denom        = √( max(1e−9, 1 − skew·sr + (kurt−1)/4·sr²) )
z            = (sr − sr_benchmark)·√(n_obs−1) / denom
DSR          = 0.5·(1 + erf(z/√2))
sr           = cost_adj_return / std_return        （每期净策略 Sharpe）
```

**分位多空与成本**

```
w_long = +0.5/n_long ,  w_short = −0.5/n_short ,  gross = 1.0
ls_t            = Σ_i w_{t,i}·r_{t+1,i}
turnover_t      = 0.5·Σ_i |w_{t,i} − w_{t−1,i}|
cost_t          = turnover_t · cost_rate
cost_adj_return = mean(ls_t) − mean(cost_t)
gate_cost_rate  = 2·(commission_rate + slippage_rate) = 0.0018
sharpe_annual   = (mean/std)·√A      A: 1d→365, 4h→2190, 1h→8760, 5m→105120
```

**时间隔离**

```
train_end       = val_start − horizon_bars − 1         # purge
embargoed_start = val_start + embargo_bars
gap_bars        = embargoed_start − train_end − 1      # 正式 run = 6
stride          = window_bars + embargo_bars = 35      # 滚动预注册
```

---

## 附录 D　参考文献与源码索引

### D.1 学术与行业文献

| 文献 | 与本书的关系 |
| --- | --- |
| Kakushadze, Z. (2016). *101 Formulaic Alphas*. SSRN | 第 6 章，手工公式库与算子语义 |
| Bailey, D. H. & López de Prado, M. (2014). *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality*. Journal of Portfolio Management, 40(5), 94–107 | 第 11、12 章，DSR 与 `expected_max_sr` |
| Bailey, D. H., Borwein, J. M., López de Prado, M. & Zhu, Q. J. (2017). *The Probability of Backtest Overfitting*. Journal of Computational Finance, 20(4), 39–69 | 第 12 章，PBO / CSCV |
| López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley, Ch. 7 / Ch. 11 | 第 14 章，purge / embargo / CPCV |
| Lo, A. W. (2002). *The Statistics of Sharpe Ratios*. Financial Analysts Journal, 58(4), 36–52 | 第 13 章，非正态 SR 方差修正 |
| Newey, W. K. & West, K. D. (1987 / 1994). HAC 协方差估计 | 第 13 章，Bartlett 核与滞后阶启发式 |
| Benjamini, Y. & Hochberg, Y. (1995). *Controlling the False Discovery Rate*. JRSS-B | 第 13 章，BH-FDR |
| Holm, S. (1979). *A Simple Sequentially Rejective Multiple Test Procedure*. Scand. J. Stat. | 第 13 章，Holm |
| White, H. (2000). *A Reality Check for Data Snooping*. Econometrica | 第 12 章，Romano–Wolf / bootstrap 族检验的源头 |
| Harvey, C. R. & Liu, Y. (2015). *Backtesting*. Journal of Portfolio Management, 42(1), 13–28 | 第 12 章，多重检验与 HLZ `|t| > 3.0` |
| Sullivan, Timmermann & White (1999). *Data-Snooping, Technical Trading Rule Performance, and the Bootstrap*. Journal of Finance | 第 12 章，数据窥探的经典实证 |
| Koza, J. R. (1992). *Genetic Programming* | 第 7 章，树表示与遗传算子 |
| Williams, R. J. (1992). *Simple Statistical Gradient-Following Algorithms for Connectionist Reinforcement Learning*（REINFORCE） | 第 8 章，策略梯度 |
| Yu, S. et al. (2023). *Generating Synergistic Formulaic Alpha Collections via Reinforcement Learning*（AlphaGen, KDD'23） | 第 7、10、17 章，动作掩码与 AlphaPool |
| Shi, H. et al. (2025). *AlphaForge*（AAAI'25） | 第 9、10、17 章，GAN 生成-预测与动态组合 |

### D.2 项目内一手文档

| 文档 | 用途 |
| --- | --- |
| `README.md` | V4 架构、CLI、配置、扩展契约、Phase II 快照与指标约定 |
| `TODO.md` | 完整实施时间线与真实数字（P0/P0.5/P1/P7、431 测试、滚动窗口 ETA） |
| `docs/README.md` §5 / §6 | 八阶段流水线、闸门校准、universe holdout、滚动预注册 |
| `docs/architecture/v4_architecture_design.md` | 统一中间体、IoC、四范式闭环、存储与沙盒设计 |
| `docs/research/industry_factor_mining_survey.md` | 18+ 仓库源码级调研，§6 三张横向矩阵、§8 P0/P1/P2、§10 反面教训 |
| `docs/research/alphamaster_competitor_review.md` | 第 19 个竞品，L5 维度、动作掩码、病因链 |
| `docs/research/framework_maturity_crypto_fit.md` | 21 框架 M/C 双维度评分、象限、7×24 年化口径 |
| `docs/research/platform_maturity_gap.md` | A/D/R/O 四维评分、S1–S5 路线图、三处口径修正 |
| `docs/research/optimization_plan.md` | Phase 0–4 落地记录与真实 bug 修复 |
| `docs/fitness_hook_guideline.md` | RankIC 转向、覆盖率惩罚、Fitness Hook 范例 |
| `skills/factorminer-research-architect/references/research-protocol.md` | 九步研究设计协议、结论等级（本书第 20 章骨架） |
| `user_workspace/reports/*.md` | `p0_holdout/README.md`、`p0_5_return_gate.md`、`p1_gate_upgrades.md`、`factor_db_reaudit.md`、`gate_calibration.md`、`crypto_features_validation.md` 等真实实验报告 |
| `user_workspace/runs/run_20261114T104814Z_b9eb49/` | 正式八阶段 run 的 manifest / metrics / walk_forward / report / status |

### D.3 核心源码索引（按主题）

| 主题 | 路径 |
| --- | --- |
| 表达式与算子 | `core/miner/expressions.py`、`operator_runtime.py`、`registry.py`、`entities.py` |
| 四范式与调度 | `core/miner/paradigms/{base,gp_miner,rl_miner,llm_miner}.py`、`core/miner/director.py` |
| NN 模型与 Miner | `core/miner/nn.py`、`user_workspace/custom_miners/my_custom_nn.py`、`my_custom_nn_temporal.py` |
| 去重与因子池 | `core/miner/paradigms/base.py`（`DiversityFilter`）、`core/miner/pool.py` |
| 评估与显著性 | `core/evaluation/{evaluator,significance,portfolio_eval,targets,annualisation,experiment_trace,holdout_guard}.py` |
| 沙盒 | `core/evaluation/code_sandbox.py` |
| 数据 | `core/data_feed/{real_client,panel_validator,crypto_streams,freshness,data_downloader,batch_downloader}.py` |
| 存储与审查 | `core/storage/factor_storage.py`、`core/inspector/{engine,metrics,reporter,resolver}.py` |
| 回测 | `core/backtest/backtest_engine.py`、`core/commands/backtest.py` |
| 实验流水线 | `core/experiment/{manifest,registry,budget,costs,walk_forward,pipeline,windows,universe,gate_calibration}.py` |
| 服务层 | `api/main.py`、`api/ws_manager.py`、`web/src/` |
| 测试 | `tests/`（36 个文件，431 passed / 1 skipped / 56 subtests） |

### D.4 竞品仓库对照

`qlib`、`KunQuant-rs`、`alphalens`、`trade-learn`、`gplearn`、`gpquant`、`AlphaGen`、`AlphaForge`、`WorldQuant_alpha101_code`、`AlphaForge-Crypto`、`crypto-factor-lab`、`RD-Agent`、`EvoAlpha`、`CosmosAlpha`、`LLM-Alpha-Mining-Framework`、`WorldQuant-LLM`、`alphaminingv2`、`alpha-factor-mining-crypto`、`Alpha-Research-Discovery`、`AlphaMaster`。

> 所有「未找到」结论均指调研当时全仓 `--include=*.py` 精确检索零命中；21 个仓库中 13 个为 tarball 下载、无 `.git`，无法核对提交历史与 CI。M/C 评分是人为分档，不同权重会改变名次但不改变象限结构。本书未运行任何外部框架的挖掘或回测，可运行性判断基于静态核查。

---

*附录完 · 全书完*
