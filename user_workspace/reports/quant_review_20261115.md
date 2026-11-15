# FactorMiner 量化审查报告

日期：2026-11-15 · 范围：`core/`(评估/挖掘/回测/数据)、`api/`、`scripts/`、`user_workspace/configs/`、`factor_db/` 实际落库数据 · 方式：静态审查 + 对现有产物只读复现实测，**未修改任何代码或数据**

---

## 0. 总判断

这套系统在**方法论框架**上比多数同类项目严谨：forward return 定义、Newey-West HAC、DSR、BH-FDR、holdout ledger、跨 run 试验预算、成本闸门、数据质量闸门都真实存在且大多实现正确。

但存在一个结构性矛盾：**所有统计校正都建立在「搜索过程没看过 OOS / 未来数据」这一前提上，而上游有三条路径把这个前提破了**。同时 BH-FDR 在实际落库数据上完全没有生效。结论是：

> 当前 `factor_db` 里任何「显著」结论都不能作为可交易证据。不是闸门设计不对，是闸门装在了被污染的下游。

最反直觉的一点：`backtest_engine.py:256-257` 的注释自己写着 *"Using the evaluation window here would be look-ahead leakage"*，而默认参数 `orientation_db_root=None` 走的正是这条路。

---

## 1. 三个已实证的硬事实（不是推测）

| 实测 | 数字 | 含义 |
|---|---|---|
| 全库 `p_adj == p_value` | **84 / 84** | BH-FDR 的家庭规模恒为 1，多重检验校正**从未生效** |
| 其中 `p_adj < 0.05` | **59 / 84（70%）** | 未校正的假阳性率；真实因子库不可能有 70% 显著 |
| 快照 `abs(std) > 1e6` | **31 / 84（37%）** | 最大 `5.67e19`，除零保护 `0 → 1e-9` 造成的数值爆炸 |
| 全零常数因子 | **3 / 84** | `fac_30710a28`、`fac_af41c257`、`fac_3f1d483f` 全库 `nunique=1` |

补充几个关键实证：

- 常数因子 `fac_af41c257` 的 `RankIC=0.0, IC=0.0`，但 `p_value=0.1056`。零 IC 序列本应无法给出 t 统计量（p≈1），这个假 p 值来自 `ic_series` 对不可计算截面填 `0.0` 后 HAC 的退化。
- **上一轮"端到端回测验证"用的 3 个因子里有 2 个是全零常数因子**（`fac_30710a28`、`fac_af41c257`），第三个 `fac_f832720b` 中位数 `0.00028` 但最大值 `6.43e8`。`rank(method="first")` 对并列值按行序（=资产字母序）给 1..20，所以这两条腿每天注入的是**满权重的字母序噪声**。那份 -14.97% / Sharpe -0.30 的报告验证的是链路能跑通，不是任何研究结论——这一点当时的表述已经说明，但现在有了具体证据。
- `.fdr_ledger.jsonl` 现有 **6 个 run / 394 个 p 值**，budget snapshot 有 `n_trials=348 / 310 p 值`。而 `scripts/reaudit_factor_db.py:279-280` 把 `n_trials` 取成「库内因子数」=84。三个口径互不相同。
- `user_workspace/.holdout_ledger.json` 已登记 `2025-07-01..2026-11-13` 为一次性 strict holdout；`holdout_access.jsonl` 里有明确的 `blocked` 记录，**说明 guard 在带日期时工作正常**。问题只在无日期路径（见 C3）。
- `factor_db/metadata_pre_reaudit/` 有 84 份备份，`--apply` 已执行：84/84 的 `p_value, hac_t, n_obs, sr, orientation, cost_adj_return, ls_t, p_adj, dsr` 等 18 个字段被重算值覆盖，`lifecycle_status` 从 `DISCOVERED` → `DISCOVERED_UNVERIFIED`，`reaudited_at` 是字面量 `"2026-11-14"`。

---

## 2. A 级：直接决定结论真伪

### A1【确认，且 13/13 配置默认触发】验证集参与逐代 fitness，holdout 纪律名存实亡

- `core/evaluation/evaluator.py:135-159` `evaluate()` 主动调 `get_test_data()` / `get_test_returns()`
- `core/evaluation/evaluator.py:451-477` `oos_enabled = cfg["out_of_sample"]` 默认 `None` → 有 test split 就自动开启；`rankic_oos = abs(RankIC_oos)` 进入几何混合 `blended = rankic_in^(1-λ) · rankic_oos^λ`（λ=0.5）
- `core/miner/paradigms/base.py:298` 每个 epoch 调 `evaluate_candidates`；gp/llm/rl 各 miner 的 `evaluate_candidates` 直接 `return self.evaluator.evaluate(candidates)`
- `gp_miner.py:252/256/259` 用 `fitness_score` 做名人堂与精英选择，`llm_miner.py:164` / `rl_miner.py:164` 用其做 reward
- `core/miner/director.py:423-431` 默认走 `purpose="explore"`，**不 reveal、不阻止重复读取**

实测：13 个配置**全部**声明了 `test_period`、**全部**未设置 `fitness.out_of_sample`。因此所有配置的每一代、每个候选都在读测试期标签并据此排序选择。

**后果**：`test_period` 是被反复用于搜索的验证集，不是 OOS。基于它的 `oos_consistent` / `oos_strength` / `dsr` / BH-FDR 全部退化为样本内统计。IC 会随迭代单调虚高。strict holdout ledger 管的是另一个窗口，挡不住这条路径。

**修法**：mining 循环中禁止传 `test_data/test_returns`，只保留 `evaluate_on` 供 run 末尾对**已冻结 shortlist** 一次性评估；把 OOS 权重从 fitness 中完全移除。若作者本意就是"用 validation 引导搜索"，那必须把 `test_period` 改名为 `validation_period`，并明确声明任何 OOS 结论只能来自 strict holdout。

### A2【确认】沙箱与算子运行时允许负 `shift`，LLM 范式无因果性防线

- `core/evaluation/code_sandbox.py:82` `"shift"` 在 `_SAFE_ATTRIBUTES` 白名单
- `core/evaluation/code_sandbox.py:230-242` `visit_Call` 只查参数名是否私有，**不查参数值/符号**；`visit_Constant`（`:201-209`）只限制 `abs(value) > 1e6`，`-1` 完全合法
- `code_sandbox.py:245-255` `validate_llm_factor_output` 只查 dtype / 至少一个有限值 / 无 inf，**不查 point-in-time 因果**
- `core/miner/operator_runtime.py:138-140` `_ts_ref(n)` 同样不约束 `n >= 0`

`df["close"].shift(-1)/df["close"]-1` 是合法白名单表达式。由于 `returns[t] = close[t+1]/close[t]-1`，h=1 时该因子与标签完全相等，RankIC→1，HAC t / DSR / 成本闸门全部给出"极显著"。`bfill`、全序列 `.rank(pct=True)` 同样能过。框架内置算子方向是对的（`_ts_ref` 用 `shift(+n)`），**漏的是自定义代码与 LLM 生成代码**。

**修法**：`visit_Call` 对 `shift/periods` 强制非负常量（`ast.USub` 直接拒绝），禁 `bfill/backfill`；`_ts_ref` 加 `n<0` 抛错；再加运行时因果探针——截断尾部 K 根 bar 重算，要求前 T-K 行输出逐值不变。这是几行 AST 检查就能堵住的漏洞，性价比最高。

### A3【确认，Web 默认路径】回测方向从被回测窗口自身的未来收益推断

- `core/backtest/backtest_engine.py:121-134` `infer_orientation()` 用 `sub["factor"].corr(sub["forward_return"], method="spearman")` 决定 ±1，样本 <10 或 corr=0 时**默认 -1**
- `backtest_engine.py:228-229, 258-265` `orient_storage = ... if orientation_db_root else storage`；默认情况下 `orient_df = df`，即**评估窗口自己**
- `web/src/pages/Backtest.tsx` 的请求体不发 `orientation_db_root` → **所有 Web 触发的回测都走这条路**
- `evaluator.py:566-578` 的 `orientation` 是「逐期截面 IC 均值符号」，且已写入 84/84 份 `metadata.metrics.orientation`，但回测**故意弃用**（docstring `:124-125`）

**后果**：用整个回测期的未来收益挑一个符号，再把这个符号作用于同一期 → 方向自由度带来的乐观偏差。且池化 Spearman 与逐期截面 IC 均值是两个不同统计量（Simpson 悖论下可反号），晋升结论与回测结论可能相反。

**修法**：默认强制方向来自 in-sample 库或元数据里**事先注册**的值；无可靠方向时该因子不参与而非默认 -1；Web 暴露 `orientation_db_root`；summary 标注 `orientation_window`。

### A4【确认，已发生】BH-FDR 家庭规模塌缩为 1

- `scripts/reaudit_factor_db.py:136-143` 对**每个因子单独**调 `apply_promotion_gates([gate_input], ...)`，`metrics_list` 长度恒为 1、未传 `family_p_values` → `bh_fdr([p])` 返回 `p` 本身
- `core/experiment/budget.py:94-97`：`base = max(trace_num_trials, snap["n_trials"])` 用 `max`，但 `family = (local_family if local_family else list(snap["p_values"])) + run_p_values` 用 **`or`**。本地 ledger 一旦非空（现在有 394 个 p 值），snapshot 的 310 个历史 p 值被整体丢弃。同一个函数里 DSR 和 BH 两道闸门口径自相矛盾，恰好绕过了该模块声称要修的"fresh clone 欠校正"。

**后果**：实证 84/84 `p_adj == p_value`、59 个"显著"。`fdr_max=0.05` 形同虚设。`n_trials` 用库大小 84 而非真实 348/394，`E[max SR] ∝ √(2 ln N)` 使 DSR benchmark 偏低约 16%。

**修法**：重审先收集全库 verdict 再一次性 `apply_promotion_gates(all_metrics, family_p_values=all_pvals)`；`n_trials` 从 `budget.load()` / `FdrLedger.cumulative()` 读；`family` 取本地 ledger / snapshot / 本轮三者的并集（按 run_id 去重）。

### A5【确认】常数/爆炸因子通过评估并入库，回测里变成字母序假信号

- `core/evaluation/evaluator.py:413-423` `_validate_metric_inputs` 只要求「至少一个有限值」，不检查方差、有效配对数或量级
- `core/miner/operator_runtime.py:34-39` `_safe_divide` 用 `right.replace(0, 1e-9)`，把零分母变成 `left × 1e9`；`_terminal_value` 对未知列名静默返回全 0
- `core/backtest/backtest_engine.py:273` `rank(method="first")` 对并列值按行序给秩；`:278` `groupby.sum()` 把 NaN 组静默变 0；`:280` `scale` 仍按全部 |w| 归一

**后果**：3 个全零因子入库（其中 2 个进了演示回测）；31 个因子 std>1e6，最高 5.67e19。Pearson IC、Turnover、`scale` 归一化、AlphaPool 岭回归 `XtX` 全被污染。零 IC 因子还拿到 `p=0.1056` 这种假 p 值，占用试验预算。

**修法**：`_safe_divide` 改 `mask(==0)` → NaN，出口统一 replace inf→NaN；`_validate_metric_inputs` 增加 `nunique>1` 与量级检查；rank 前过滤 `(timestamp, factor_id)` 的 `nunique<2` 因子日，并列用 `average` 并剔除；`scale` 只按有效权重归一。

---

## 3. B 级：度量口径失真（结论方向可能对，数值不可信）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| B1 | Sharpe / 波动率年化硬编码 √252、NYSE 日历；`timeframe` 只用于找文件，从不传给 config | `backtest_engine.py:431-453`（FactorMiner 侧无 `periods_per_year`），ml4t `analytics/equity.py:119-134` | 加密 7×24 日频应 365 → Sharpe 低估 17%；4h 应 2190 → 低估约 3 倍；1h 约 6 倍 |
| B2 | funding 按**每根 bar**收一个**日费率** `1e-4`；真实 funding 文件名带 timeframe，非 1d 回测找不到→全量回落常数；8h 结算重采样后的空洞被 `fillna(rate)` 当成"再收一次全额" | `backtest_engine.py:409-423` | 4h 资金费 6 倍、1h 24 倍（年化 3.65%→87.6%）；CLI 帮助写的是 Daily rate |
| B3 | `h>1` 时 `r[t]=close[t+h]/close[t]-1` 的重叠收益被当成独立观测，`n_obs=T` 喂给 DSR 的 `z∝√(n_obs-1)`；`mean_return` 是每 bar 的 h 期累计收益 | `portfolio_eval.py:177-220` | h=5 时 z 放大约 √5≈2.24 倍，DSR 虚高；`cost_adj_return` 与"可直接对比回测"的注释不符 |
| B4 | `LinearCombinedFactor` 成员权重来自岭回归（符号已含方向），展开后又对每个成员乘一次 `infer_orientation` | `expressions.py:361-369` + `backtest_engine.py:184-185, 273-274` | 负 IC 成员得到 `w<0 × (-1) = +`，方向被抵消两次，学到的组合权重语义失效 |
| B5 | `fitness` 与 `oos_strength` 都用 `abs(RankIC)` | `evaluator.py:448,458`；`significance.py:523-526` | 纯噪声 `E\|IC\|≈0.8σ>0` 且恒正，搜索梯度指向"样本内方差更大"；符号翻转因子只乘 0.3 仍存活 |
| B6 | 自定义 fitness hook 分支 `metrics.setdefault("oos_consistent", True)` 恒真 | `evaluator.py:206-216` | 启用 `fitness.hook` 后 OOS 方向翻转的因子照样通过一致性闸门 |
| B7 | `ic_series` 用 `argsort().argsort()` 无并列平均秩，零方差/少资产截面返回 `0.0` 而非 NaN；与 `evaluator.py:321` 的 pandas `corrwith(spearman)` 口径不一致 | `significance.py:37-74` | 同一因子两套 RankIC；填 0 稀释 mean_ic、压低 std_ic 使 IR 不降反升、扭曲 HAC |
| B8 | 独立 OOS 脚本硬编码 `close.shift(-1)/close-1`、逐 asset 单表 compute、全池化 spearman、方向从 OOS 自身学 | `user_workspace/experiment_tools/recompute_oos_factors.py:113-143` | 与 `entry_price=next_close` / `horizon_bars=5` 配置差一整个 bar；正确的 `pipeline._stage_oos_recompute` 与之并存，极易误用 |
| B9 | `cs_rank` / `scale` 对 Series 退化为**全时间轴**排名/全样本归一化；`resolve_operator_specs` 不校验 operator × mining_mode | `operator_runtime.py:124-128, 315-320, 541-560` | `sequential_single` 下加 `cs_rank` 即产生全样本未来函数 |

---

## 4. C 级：可复现性与运维

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| C1 | 不记录 seed / 完整配置 hash / 数据末根日期 / 代码版本；`generation_config` 只挑 4 个键；`configNN.json` 写顶层 `random_seed` 而 director 读 `experiment.seed`；13 配置仅 2 个有 seed、0 个有 manifest | `director.py:303-308, 477-482`；`api/main.py:662-666` | 同配置重跑不可复现；库里已有参数与任何现存配置都不匹配的因子，来源无法追溯；`/api/launch` 还无条件用 `miner_name` 覆盖配置里的 `paradigm` |
| C2 | 全库重审 `--apply` 用 `metrics.update()` 覆盖原始评估值、`json.dump` 非原子写、`reaudited_at` 硬编码 | `reaudit_factor_db.py:254-264` | 已发生：84/84 原始指标丢失，主库排序/闸门全部基于重审值；备份目录是唯一基线但 API 只读主库 |
| C3 | `data_quality.strict` 默认 `False`，13/13 配置未声明；critical（重复时间戳/乱序/空面板/收益全 NaN）只打日志继续跑；多段 `mine_period` 用 `pd.concat` 不去重不排序 | `panel_validator.py:23-30, 235-243`；`real_client.py:210-222` | 当前 20 个日线文件恰好干净，但这是"恰好"不是"被拦住"；重叠 period 会直接制造重复时间戳 |
| C4 | 无 start/end 的回测跳过 holdout guard 的 overlap 检查（`explore` 只 warning + `allowed=True`），引擎随后退化为数据全区间 | `holdout_guard.py:174-187`；`api/backtest_runs.py:145-153` | `BacktestRequest.start/end` 默认 None，Web 不带日期点一次回测就把已登记的 `2025-07-01..2026-11-13` strict holdout 全读进来，ledger 无 reveal 记录 |
| C5 | 配置与实际数据不匹配：清理后库里只有 20 个 1d + 1 个 BTC-5m，**无任何 1m 文件**，但 5 个配置用 1m、3 个用 5m 且含 SUI；缺文件时 `real_client` 自动下载失败返回**空 DataFrame** 而非报错 | `real_client.py:144-151, 184-186`；`user_workspace/configs/*.json` | Web 上这些配置可见可点，会在空面板上继续挖掘。注：昨天清理掉的 1m 文件本来就全部损坏（无时间列或结构损坏），清理没有让"可用配置"变不可用，只是把隐性失败变成显性 |
| C6 | 落库跨 metadata.json + parquet + source + weights 多文件，全部直接覆盖写、无 tmp+replace、无事务；`id()` 映射使 `member_factor_ids` 可能含 `None`；`fac_{uuid4()[:8]}` 仅 32 bit 不查重 | `factor_storage.py:147-150, 332-346`；`director.py:276-338` | 崩溃留半写 JSON / 孤儿文件；组合权重静默退化为等权；不存在的 factor_id 会被追加到 `latest_factor_ids` 返回给前端 |
| C7 | 请求参数校验缺失：不存在的 factor id 被静默 `continue` 跳过，只要混入一个有效 id 就产出完整 `sharpe` 报告；`summary["n_factors"]` 报请求数而非实际使用数；`weights` 不校验 NaN/负/全零；`db_root`/`config` 任意字符串 | `backtest_runs.py:95-97`；`backtest_engine.py:247-251, 593` | 拼错一个 id 也能拿到"成功"回测 |
| C8 | 协作式取消只在 epoch 边界生效，而唯一落库点在 `mine()` **返回之后**；取消 → 本轮全部结果丢失，但 UI 文案称"已完成的 epoch 结果仍保存在 factor_db"；反向若取消发生在写库阶段则完全无效 | `api/main.py:627-630, 708-710`；`director.py:105-107` | 静默丢结果 + 取消在关键阶段失效 + 错误状态文案 |
| C9 | `update_data.py` 的 `download_mode="fill_gap"` 在 `batch_downloader` 里**从未实现**（只判断 `!= overwrite`）；`trim_after` / 合并都直接 `to_feather(path)` 覆盖源文件；合并失败另存 `*-new-<ts>.feather` 孤儿，且 `parse_data_filename` 会把它解析成幽灵资产 | `update_data.py:176-201`；`batch_downloader.py:330-357`；`naming.py:47-55` | 无人值守写盘失败会破坏 1d 历史文件；孤儿文件污染 freshness 报表；`keep='last'` 用新下载 overlap bar 覆盖已收盘历史 |
| C10 | `evaluation.timeout_seconds` 文档说"per-candidate"，实现是 `timeout_per_candidate × len(candidates)` 的**整轮**预算；`ThreadPoolExecutor.shutdown(wait=False)` 无法杀掉卡死线程，后续 epoch 全部静默 timeout 并可能把 fitness=nan 的占位因子写库 | `evaluator.py:95-104, 251-288` | 闸门"看起来生效"实则静默降级 |

---

## 5. 经核查**不构成**问题（避免误报）

- **信号到成交没有 t 日收盘成交**：`ExecutionMode.NEXT_BAR` + `next_bar_execution_price=CLOSE`，实测 trades.csv 首笔 `2024-01-02` 对应 `2024-01-01` 信号，滞后 1 根 bar。
- **手续费/滑点双边都计**：开平仓各收一次 commission，每次 fill 加/减滑点，短腿同样计费；`total_costs=46313` 是**美元**（占 1,000,000 的 4.63%），量纲合理。
- **forward return 定义正确**：`targets.py:119-133` 用 `close.shift(-h)/close-1`，因子 f[t] 与 r[t] 同索引对齐，是合法的 point-in-time；`_validate_metric_inputs` 强制 index/columns 相同。
- **框架内置时序算子方向正确**：`operator_runtime` 全部右闭合 rolling、`_ts_ref(n)=shift(+n)`，无未来函数。问题只在自定义/LLM 代码入口（A2）与 Series 退化（B9）。
- **面板对齐无静默填充**：`load_and_pivot` 用 `reindex(all_dates, all_assets)`，缺失为 NaN；crypto streams 用 past-only ffill。
- **HAC t / BH-FDR / Holm 公式本身是标准实现**：`significance.py:88-111` 的 Newey-West Bartlett 核正确。问题在**喂给它的家庭规模**（A4、B7）。
- **holdout guard 在带日期时工作正常**：`holdout_access.jsonl` 有真实 `blocked` 记录。问题只在 unbounded 路径（C4）。
- **equity 起点与最大回撤计算正确**：全曲线 drawdown 与 `max_drawdown_pct=35.195` 一致，cagr 用墙钟年数不随 timeframe 失真。

---

## 6. 最小修复集（按 ROI 排序）

只改这 7 处，就能把「结论是否可信」从不可判定变成可判定：

1. **切断 mining 循环的 OOS 输入**（A1）——`evaluate()` 在搜索期不读 test returns，OOS 只在冻结 shortlist 上评一次。这是唯一能恢复所有下游统计意义的前提。
2. **堵负 shift + 加因果探针**（A2）——AST 检查 `shift/periods >= 0`、禁 bfill，运行时尾部截断重算比对。
3. **强制回测方向来自 in-sample**（A3）——默认不传 `orientation_db_root` 时拒绝或读元数据，不再用评估窗口未来收益。
4. **修 BH family 与 n_trials 口径**（A4）——`or` → 并集；重审一次性对全库做 FDR；`n_trials` 从 budget/ledger 读。
5. **拒绝常数/爆炸因子**（A5）——`_safe_divide` 改 NaN，`_validate_metric_inputs` 加方差与量级检查，rank 过滤无离散度截面。
6. **年化系数按 timeframe**（B1）+ **funding 按 bar 时长折算**（B2）——两处都是几行配置，直接影响 Sharpe 与净收益量级。
7. **C4 + C7**：回测强制 start/end 且校验 factor id 存在性——堵住 Web 上最容易误触的两条"看起来正常"的错误路径。

C1（run manifest / seed）应与 1 一起做：否则修完之后仍无法证明"这次结论是用哪份数据、哪份代码、哪个种子跑出来的"。

---

## 7. 审查边界

- **代码基线**：审查时工作区有 11 个未提交改动（`git diff --stat` 显示 `evaluator.py / portfolio_eval.py / significance.py / targets.py / pipeline.py / director.py / base.py / pool.py / TODO.md / test_director_gating.py / configExperiment.json`，mtime 2026-11-14 23:32–23:42，内容为 P0-H1「fitness 与晋升闸门对齐」）。本报告的行号与结论均基于**包含这些改动的工作区版本**，不是 `HEAD`。这点尤其与 A1 相关：这批改动在 fitness 里加了 `ls_cost_adj_return` / `tradeable_floor`，但**没有**切断 `test_returns` 进入 `_build_fitness`，所以 A1 依然成立，且在改这块时顺手修成本最低。
- 全部结论来自静态审查 + 对现有产物（84 份 metadata、84 份 parquet 快照、6 行 fdr ledger、holdout ledger、一次历史回测运行）的只读复现。
- 未执行挖掘、下载或新回测，因此**未验证** A1 在真实长跑中把 IC 抬高了多少、B3 在 h=5 配置下 DSR 虚高的具体幅度。
- `member_factor_ids` 含 `None`、TaskStore 跨进程并发、holdout 窗口与部分配置 test_period 重叠这三项标注为「需确认意图/是否已实际发生」，未作为已确认缺陷计入。
- 本次审查未修改任何文件。
