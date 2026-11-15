# FactorMiner 全模块代码审查 · 全量问题登记册

日期：2026-11-15 · 方式：按模块逐个通读（10 个模块组并行 + 主审查人交叉复核），全程**只读**，未修改任何代码或数据
覆盖：`core/` 全部 72 个 .py、`api/` 4 个、`scripts/` 5 个、`web/src/` 全部 .ts/.tsx、13 个配置、CI/Docker/compose/nginx/setup.py、`tests/` 41 个文件（抽查 9 个 + 全量扫描）
基线：工作区版本（含 2026-11-14 23:32–23:42 未提交的 P0-H1 改动），非 `HEAD`

---

## 0. 总判断（在上一轮结论之上更新）

上一轮结论：*「闸门在下游、泄漏在上游，当前 factor_db 的显著结论不可作为可交易证据」*，详见 `quant_review_20261115.md`（A/B/C 三级约 30 条）。

**本轮发现一个更基础的问题**：不仅研究结论不可信，连「系统是否真的跑过、结果是否真的算出来」都无法从退出码、界面和报告判断。至少 11 处是**假成功**：

| 假成功 | 位置 | 实际行为 |
|---|---|---|
| WebSocket 实时通道 | `api/main.py:37-49,65` | `require_auth(request: Request)` 被全局依赖复制到 WS 路由，实测 **配不配 token 都抛 `TypeError: missing required positional argument: 'request'`**，握手 100% 失败 |
| 断层补全 | `core/data_feed/gap_filler.py:273-275` | `# 暂时模拟执行成功` + `success_count += 1` + `sleep(0.1)`，从不下载也不写回，却返回成功 |
| CLI 退出码 | `core/cli.py:88-109` | `main()` 丢弃所有命令返回值且不 `sys.exit`，`factorminer` 主入口恒返回 0 |
| 下载命令 | `core/commands/download.py:28-40` | 不接收 `{'success': False}`，一律打印 "Batch download completed." 并退出 0 |
| LLM 挖掘 | `core/miner/paradigms/llm_miner.py:101-129` | 初始化/整批失败时静默降级为 2 个写死的 mock 表达式，照常入库 |
| OHLC 健康检查 | `core/data_feed/health_checker.py:310-322` | `invalid_count = ...if 'invalid_count' in locals() else 0` 恒为 0，脏 K 线全放行 |
| 数据覆盖率 | `data_downloader.py:1105-1127` | 以 `index.max()-index.min()` 为分母，首尾缺失永远算 100 分 |
| 下载分页 | `data_downloader.py:154-183` | 按 30 天自然窗 + `limit=1000` 跳窗推进，1m 每窗只覆盖 16.7 小时，之后全丢；异常批 `break` 后仍照常保存 |
| 已下载文件列表 | `data_downloader.py:1204-1213` | `df.index.min().strftime()` 对 RangeIndex 必抛 AttributeError，`get_data_info` 恒失败，UI 永远看不到已下载文件 |
| 研究/验收报告 | `experiment_tools/*.py`、`scripts/*.py` | 结论段落写死 "-7.37%"、"84 个因子"、"无一通过" 等数字，不随计算结果变化 |
| 前端年化收益 | `web/src/pages/Backtest.tsx:307-308` | `cagr=-0.1788` 走 `pct()` 只加 `%` → 界面显示 "-0.18%"，真实 **-17.88%**，缩小 100 倍 |

> 因此当前优先级应调整为：**先让系统"真的跑、真的报错、真的显示对"，再谈统计闸门的修复**。否则修完 A 级泄漏，验证手段本身还是坏的。

**本轮新增约 78 条**（不含上一轮 A/B/C 约 30 条），按模块分布：
miner 核心 8、paradigms 6、evaluation 8、experiment 8、data_feed 8、commands/CLI 8、api 8、support 8、scripts/配置/部署/测试 8、web 8，另附各模块补充触发点约 20 处。

---

## 1. 主审查人亲自复核的证据（✅ = 实测坐实，非 agent 推断）

| # | 复核项 | 实测结果 |
|---|---|---|
| ✅1 | WS 握手 | 真实 ASGI `scope={"type":"websocket"}` 调用 `app()`：`token=None` 与 `token='s3cr3t'` **均抛 TypeError**；`APIWebSocketRoute.dependencies` 确含 `require_auth` |
| ✅2 | 前端 cagr 单位 | `summary.csv` 中 `total_return_pct=-17.87`、`cagr=-0.1788`、`volatility=0.2075`；`pct()` 只做 `.toFixed(2)+'%'` → 界面 -0.18% / 0.21% |
| ✅3 | 配置 entry_price | 13 个配置：**10 个走默认 `current_close` + 2 个显式 `current_close` = 12 个**，仅 `configExperiment.json` 用 `next_close`；`targets.py:125` 注释明写 `next_close` 才与 NEXT_BAR/CLOSE 对齐 |
| ✅4 | `annualisation` '1M' | `infer_annualisation('1M') = 525600.0`（分钟口径），月线应约 12 → Sharpe 放大约 **209 倍** |
| ✅5 | `_ts_rank` 方向 | 递增序列 `[1..5]`，最后（最大）值返回 **0.0**；`rank(pct=True)` 应为 1.0，方向完全颠倒 |
| ✅6 | `startup_validation` NN | `--miner NN` 报 `Unknown Miner 'NN'. Available Miners: GP, LLM, RL`；`DL→NN` 迁移后又被自己判未知；`director.py:88` 实际支持 NN |
| ✅7 | `cli.py` 默认因子 | `--factors` 硬编码 6 个 `fac_c25e28c3...`，而 `commands/backtest.py:34` 已改 `DEFAULT_FACTORS=[]` 并注明"这 6 个已被 re-audit 降级"；`main()` 无 `sys.exit` |
| ✅8 | `gap_filler` | `execute_fill_tasks` 注释"暂时模拟执行成功"，只有 `success_count += 1` 和 `sleep(0.1)` |
| ✅9 | `health_checker` | `invalid_count = invalid_ohlc.sum() if 'invalid_count' in locals() else 0` —— 首次赋值时必取 0 |
| ✅10 | 下载分页 | `batch_end = min(current_dt + timedelta(days=30), end_dt)`，`since` 用 `current_dt.timestamp()*1000`、`limit=1000`，推进用 `current_dt = batch_end` 而非返回末根时间戳 |
| ✅11 | `setup.py` | `install_requires=["ml4t-backtest>=0.1.0", "polars>=1.36.1"]`，而 `requirements.txt` 明说该包不在 PyPI → `pip install -e .` 必失败，且缺 pandas/numpy/fastapi/ccxt |
| ✅12 | `configRL.json` | 顶层键是 `rl_config`，`rl_miner.py:87-89` 只读 `config["rl"]` → `learning_rate 0.5` 静默变默认 `0.05`、`max_depth` 丢失 |
| ✅13 | `nginx.conf` | 只有 `location /` 的 SPA fallback，无 `/api/` proxy_pass、无 `/ws/` Upgrade 反代 |
| ✅14 | 脚本写死结论 | `summarize_oos_validation.py:146/152`、`reaudit_factor_db.py:218/235`、`validate_gates.py:250` 均含硬编码数字与判词 |
| ✅15 | `useWebSocket` cleanup | `useEffect` 依赖只有 `[connect]`，cleanup 里 `if (socket) socket.close()` 捕获的 `socket` 恒为首次渲染的 `null` |
| ✅16 | 全库统计（上一轮） | `p_adj == p_value` 84/84、59/84 "显著"、31/84 std>1e6、3 个全零因子；ledger 6 runs/394 p 值，snapshot n_trials=348 |

---

## 2. core/miner/（8 个文件，零问题：`__init__.py`）

- **[M-01] CRITICAL · `operator_runtime.py:64-73`** — `_ts_rank` 返回 `(x > last).sum()/(len(x)-1)`：当前值最大→0、最小→1，与 `rank(pct=True)` 及同文件 `_cs_rank`（最大→1）**方向相反**。✅ 后果：`crypto_features.py:161/219/253` 的 `funding_rank`/`oi_rank`/`basis_rank` 把"高拥挤"映射成低分位，经济先验反号；GP 里所有 ts_rank 因子符号翻转；并列值只算严格大于，重复值多时系统性低估。**修法**：改 `(x <= last).sum()/len(x)` 并复核全部 `*_rank` 模板先验。
- **[M-02] CRITICAL · `pool.py:220-244`、`expressions.py:355-367`** — 两处岭回归对**未标准化/未中心化/无截距**的 X 直接 `X.T@X + λI` 求解。子因子量纲差 1e5 倍（ts_rank∈[0,1]、cumsum~1e5、close~1e4），λ 对小尺度列近乎无限正则、对大尺度列几乎无效，且非零均值子因子把均值吸进权重。**修法**：mask 后逐列 z-score（OOS 复用同一 scaler）、加不惩罚的截距列、λ 按列尺度归一化。
- **[M-03] HIGH · `pool.py:136-217`** — `_safe_pearson` 在 <10 点或 denom=0 时返回 `None`，`max(best, abs(c))` 跳过 → 常数因子 `mic=0` 一律放行；`_admit_with_replacement` 满池路径不查冗余；`evaluate_admission` 也不校验单因子 RankIC。另 mutual-IC 用「各自 IC 时间序列的 Pearson」而 docstring 声称 AlphaGen 式因子值截面相关【需确认意图】。**修法**：None 即拒绝、满池同样查冗余、明确定义。
- **[M-04] HIGH · `operator_runtime.py:106-117`** — `_ts_decay_linear`：分子 `np.nansum(x*seg)` 忽略 NaN 对应权重，`denom=np.nansum(seg)` 仍用满段 → 有缺口窗口输出被压低；非满窗口取 `weights[-len(x):]`（尾部大权重段）而非 `weights[:len(x)]`，样本头尾衰减形状不可比。
- **[M-05] HIGH · `nn.py:267-309`** — early-stopping 记录**更新前** W 的 `loss`，却保存**更新后** `W'` 的 `best_parameters`，训练结束恢复到"下一步"参数；全程无 validation split，`patience` 只监控训练拟合度。
- **[M-06] MEDIUM · `operator_runtime.py:578-586,181-202,611`** — 未知 terminal 静默返回全 0 面板（`_zero_like_data` 对 dict 只取第一个 stream 的 index/columns）；`np.argmax/argmin` 遇 NaN 返回 NaN 位置；AST 节点多余 key 无条件转发 `**params` 触发 TypeError。**这是 A5 常数因子与 C5 无数据配置的具体触发点**。
- **[M-07] MEDIUM · `director.py:320-376,297-310,485-509`** — 落库分流 5 个 `isinstance` 分支**无 else**（自定义 expression 静默不落库但 `latest_factor_ids` 已 append）；`expressions.py:254-262` 的 `from_dict` 不读 `member_factor_ids` 且只还原含 `"op"` 的 dict → 组合因子回读无法重算；`FactorMetadata` 的 `logic_reference`/`parent_ids`/`data_lineage` 全不传；`logic_hash` 在精英保留分支为空串使 `get_all_logic_hashes()` 漏判；snapshot 失败只 warning。
- **[M-08] MEDIUM · `registry.py:20-34`、`operator_runtime.py:528-533`、`crypto_features.py:65-77,289-321,474-477`** — 自定义算子注册只校验 arity，注册 `lambda s: s.shift(-1)` 即引入未来函数（**A2 的另一触发点**：沙箱只管 LLM 代码，AST 算子路径不受约束）；与内置同名的自定义算子注册成功却永不生效；`long/short_liquidations` 标注 vendor/event 供给却无 lag【需确认意图】；给了 `limit` 未给 `rng` 时不 shuffle，OI/basis/liquidation 模板系统性不进搜索空间。

**补充**：`entities.py:59-63` `for_candidate` 用 `is` identity；`:65-71` `EvaluationFeedback.metrics` 过滤失败项后与 `candidates` 不再等长，下游 `zip()` 错位；`:81-88` `raw_outputs` 只保留最后一项；`pool.py:225/273` 权重拟合用 `weight_fit_shift` 的 target 而 admission 用未 shift 的 returns，horizon 口径不一致。

---

## 3. core/miner/paradigms/（7 个文件，零问题：`__init__.py`、`dl_miner.py` 全 raise 的弃用占位）

- **[G-01] HIGH · `rl_miner.py:159-178`** — REINFORCE 的 advantage 用「含自身、逐步累积的运行均值」而非 batch mean，第一个样本贡献恒为 0；`self.baseline` 赋值后从未读取。梯度系统性有偏。
- **[G-02] HIGH · `llm_miner.py:101-129`** — API 初始化失败或整批失败静默降级为 `df['close'].pct_change(1)` / `df['volume']*df['close']` 两个 mock（cross_asset 下 `df['close']` 还会直接失败），仍正常返回入库。
- **[G-03] MEDIUM · `gp_miner.py:230-238`** — 交叉子代 `parent_ids` 写成 `List[List[str]]` 且装的是祖辈列表而非父代 ID，谱系类型与语义双重损坏。
- **[G-04] MEDIUM · `gp_miner.py:187-195`** — `_mutate` 的 else 分支对非 dict 结果再抽一次并 `tgt.update(...)`，终端常量/字符串命中时抛 ValueError；正常路径也把"替换成终端"误做成再抽子树。
- **[G-05] MEDIUM · `rl_miner.py:106/113/126`、`gp_miner.py:118-126`** — 全目录无种子注入，且动作走 `np.random`、终端/窗口走 `random` 两个独立 RNG，外部单一 seed 无法复现轨迹（C1 的具体触发点）。
- **[G-06] MEDIUM · `base.py:306,199,457/506`** — 名人堂排序缺 `fitness_score` 时默认 `0.0`，`_finalize` 同情形默认 `-1e9` 并过滤；自定义 hook 返回 dict 时无 fitness 的候选排到真实低分因子之上，且又被 valid 剔除，名人堂与可组合集合矛盾。

**补充**：`base.py:89-101,120-127` 面值去重提前 `compute`，异常被 `logger.debug` 吞掉 → `signature=None` → 软去重静默放行；精英本轮不参评却仍 compute 一次，加速耗尽 archive 配额；`llm_miner.py:114-136` 整批 prompt 完全相同、清洗只去 ` ```python `、`llm_api_manager.py:88-134` 重试不覆盖"200 但内容非法"。

---

## 4. core/evaluation/（9 个文件，零问题：`targets.py`、`__init__.py`；forward return 定义与 HAC/BH 公式经复核正确）

- **[E-01] CRITICAL【方法论风险】 · `significance.py:305-307,248,276-277`** — DSR 把 Bailey-LdP 要求的**试验间 SR 横截面标准差 σ(SR)** 错换成单个 SR 的采样标准误 `1/sqrt(n_obs-1)`，门槛随样本量增大反而下降、对累积 n_trials 几乎不敏感，`dsr_min=0.90` 形同虚设。补充【确认 bug】：`denom = sqrt(max(1e-9, ...))` 的 clamp 在根号内，二次式为负/极小时 `denom≈3.16e-5` → `z→±∞` → DSR≈1.0 虚假通过。
- **[E-02] CRITICAL · `holdout_guard.py:101-108,191-194,251-270`** — 单侧区间被补成 `0000-01-01..9999-12-31`，`purpose="final"` 会用这条无限窗口 `ledger.reveal` → 一次误用把**整个时间轴**登记为 burned，此后所有窗口被 `HoldoutReuseError` 永久阻断。附带 `_normalise` 丢日内时间与时区、垃圾值截断成 `"garbage_st"` 参与字典序比较、`period_key` 不含 timeframe/horizon（C4 同区域但根因不同）。
- **[E-03] HIGH【方法论风险】 · `significance.py:359-366,217-232`** — `pvalue_from_t` 把 NW HAC t 当 N(0,1) 求双侧 p，HAC t 在小样本/多滞后下尾部更厚 → raw p 系统性偏小 → BH/Holm 后仍偏松。补充：`empirical_p = mean(null >= value)` 无 +1 连续性校正，`n_perm=100` 时可返回 0.0。
- **[E-04] HIGH · `experiment_trace.py:88,123-173,238-257,301-317`** — ledger 用**实例级** `RLock` + 内存快照 + 全量覆盖写，跨线程/进程 TOCTOU：两个调用各自 `_load` 旧快照、`overlapping` 互相看不见 → 重叠窗口双双通过并 reveal，后写者覆盖先写者，one-time holdout 可被重复窥视。补充：重复 signature **替换而非累加**低估 N；`TrialRecord(**json.loads(line))` 遇未知键抛 TypeError 被静默吞掉。
- **[E-05] HIGH · `portfolio_eval.py:198-222,328-345`** — `sharpe`/`hit_rate`/`std_return`/`ls_t` 全用**未扣成本**的 gross `ls`，而 `cost_adj_return`/`net_skew`/`net_kurtosis` 用净序列 → `ls_significant` 声称检验 net return，实际 t 是 gross，换手越大显著性越被高估（B3 同根因新触发点）。
- **[E-06] HIGH · `code_sandbox.py:106-107,201-223,319-332`** — 常量限制可绕过：`999999*999999*999999`（`Mult` 允许、每个常量 ≤1e6、`visit_BinOp` 只挡 `Pow`）→ `list(range(10**12))`；本环境正是 Windows，`import resource` 的 ImportError 被吞，RLIMIT_AS/RLIMIT_CPU 全失效，只剩 5s 墙钟（A2 之外的资源维度逃逸）。
- **[E-07] MEDIUM · `annualisation.py:45-75`** — `unit.lower()` 使 `"1M"` 被当成 `"m"`=1 分钟 → ✅ 实测 `infer_annualisation('1M')=525600`（应约 12），Sharpe 放大约 209 倍。
- **[E-08] MEDIUM · `evaluator.py:444-446,777-783,503-506,330-339`** — shrinkage 的 `n_obs` 用 `_valid_pair_count`（cross_asset 返回 **T×N 单元格数**），`shrink≈1` 永不生效，与 HAC/DSR 用的 `ic_ts.size=T` 口径不一致；`ls_cost_adj_return` 以 `rebalance_bars=1` 计算而 gate 以 horizon 持仓计算，h>1 时 P0-H1 的「search target 对齐 gate」**反向失效**；`ls_orient` 取 RankIC(pandas corrwith) 而 `orientation` 取 `ic_series` 均值，符号可能相反。

---

## 5. core/experiment/（10 个文件，零问题：`__init__.py`）

- **[X-01] CRITICAL · `pipeline.py:201-205`** — OOS warmup 行切片 `values.loc[values.index.intersection(test_index)]` 失败被 `except: pass` 吞掉，`_oos_warm_panel` 拼进来的 `mine.tail(n)` 全部留在 OOS 快照；函数注释承诺 "no in-sample bar leaks" 却无写盘前断言。
- **[X-02] CRITICAL · `pipeline.py:312-322` + `manifest.py:219-220`** — `resume` 分支**完全不调用 `verify_manifest`**，而该函数在 `observed.get(k)` 为假（缺 `git_sha`/`operator_set_hash`/`data_snapshot_hash`）时跳过检查、返回 `reproducible=True` → 代码/数据/算子已变的 run 被判可续跑并复用陈旧因子值。补充：`seed_applied` 只反映"声明了 seed"；`_data_files` 用相对 `Path("data")` 依赖 cwd。
- **[X-03] CRITICAL · `pipeline.py:392-399` + `universe.py:274-288`** — guard 在 `stage()` 之外无条件执行、非幂等：resume 时首跑已写入 ledger，strict 下 `prior` 存在即抛 `UniverseReuseError`，异常不经 `stage()` 的 try/except，`status.json` 不写 failed，run 静默停在 `created`；且 guard 在 `data_checked` **之前**就消耗一次性额度，首跑失败会烧掉窗口却无任何 OOS 结果。
- **[X-04] HIGH · `costs.py:24-45`、`pipeline.py:394`、`walk_forward.py:34`、`gate_calibration.py:64`** — 成本「单源」实为四源：`resolve_cost_params` 算出 `consistent=False` 只塞进 state、从不强制；`walk_forward`/`gate_calibration` 直接用 `DEFAULT_COST_RATE` 常量**不读 config**；`defaults.py:20` 写"不要重复硬编码 0.0018"而 `costs.py:18` 本身就硬编码。结果正是模块要防的「过 gate 却在回测亏钱」。
- **[X-05] HIGH · `budget.py:50-51,62-77`** — snapshot `load` 遇损坏/非法 JSON 静默返回空表 → `sync` 从 0 重建，直接违背 "monotonic, sync never lowers"；`load→merge→os.replace` 无跨进程锁；`p_values` 裁到 10000 而 `n_trials` 用 `max` 无限增长 → BH 与 DSR 分母再次分叉。（A4 同根因新触发点；`director.py:220-223` 把 sync 异常 `logger.debug` 吞掉。）
- **[X-06] HIGH · `registry.py:63,108-125,171-172`** — `mkdir(exist_ok=True)` 使 run_id 撞车时静默覆盖旧 run 产物；`update_status` 不校验 stage 单调性、终态可被 `write_artifact` 重新打开为 `running`；`TERMINAL_STATUSES` 含 `failed` 使 `find_resumable` 跳过失败 run → **失败后根本无法 resume**；`STAGE_ORDER` 定义后从未被引用且缺 `oos_prepared`。
- **[X-07] MEDIUM · `windows.py:140`、`walk_forward.py:56-57,244-261`** — `_file_dates` 的 `.normalize()` 把 1h/15m/5m 全部压到当天午夜，模块声称 "bar space" 却每天只剩 1 根，`window_bars=30` 实际约 30 天；`build_folds` 样本不足返回 `[]` 而 `walk_forward_validate` 报 `n_folds=0 / refit_per_fold=True`，`stage()` 只看不抛异常就记 validated → 0 folds 实验 green。
- **[X-08] MEDIUM【需确认意图】 · `gate_calibration.py:169-216,466-476`** — `p_value` 与 `null_tstats` 都从**同一份 in 样本**置换生成，并非独立证据，却被当作两道独立闸门同时要求通过；随后用这个被压低的检出率"证明"闸门功效不足，构成循环论证。`DEFAULT_TRIALS=182` 硬编码，与真实 `n_trials=348` 口径错配；用户 `--trials` 不含 182 时 `mde.get("182")` 返回 None 会误报"未达目标功效"。

**补充**：`universe.py:216` `split_hash` 只按最终集合精确匹配，微调 holdout_frac 即可产生新 hash 再次 reveal 同一批标的；`_append_ledger` 读-改-写无锁、`_read_ledger` 损坏时返回 `[]` 会让全部 burned 记录消失。

---

## 6. core/data_feed/（12 个文件，零问题：`__init__.py`；`freshness.py`/`naming.py` 本轮无新增）

- **[D-01] CRITICAL · `data_downloader.py:612-616`** — 补洞下载返回空时 `continue` 不推进 `current_ts`，**无上限死循环**；`remaining_candles=0` 导致 `dynamic_limit=0` 时同样挂死，外层 per-gap try/except 捕获不到。
- **[D-02] CRITICAL · `data_downloader.py:154-183`（`batch_downloader.py:136-206` 同根因）** — ✅ 分页按 30 天自然窗 + `limit=1000` + `since=窗口起点` 推进，不按返回末根时间戳：1m 每窗只覆盖 16.7 小时、5m 3.5 天、15m 10 天，日内数据大量丢失；异常批 `break` 后仍走验证并 `success=True` 落盘，而覆盖率以首尾为分母 → **残缺文件判 100 分写入**。
- **[D-03] CRITICAL · `gap_filler.py:273-275`** — ✅ `execute_fill_tasks` 只模拟计数、从不下载也不写回，返回 `success=True`。配套 `processor.detect_gaps:126-127` 把 `large_gaps.items()` 的**索引 label** 当 `.iloc[idx]` 位置用、`generate_download_tasks:191-192` 用 `%Y-%m-%d` 丢掉小时粒度 —— 即便接上真实下载，补洞时间范围也是错的。
- **[D-04] HIGH · `health_checker.py:310-322`** — ✅ `invalid_count = ... if 'invalid_count' in locals() else 0` 恒为 0，OHLC 逻辑校验完全失效。补充：`real_client.py:275-299` 的 `_ensure_quality_checked` **先**置 `_quality_checked=True` 再吞掉非 `DataQualityError` 异常且不重置 → 首次校验异常会使质量门在整个客户端生命周期永久失效（与 C3 不同根因）。
- **[D-05] HIGH · `data_downloader.py:160-168`、`batch_downloader.py:151-159`** — `fetch_ohlcv` 返回的**最后一根未收盘 K 线**原样落盘，无任何剔除；`drop_duplicates(keep='last')` 不会纠正，全库每个文件末根都带此污染。
- **[D-06] HIGH · `data_downloader.py:137,163`、`batch_downloader.py:120,154,192,221-223`** — naive `datetime.timestamp()` 按本地时区(UTC+8)解释 `since`，比意图 UTC 早 8 小时且父类再做一次 UTC-naive 过滤二次错位；batch 把返回时间戳转 `Asia/Shanghai`，验证用 `tz_localize(None)` 保留上海墙上时间，落盘又转回 naive UTC，日线边界差 8 小时；tz-aware/naive 混合索引令 `_merge_with_existing_data` 的 concat 抛错被静默吞掉。
- **[D-07] MEDIUM · `loader.py:287-288,206/215/223-228`** — `preprocess_data` 用 `bfill` **无条件后向填充**（对含 `returns`/指标列的表是直接前视偏差）；`log(close/shift(1))`、`ma_ratio`、`price_position`、`volume_ratio` 多处除零产生 ±inf 且 ffill/bfill 不清理。
- **[D-08] MEDIUM · `crypto_streams.py:368-380`、`data_downloader.py:1204-1213`** — stream 写盘非原子覆盖且合并失败静默丢弃历史；✅ `get_data_info` 把 RangeIndex 当 DatetimeIndex 调 `.strftime()` 必抛 AttributeError → `list_downloaded_data` 永远返回空列表，UI 看不到任何已下载文件。

**【需确认意图】**：`naming.safe_symbol:16` 把 `/`、`:` 都压成 `_` 不可逆；`parse_data_filename:51-54` 的 futures 后缀校验会误拒币本位交割 `BTC/USD:BTC`；`freshness.display_symbol:40-44` 对多段 base 逆向还原有歧义。

---

## 7. core/backtest/ + core/commands/ + core/cli.py（8 个文件，零问题：`backtest/__init__.py`；`backtest_engine.py` 本轮无新增独立项）

- **[K-01] CRITICAL · `core/cli.py:88-109`** — ✅ `main()` 丢弃全部命令返回值且不 `sys.exit`，`factorminer`（`setup.py:15` console_script）退出码恒 0；`run_backtest_cmd` 的 `return 1` 被吞，guard 抛错变裸 traceback，CI/脚本按 `$?` 判断时失败被判成功。
- **[K-02] CRITICAL · `core/cli.py:59-62`** — ✅ `--factors` 默认仍是 6 个硬编码已降级/已删 id，而 `commands/backtest.py:34` 已改空默认 + 运行时 `_discover_factor_ids`；主入口不传 `--factors` 时后者永不触发，`python -m core.cli backtest` 与 `python -m core.commands.backtest` 行为完全不同。两份 `parse_args` 还导致 `--holdout-mode` choices 不一致。
- **[K-03] HIGH · `commands/download.py:28-40`** — 不接收 `download_ohlcv_batch` 的 `{'success': False}`，无条件打印 "Batch download completed." 且退出 0；该下载器把所有异常都吞成返回字典从不抛出。补充：`--mode` 的 `full`/`incremental` 在 `batch_downloader.py:330/352` 是死值（都走 merge）；与 `update_data.py` 形成重复实现，后者有幂等计划/退避/裁剪未收盘/JSON 报告/失败退出码，前者一点没复用。
- **[K-04] HIGH · `commands/backtest.py:17-18,175-178,215-221`** — `--config` 覆盖优先级与自身 docstring 相反：`overrides.get(key, args.x)` 是 **config 赢 CLI**，用户显式 `--commission 0.001` 被旧值覆盖无提示；`--config` 路径不存在/JSON 损坏时静默跳过（`inspect.py:47` 同款）。所有 CLI flag 带非 None 默认值，命令层无法区分"显式传参"与"默认值"。
- **[K-05] HIGH【需确认意图】 · `commands/backtest.py:194-205`** — `purpose="final"` 的 `ledger.reveal()` 在回测产出**之前**落账，回测因无重叠数据/缺价格文件抛异常时窗口已被烧掉，重试必然撞 `HoldoutReuseError`；且只要传了 `--orientation-db-root` 就置 final，即便它等于 `db_root`（此时 `strict_oos=False`）。
- **[K-06] HIGH · `commands/inspect.py:56,60-68`** — 只给 `--start` 或只给 `--end` 时 `periods=None`，guard 走 unbounded 只 warning 放行 → **单侧区间即可完全绕过一次性 holdout ledger 检查**（与 E-02 是同一 guard 的两个洞口）；`guard_periods` 抛错在 try 块之外，是裸 traceback。
- **[K-07] MEDIUM · `commands/backtest.py:80-92`** — 权重 `id=w` 分支不校验未知 id（拼错则静默全用 1.0）；混合格式 `fac_a=0.5,0.5` 落入 positional 分支抛未捕获 ValueError；不校验 NaN/inf/全零。补充：`--no-expand-combinations` 在两层 parser 都定义但 `run_backtest` 无 `resolve_combinations` 形参，是死开关。
- **[K-08] MEDIUM · `commands/run_experiment.py:38,42`、`cli.py:22,27`** — `--miner`/`--user-dir` 带默认值却**无条件覆盖** config 的 `paradigm`/`user_dir`（同文件 `max_iterations`/`seed` 都正确只在显式传入时覆盖）→ config 写 `LLM` 被静默改成 `GP`，manifest 的 `operator_set_hash` 与实际不符。补充：5 个 command 各自 `logging.basicConfig` 而 `cli.py` 不配置；`mine` 命令层完全未接 holdout_guard；`experiment` 子命令缺 `--holdout-mode`。

---

## 8. api/（4 个文件，零问题：无）

- **[A-01] CRITICAL · `main.py:37-49,63-66`** — ✅ **实测坐实**：全局 `dependencies=[Depends(require_auth)]` 被 FastAPI 复制到 `APIWebSocketRoute`，但 `require_auth(request: Request)` 在 websocket scope 下无法注入 `request` → `TypeError`，配不配 token 都失败。`/ws/monitor` 100% 不可用，任务进度/日志/取消/scatter/下载进度全收不到，`useWebSocket` 永远连不上。附带：默认无 token 时所有写端点无鉴权 + `allow_origins/methods/headers=["*"]`，任意第三方页面可跨站调用；`==` 非常量时间比较；`/api/health` 精确 path 白名单在反代前缀下失效。`tests/test_api_auth_cors.py:25` 只构造 http scope，从未覆盖 WS。**修法**：参数改 `HTTPConnection`，WS 单独用 query/subprotocol 传 token，`hmac.compare_digest`，收紧 CORS。
- **[A-02] CRITICAL · `main.py:213,225,741,764`** — 同步 CCXT 网络 IO（`load_markets`/`fetch_tickers`）、`pd.read_feather`（数百 MB）、三重循环串行 `await` 路由函数，全部直接写在 `async` 路由里 → 单 worker 的 event loop 被冻结，期间所有 HTTP、`manager.broadcast`、`check_cancelled` 停摆，等价整站 DoS。`run_mining_task_background` 正确用了 `to_thread`，说明是遗漏。
- **[A-03] HIGH · `backtest_runs.py:115-121,205-211`** — 后台线程无并发上限、daemon 被进程退出直杀、`finally` 不执行 → `status.json` 永久 `running`，启动时**没有** TaskStore 那样的 `running→interrupted` 迁移 → 幽灵任务；`asyncio.to_thread` 用默认线程池会让 uvicorn shutdown join 卡死；`TERMINAL_STATUSES` 不含 `interrupted`，`request_cancel` 对幽灵任务返回虚假 `cancelled=True`。
- **[A-04] HIGH · `main.py:805-821,844-854`** — 下载进度回调在 `to_thread` worker 线程里调 `asyncio.get_running_loop()` 必抛 `RuntimeError` 被 `except: pass` 吞掉 → 所有中间进度丢失，UI 从 0 直接跳 100（失败时也被伪装成 100%）。对比同文件 `606/642/654` 挖矿用的 `run_coroutine_threadsafe(..., main_loop)` 是正确写法；`loop.create_task` 未保留引用可能被 GC。
- **[A-05] HIGH · `ws_manager.py:18-24`** — 广播失败的连接既不移除也不 close，`active_connections` 单调增长；迭代中 list 可被 connect/disconnect 修改；裸 `except:` 连 `CancelledError` 一起吞使 shutdown 无法取消；WS 端点只捕获 `WebSocketDisconnect`，TCP 半开（无 ping/pong）跳过清理。
- **[A-06] HIGH · `main.py:299,321,88`** — `logic_reference.source_file` 未取 `.name`，`../../...` 或绝对路径可让 `/api/factors/{id}` 回显任意文件（同函数 `model_file` 已用 `Path(...).name` 防护，是明显遗漏）；`get_run` 的 `run_id` 不校验可探测 `BACKTEST_ROOT` 之外目录；所有根路径相对 cwd 且 `TaskManager.load()` 在 **import 时**按 cwd 读盘。
- **[A-07] MEDIUM · `main.py:621-625,606,717-719`** — 并发挖矿任务把各自 `ws_handler` 挂到同一批全局 logger（`core`/`custom_miners`/`custom_fitness`）→ 日志串台广播到别人的 task_id；A 结束 `setLevel(前值)` 会还原掉仍在运行的 B 需要的 INFO 级别；`run_coroutine_threadsafe` 的 future 从无人检查。
- **[A-08] MEDIUM · `main.py:270/443,741,769,176`、`task_store.py:53,88-96`** — `get_data_coverage` 无 `date` 列时隐式返回 `null`；批量端点 `await` 另一个路由函数，任一组合触发 422 会冒泡使整批结果全丢；`TaskStore.load()` 只校验 `id`，缺 `start_time`/`status` 会让 `/api/tasks`、`/api/stats`、`/api/dashboard` 三端点 500；`_trim` 不裁 `factors`/`metrics`；`/api/download_data` 的秒级 task_id 会同秒碰撞且不进任务表；`success_rate` 在 stats 是字符串、在 dashboard 是 float。

---

## 9. execution / inspector / analysis / storage / config / utils / startup / cli（17 个文件，零问题：5 个 `__init__.py`、`core/config/defaults.py`）

- **[S-01] CRITICAL · `core/utils/dynamic_loader.py:44-58`** — 插件经 `exec_module` **裸执行顶层任意代码**，无 `sys.modules` 注册、模块名按 stem 拼装（不同子目录同名互相覆盖）、`except Exception` 拦不住 `SystemExit`/`os._exit`；`rglob` 顺序不确定使注册结果不可预测。与 `code_sandbox.py` 的 AST 白名单 + 子进程隔离形成强烈反差。
- **[S-02] HIGH · `core/startup_validation.py:20-21,35,56-57`** — ✅ `NATIVE_MINERS = {"GP","RL","LLM"}` **无 "NN"**，但 `director.py:88` 支持 NN；`DL→NN` 迁移后又被自己判未知 → `--miner NN`/`DL` 一律被拒且提示里列出不存在的候选项。校验面过窄：seed 不一致、缺 data_quality/manifest、成本恒等式、holdout/windows 全不拦；`fitness: 0`、`search_space: []` 等 falsy 非法类型漏检；`isinstance(True, int)` 让 `max_iterations: true` 通过；`user_workspace` 目录不存在也被计为 fatal，首次运行合法配置照样拒绝启动。
- **[S-03] HIGH · `core/inspector/resolver.py:45-65`** — 逻辑契约分派与 `execution/compiler.py:35-79` 不一致：compiler 支持 `nn_channel`/`dl_channel`/`rl_actions`，`factor_storage.py:243-248,165` 确实这样落库，resolver 全不支持 → **NN/RL 因子根本无法审查**；`execution/inspector.py:34-44` 又是第三套按 miner_type 的分派，三处契约各自漂移。
- **[S-04] HIGH · `core/inspector/metrics.py:53-56,118-155,283,296-300`** — Lag IC Decay 四条分支口径互异（lag 期累计前瞻收益 / `pct_change(lag).shift(-lag)` 过去收益前移 / `returns.shift(-(lag-1))` 单期平移），`reporter.py:65-66` 却统一打印为 "RankIC across Lags"；cross_asset 分支丢弃 `target_spec` 使报告宣称的收益定义是假的；截面 `calc_cs_stats` 同样用 i.i.d. 标准误。
- **[S-05] HIGH · `core/inspector/reporter.py:32-77`** — 失败/空序列被渲染成"干净"全 0 表格（`engine.py:136/182` 存的是 `{"error":...}`，reporter 完全不读）；空 IC 序列的 `pos_ratio` 被硬凑成 100%/0%；`overall` 在两分支同名不同义；turnover 单资产归一化而 cross_asset 未归一化却同表展示。
- **[S-06] MEDIUM · `core/analysis/factor_analytics.py:52,74,83,121-131`** — `quantile_spread` 用 `max-min` 而非 `Q_last-Q_first`（非单调时系统性高估且恒正）；`_finite_number(...) or 0.0` 让缺失伪装成 0；`rolling_ic` 两模式语义不同却共用 `latest_rolling_ic`；IC 用 Pearson 与主打 Spearman 不符；`window = min(rolling_window, len(frame))` 在 cross_asset 下 `len(frame)=T×N` 导致 `min_periods` 超序列长度、全 NaN；`pivot` 遇重复 (ts,asset) 抛 ValueError。
- **[S-07] MEDIUM · `core/storage/factor_storage.py:271-280,315,326-352`** — `save_factor_values` 写出**全 NaN 的 forward_return**，而 `analyze_factor_snapshot:117` 用 `dropna` 把所有行删光并抛错 → 写端能存、读端必炸；parquet 先落盘、metadata 失败返回 False 会留孤儿；`load_factor_values` 无 try；`factor_id` 未校验可含路径分隔符。
- **[S-08] MEDIUM · `core/cli.py:59-62,72-74,88-109`** — `commands/inspect.py:47-56` 的 `--config` 损坏时只 warning，随后用 `engine.py:75-79` 硬编码默认 `BTC/USDT:USDT` / `2025-07-20..08-15` / `5m` 跑出**与配置完全无关的报告**；单给 `--start` 被无声丢弃；`--type` 默认 spot 与 `backtest --trade-type` 默认 futures 不一致；`cli.py:72-74` 又硬编码了 `defaults.py:20` 声明"不要重复硬编码"的成本常量；`import logging` 未使用。

**补充**：`compiler.py:30-32` 的 `logic_ref.get("type")` 未做 `or {}`，`logic_type=None` 时会用 `ast={}` 编译出"能返回但计算无效"的因子；`deploy_to_live_server:109-116` 是恒定抛 `NotImplementedError` 的占位 API；`metrics.py:24` 用可变默认参数 `lags=[1,2,3,5,10]`。

---

## 10. scripts / 配置 / 部署 / 测试

**已逐个读完**：`scripts/` 5 个 .py、`user_workspace/configs/` 13 个 json、`user_workspace/experiment_tools/` 14 个 .py、requirements×2、setup.py、ci.yml、Dockerfile、docker-compose.yml、.dockerignore、nginx.conf、package.json、.gitignore、`tests/` 41 个（精读 9 + 全量扫描）。
**零问题项**：`experiment_tools/__init__.py`、`requirements-dev.txt`、`web/package.json` 与 CI frontend job（Node 20 / lock / `tsc -b && vite build` / cache 路径均正确）、`.dockerignore:6-8`、`tests/conftest.py`、`test_review_p0_fixes.py`、`test_review_p1_fixes.py`、`test_data_naming.py`（断言扎实，非自欺）。

- **[T-01] CRITICAL · `configs/configRL.json:15`** — ✅ 字段名写成 `rl_config`，而 `rl_miner.py:87-89` 只读 `config["rl"]` → `learning_rate 0.5` 静默变默认 0.05、`max_depth` 丢失；`configAdvancedGP.json:2` 用顶层 `"miner"` 而 `director.py:43` 只读 `"paradigm"`；`startup_validation` 对拼错的顶层键不报错。对照 `_p0_smoke.py:37` 用的是正确键 `"rl"`。
- **[T-02] CRITICAL · `web/src/**`（6 个文件）+ `web/nginx.conf`** — ✅ 全站硬编码 `http://localhost:8000` / `ws://localhost:8000`，vite 无 proxy、nginx 无 `/api/` 与 `/ws/` 反代；`Dockerfile:7-9` 注释自认这是设计。远程部署时浏览器把 localhost 解析成用户自己的机器，HTTPS 页面下 `http://`/`ws://` 还会被 mixed-content 拦截 → UI 全废。
- **[T-03] HIGH · 12/13 配置的 `target.entry_price`** — ✅ 10 个走默认 + 2 个显式 `current_close`，仅 `configExperiment.json` 用 `next_close`；而 `targets.py:125` 注释明写 `next_close` 才与回测 `NEXT_BAR/CLOSE` 对齐（test_review_p1_fixes.py:78-94 已锁定该回归）。**评估标签比可成交收益早一根 bar，IC/RankIC 系统性乐观**；`post10_factor_audit.py:169`、`recompute_oos_factors.py:125` 也都是 `current_close`，交叉验证互相"印证"了错误基线。
- **[T-04] HIGH · `scripts/update_data.py:255`** — 下载后验证 `fresh = {item["safe_symbol"]: item ...}` **未过滤 stream**（`plan_updates:116-117` 明确过滤了）；stream 与 OHLCV 共享 `safe_symbol`，同名键后者覆盖前者 → OHLCV 下载失败但 funding 流恰好更新到 T-1 时被误判 `updated` 并 exit 0。这是 "Loud on failure" 承诺的直接反例。
- **[T-05] HIGH · `experiment_tools/summarize_oos_validation.py:146-153`、`scripts/reaudit_factor_db.py:218-236`、`validate_gates.py:239-253`、`validate_crypto_features.py:248-253`、`audit_llm_holdout.py:211`** — ✅ 结论段落写死 "-7.37% / -11.18% / 虚高约 4.6pt"、"84 个因子"、"即使分母降到 1 通过数也只有 1"、"6 个因子全部被拦"、"9 个代码候选"，不随计算结果变化；`validate_gates.py` 甚至与自身 `n_trials=1 时 6 个里 5 个能过` 直接矛盾；`validate_gates.py:378` 通过判定只看 `blocked == total`，与叙述无关 → **文档层永远通过**。
- **[T-06] MEDIUM · `Dockerfile:39-50`、`docker-compose.yml:14-18`** — 镜像未 `COPY user_workspace/`，直接 `docker run` 时 `/api/configs` 空、自定义算子 ImportError；容器 `USER miner` uid 10001 而 bind mount 属宿主 uid，`chown` 只作用镜像层被挂载覆盖 → 无法写 data/factor_db/reports/logs；api 无 healthcheck 依赖。
- **[T-07] MEDIUM · `setup.py:10`、`requirements.txt:2,19`、`.gitignore:95-99`** — ✅ `pip install -e .` 必失败（装 PyPI 上不存在的 `ml4t-backtest`，且只列 2 个依赖）；`pandas>=1.5.0` 与 `post10_factor_audit.py:180` 的 `include_groups=False`（需 ≥2.2）冲突；`feather-format` 废弃包会拖回旧 pyarrow；`.gitignore` 的 `data/` 被忽略后 `!` negation 因父目录被排除而不生效 → fresh clone 无 `data/binance/futures/`，`test_universe_holdout.py:217` 在 CI 上**必然 skip**（docstring 却声称 "works on a fresh clone too"）；CI 缓存键只盯 requirements。
- **[T-08] MEDIUM · `experiment_tools/recompute_oos_factors.py:100-147` 等** — 编译/计算失败只 `[WARN] continue`，全部失败仍打印 "recompute done." 且 `main()->None` → exit 0；直接覆盖写 parquet，某因子本次全失败时旧 OOS 快照不清理 → 陈旧与新快照混库；`download_universe_holdout.py:105` `ok = bool(result.get("success", True))` 默认成功；`reaudit_factor_db.py:281` oos_root 不存在时静默关闭全部 OOS 闸门；`validate_alphapool.py:191` OOS 目录不存在跳过 Part 2 却仍写"OOS 探针弱表现"；多数 `main()` 无返回值、默认路径全相对 cwd；`post10_factor_audit.py:320` 算出的 `factor_equivalent_within_1e_10` 不进任何 gate。**CI 只跑 `pytest tests/ -q`，对上述 19 个脚本零测试、零 smoke、零退出码断言**。

**补充**：`regate_p0.py:76-108` 复现 reaudit 的全部问题（库大小 n_trials、逐因子 BH、`metrics.update`、非原子写、备份跳过）；`download_universe_holdout.py:103` 与 `update_data.py:200` 同用未实现的 `fill_gap`；`run_ml4t_backtest.py:90` 硬编码"六因子全负 IC"反转。

---

## 11. web/src/（14 个文件，零问题：`App.tsx`、`main.tsx`、`package.json`、`i18n.tsx`）

> 契约核对结论（避免误报）：前端发送的 11 个字段**全部**落在 `BacktestRequest`/`RUN_FIELDS` 内，响应类型与后端一致，无被静默丢弃的多余字段；i18n 三语各 177 键、无缺失、插值参数全匹配；后端任务状态全集不含 `timeout`。

- **[W-01] CRITICAL · 6 个文件 + `nginx.conf`（同 T-02）** — 全站硬编码 localhost:8000，生产部署全废；`try_files` 把 404 也吞成 index.html，故障更难察觉。
- **[W-02] CRITICAL · `Backtest.tsx:75-79,307-308`** — ✅ `pct()` 只加 `%` 不乘 100，但 `cagr`/`volatility` 是小数 → 界面 -0.18% / 0.21%，真实 -17.88% / 20.75%。附带 `max_drawdown_pct` 正值无负号、结果卡未落实涨红跌绿、历史列表 `Number(undefined)=NaN` 误标绿色、多处 `toFixed` 前未判空。
- **[W-03] HIGH · `hooks/useWebSocket.ts:56-66`** — ✅ `useEffect` 依赖只有 `[connect]`，cleanup 捕获的 `socket` 恒为首次渲染的 `null` → 卸载后连接不关闭且 `autoReconnect` 无限重建；`StrictMode` mount→unmount→mount 直接留孤儿连接；`Launchpad.tsx:52` 的 scatter 无上限（logs 有 `slice(-49)`），长任务内存单调增长；日志 `key={i}` + `reverse()` 重排导致行复用错乱。
- **[W-04] HIGH · `Backtest.tsx:137-156`、`Inspector.tsx:322-369`** — 轮询 `openRun` 无条件 `setActive`，无请求序号/AbortController → A 的飞行请求返回后把用户新查看的 run B 冲回 A，图表与 `statusDraft` 错位；卸载后仍 setState。`Home.tsx:93-111` 有 `active` 标志，此处缺位。
- **[W-05] HIGH · `Launchpad.tsx:112-132`、`DataDownload.tsx:89-98,117-133`** — 启动/停止/下载均不检查 `response.ok`、无 `window.confirm`、无提交禁用态、错误只进 console；连点可重复启动任务；下载唯一退出条件是 `progress>=100`，后端失败/WS 断连/永不发 100 时 `isDownloading` 永远 true、按钮区被进度条替换 → **死锁状态机**。
- **[W-06] HIGH · `DataDownload.tsx:142-156`、`Backtest.tsx:214-217`** — 自定义 symbol 只 `.trim().toUpperCase()` 无正则白名单，`../../` 直达后端下载/落盘路径；回测 `Number('')=0` 把费率静默清零、`Number('abc')=NaN` → JSON null 触发 422；日期异常时 `new Date(...).toISOString()` 抛 Invalid time value。与「后端不校验请求参数」叠加放大。
- **[W-07] MEDIUM · `Home.tsx:76-84,185-193`、`MainLayout.tsx:57-58`** — 任务状态样式表漏 `cancelled`/`interrupted`，用户主动停止的任务显示成"失败"红色且统计卡片合计对不上 `tasks.total`；导航栏引擎状态**永远写死在线**（完全不读 `/api/dashboard`），与 `Home.tsx:137` 基于 `engine_online` 的真实状态自相矛盾。
- **[W-08] MEDIUM【需确认意图】 · `Backtest.tsx:212-225`** — 前端不发 `orientation_db_root` / `holdout_mode` / `allow_holdout_reuse`，UI 无任何 OOS 与真实成本开关 → Web 回测恒为 `purpose="explore"`、`strict_oos=false`，界面收益容易被误读为经过严格 OOS 验证；表单写「资金费/日」但后端按 bar 计。

**未单列的小瑕疵**：`FreshnessPanel.tsx:102,154,222` 无判空/卸载保护、key 未含 exchange；`Inspector.tsx:379` `-Infinity` 参与减法；`DataDownload.tsx:328/346/430` 数组下标作 key、`fetchCoverage` 多请求乱序；`Home.tsx:110` 5 秒轮询仍可能乱序覆盖。

---

## 12. 与上一轮 A/B/C 清单的关系

上一轮 `quant_review_20261115.md` 的约 30 条**不在本册重复展开**，此处仅作索引，其中若干条在本轮找到了新的触发点：

| 编号 | 主题 | 本轮新触发点 |
|---|---|---|
| A1 | 每代 fitness 读 test_returns | — |
| A2 | 沙箱允许 `shift(-1)` | **M-08**（AST 自定义算子同样无因果校验）、**E-06**（资源维度逃逸） |
| A3 | 回测方向用评估窗口未来收益 | **W-08**（Web 无开关） |
| A4 | BH-FDR 家庭规模=1 | **X-05**（budget snapshot 非原子 + 截断不自洽）、**E-04**（trace 去重低估 N） |
| A5 | 常数/爆炸因子入库 | **M-06**（未知 terminal 造 0）、**D-05/07**（未收盘 bar、bfill/inf） |
| B1 | 年化 √252 | **E-07**（`1M`→525600） |
| B3 | h>1 重叠收益 | **E-05**（gross/net 混用）、**E-08**（fitness 与 gate rebalance 口径漂移） |
| B7 | ic_series 并列秩/填 0 | **S-04**（Inspector 截面分支同病） |
| C1 | 无 seed/manifest | **G-05**（两个独立 RNG）、**X-02**（resume 不 verify） |
| C4 | 无日期回测绕过 guard | **E-02**（单侧 final 烧掉无限窗口）、**K-06**（inspect 单侧后门） |
| C5 | 配置引用无数据周期 | **T-01**（字段名写错）、**T-03**（entry_price 口径） |
| C6 | 落库非原子 | **S-07**、**D-08**、**X-06** |
| C7 | 请求参数校验缺失 | **K-07**（权重侧）、**W-06**（前端侧）、**A-06**（磁盘 metadata 侧） |
| C8 | 协作式取消语义 | **A-03**（幽灵任务虚假 cancel） |
| C9 | fill_gap 未实现/原子写 | **K-03**、**D-01/02/03/08** |
| C10 | timeout 语义 | — |

---

## 13. 优先级修复路线（更新版）

**第 0 层：让系统真的跑、真的报错、真的显示对**（本轮新增，优先于上一轮的最小修复集）
1. `A-01` WS 鉴权依赖改 `HTTPConnection`（1 行，恢复整个实时通道）
2. `W-02` 前端数值按 `*_pct` / 小数两类分别格式化
3. `K-01/K-03` CLI 统一退出码与下载失败返回
4. `D-03/D-04/D-02` gap_filler 接真实下载、health_checker 的 `invalid_count`、下载分页按末根时间戳推进
5. `T-05` 报告结论改为插值生成 + 一致性断言，`T-08` 脚本失败返回非零并纳入 CI smoke
6. `T-02/W-01` API base 改相对路径 + nginx/vite 反代

**第 1 层：口径对齐**（决定 IC 是否可比）
7. `T-03` 12 个配置的 `entry_price` 统一为 `next_close`（或改 `ForwardReturnTarget` 默认值）
8. `M-01` `_ts_rank` 方向、`M-02` 岭回归标准化、`E-07` 年化单位
9. `E-05/E-08` gross/net 与 fitness/gate 的 rebalance、成本口径统一

**第 2 层：上一轮 A 级泄漏**（决定结论真伪）
10. `A1` 切断 mining 循环的 OOS 输入 → `A2/M-08` 堵负 shift + 因果探针 → `A3` 强制方向来自 in-sample → `A4/X-05` BH family 与 n_trials → `A5/M-06` 拒绝常数/爆炸因子

**第 3 层：可复现与运维**
11. `C1/X-02/G-05` run manifest + 统一 seed + resume 校验；`K-02/S-02/T-01` 配置与默认值单一来源；`E-02/K-06` guard 单侧与 unbounded 收口；`E-04/X-06/A-03` ledger 与任务状态的并发/原子性

---

## 14. 审查边界

- 全部结论来自静态通读 + 对现有产物的**只读**复现（84 份 metadata/parquet、6 行 fdr ledger、holdout ledger、`summary.csv`、一次历史回测运行、真实 ASGI websocket 握手）。
- 未执行挖掘、下载、新回测或 `holdout_guard.reveal`，因此**未验证**这些结论在真实长跑中的数值幅度（例如 A1 把 IC 抬高多少、D-02 造成多少比例的数据缺失）。
- 标注【需确认意图】的 8 处（M-03 mutual-IC 定义、M-08 liquidation lag、K-05 reveal 时机、X-08 循环论证、D 命名歧义、W-08 是否只做探索式回测等）需作者确认设计意图后再改。
- 各 agent 报告的文件:行号基于当前工作区版本；主审查人已亲自复核第 1 节列出的 16 项，其余为静态审查结论。
- 本轮**未修改任何代码或数据**；仅新增本报告与记忆文件。

---

## 15. 第 0 层修复状态与勘误（2026-11-15 续）

### 勘误（实测推翻原结论）
- **✅6 `startup_validation` 拒绝 NN 属于条件性误报，本轮未改该文件。** 实测 `load_user_modules("user_workspace")` 后 `MinerRegistry._registry` 含 `NN` / `MyCustomNN` / `MyTemporalNN`（load errors = 0），以 `configNN.json` + `paradigm=NN` 调 `validate_mining_startup` 返回 OK；`director._instantiate_miner` 也是**注册表优先**，`NN` 只在未加载用户模块时才会落到 `raise ValueError`。原结论只在「未加载用户模块」时成立，不能据此把 `NN` 塞进 `NATIVE_MINERS`（那会掩盖插件未加载的真实故障）。

### 本轮已完成（只改第 0 层，未触碰并行的 P0/P1 未提交改动）
| 项 | 处理 | 验证 |
|---|---|---|
| `A-01` / `T-02` / `W-01` | 上一轮已修（WS 鉴权 + 端点统一 + nginx/vite 反代） | 真实握手 101/401 |
| `K-01` | `cli.main()` 归一化命令返回值 → 真实退出码；无命令返回 2；`--factors` 默认改为空，交给 `_discover_factor_ids` | `python -m core.cli` → 2；`--help` → 0；单测覆盖 int/bool/dict/None |
| `K-03` | `download.run_downloader` 检查 `{'success': False}` 与异常，任一失败返回 1；`SmartBatchDownloader` 改延迟导入（缺 ccxt 也能加载 CLI） | 4 个单测（全失败/部分失败/异常/全成功） |
| `D-03` | `gap_filler.execute_fill_tasks` 删除模拟成功；无真实 downloader 时显式 `success=False`，注入 downloader 时按真实返回计数；`auto_fill_gaps` 不再硬编码 `success=True` | 5 个单测 |
| `D-04` | `health_checker.invalid_count = int(invalid_ohlc.sum())`（原写法首次赋值必取 0） | 2 个单测（1 条坏 OHLC 被报出 / 干净数据无告警） |
| `D-03` 附带 | `processor.detect_gaps` 排序后 `reset_index(drop=True)`，`large_gaps.items()` 的 label 与 `.iloc` 位置不再错配 | 1 个单测（非零 index 的断层边界正确） |
| `W-02` | `Backtest.tsx` 拆 `pct()`（已是百分数）/ `pctRatio()`（小数 ×100）/ `drawdownPct()`（回撤显负号），指标卡按涨红跌绿着色，历史列表 NaN 不再误标绿色 | `npm run build` 通过；真实 `summary.csv` 中 `cagr=-0.1788` → -17.88% |
| 第 1 节复核 11 / 12 | `setup.py` 以 `requirements.txt` 为单一依赖来源（含 pinned git commit）；`configRL.json` 顶层键 `rl_config` → `rl` | 27 行依赖全部通过 PEP 508 解析；`learning_rate=0.5` 现可被 `RLFactorMiner` 读到 |

**验证**：全量 `pytest -q` → 493 passed / 1 skipped / 56 subtests；`npm run build` 通过。首轮全量曾有 2 个 `test_llm_guardrails.py` 的 `diagnostics` KeyError，复跑同一套件全绿，属偶发状态污染，与本轮改动无关（单独跑该文件 22 passed）。

**仍待处理（未动，改动面或研究口径需先确认）**：`D-02` 下载分页按自然月 + `limit=1000`（需重写并按末根时间戳推进）；`D-01` 空返回死循环；`T-05/T-08` 脚本结论插值与 CI smoke；`T-03` 12 个配置 `entry_price` 统一 `next_close`（会改变既有 IC 结论）；`E-01` DSR 的 σ(SR) 与 clamp；`E-02` holdout_guard 单侧无限窗口；`E-07` `infer_annualisation('1M')` 大小写歧义（1M=月被当 1m=分钟）；`M-01` `_ts_rank` 方向。

---

## 16. 第 0 层收尾 + 第 1 层 7/8（2026-11-15 第二轮）

按 §13 编号继续推进（用户「继续按顺序修复」），本节覆盖第 4、5、7 项与第 8 项的两处。

| 项 | 处理 | 验证 |
|---|---|---|
| `D-02`/`D-01` | 新增 `core/data_feed/pagination.py`：`paginate_ohlcv` 按**返回末根时间戳 + 1 根**推进，空返回有界前进、停滞/异常/超批次上限均返回 `complete=False` 与 `stopped` 原因；`data_downloader.download_ohlcv`、`batch_downloader.download_ohlcv_batch`、`data_downloader._download_missing_data` 三处统一调用，并在 `complete=False` 时返回 `success=False`，不再把残缺数据判 100 分落盘 | `tests/test_pagination.py` 8 项；venv 实测两个下载器对 incomplete 均返回 `success=False`；1m/30 天 43200 根全覆盖 |
| `T-05` | `summarize_oos_validation.py` 新增 `build_conclusions(runs, ics)` 纯函数，结论里的收益/Sharpe/衰减/翻转数/成本侵蚀/泄漏虚高/因子数全部由数据生成，定性判词随符号变化；`reaudit_factor_db.py` 的拦截主因、库规模、分母敏感性结论、`reaudited_at` 日期全部插值；`validate_gates.py` 的拦截数/DSR 区间/宽松分母通过数/OOS 收益改为从 `df` 与 `summary.csv` 读取，读不到写 `n/a` | `tests/test_report_conclusions.py` 9 项，含「改数据→结论跟着变」与「源码不含历史数字」静态断言 |
| `T-08` | 三个脚本补失败退出：空数据/数据不一致返回 1，正常返回 0；CI backend 新增 `Smoke-check research scripts`（`compileall` + 空库运行必须非零退出） | 本机 compileall=0；空库 `reaudit` 退出码 1；CI YAML 解析通过 |
| `T-03` | 12 份配置显式声明 `target.entry_price = "next_close"`（7 份补 target 块、3 份补字段、2 份从 `current_close` 改），`configExperiment.json` 原本已是 next_close 未动 → 13/13 对齐 NEXT_BAR/CLOSE | `tests/test_layer1_alignment.py` 遍历断言；**注意：这是口径变更，factor_db 84 份历史 metrics 与新口径不可比，需用新配置重算基线** |
| `E-07` | `annualisation.py` 区分大小写：`1M` = 月（12 期/年）、`1m` = 分钟（525600 期/年），新增 `1Y` | 新增 4 项断言，旧 `1d/1h/5m/4H` 行为不变 |
| `M-01` | `operator_runtime._ts_rank` 改为与 `rank(pct=True)` 一致的平均秩（最大值→1.0、并列取平均），原实现对递增序列恒返回 0.0 | 新增 4 项断言，与 pandas `rank(pct=True)` 逐窗口对齐 |

**验证**：全量 `pytest -q` → **519 passed / 1 skipped / 72 subtests**；`compileall` 通过；CI YAML 通过。

**本轮刻意未动（与工作区用户并行的 P0/P1 未提交改动重叠，等其提交后再修）**：`M-02` 岭回归标准化（`pool.py` / `expressions.py`）、`E-05/E-08` gross/net 与 rebalance 口径（`evaluator.py` / `portfolio_eval.py`）、`E-01` DSR 的 σ(SR) 与 clamp（`significance.py`）。这些文件同时被用户改动，单独提交会造成补丁交叉；第 2 层 A 级泄漏（`A1` mining 循环 OOS 输入、负 shift、方向来自样本内、BH family）与第 3 层仍按原顺序排队。

---

## 17. 第 1 层第 8 项收尾：`M-02` 岭回归标准化（2026-11-15 第三轮）

用户「继续修复第 8 项」→ 补齐上轮留下的 `M-02`。`M-01` / `E-07` 已在 §16 完成，至此 §13 第 8 项全部结束。

| 项 | 处理 | 验证 |
|---|---|---|
| `M-02` | 新增 `core/miner/ridge_weights.py::fit_scale_aware_ridge`：先按 finite mask 取样本、逐列 z-score（均值/标准差只在拟合样本上算）、常数列标记 degenerate 并置零斜率（不再除 ~0 产生 1e19 权重）、**前置全 1 列做不惩罚截距**（目标均值不再被吸进斜率）、λ 在标准化后天然按列尺度归一；最后把斜率**映射回原始空间** `w_raw = w_z/std`、`b_raw = b - Σ(w_z·mean/std)`。`AlphaPool._optimize_weights` 与 `fit_linear_combination` 两处统一调用；`AlphaPool.report()` 新增 `ridge` 诊断段（method / center / scale / z 权重 / degenerate / 截距） | `tests/test_ridge_standardization.py` 9 项；既有 `test_alphapool.py` / `test_linear_combination.py` / `test_backtest_weights.py` / `test_review_p1_fixes.py` 合计 39 passed；全量 **528 passed / 1 skipped / 72 subtests** |

**为什么映射回原始空间，而不是让 OOS 复用 scaler**：`LinearCombinedFactor.compute()` 与 `backtest_engine.resolve_signal_weights()` 都按 `Σ(w_j·raw_factor_j)` 消费权重，`to_dict()` 只序列化 `weights`。若返回 z 空间斜率，落库的 84 份组合因子与全部回测会被静默改义。映射回原始空间后「OOS 用同一 scaler」由代数恒等式保证（测试 `test_raw_space_prediction_matches_standardised_space` 逐元素验证 `Z·w_z + b_z == X·w_raw + b_raw`），无需持久化状态。截距是逐截面常数，对 RankIC / rank 信号 / 回测不变，故不入序列化，但保留在诊断里。

**实测对比**（`n=1200`，列尺度 0.01 / 1e5，均值 3 / 2e4，真值 `[80, 6e-6]`，λ=1）：旧 `X'X+λI` 解 `[2.287, 5.80e-6]`，小尺度列被压掉 **97%**，z 空间权重 `[0.022, 0.594]` 几乎全给了大尺度列；新解 `[79.936, 5.99e-6]`，z 空间 `[0.782, 0.613]`，截距 `-232.93`（真值 -233.12）。

**`walk_forward.py:_fit_ridge` 未动**：它已有 `_standardise()` 在 train 上求 mean/std 并应用到 val，M-02 的量纲问题不存在；仅缺不惩罚截距。加截距会改变权重维度并牵动 `np.tensordot(weights, z_val, axes=(0,0))` 与 `_net_return`，属独立改动，列入第 2 层后续。

**与用户未提交改动的隔离**：`pool.py` 同时含用户的 P1-M8 改动（`weight_fit_shift` 默认 0）与本节修复。提交时用 `git update-index --cacheinfo` 只把「HEAD + 本节修复」的 blob 放入 index，工作区保留两者，因此 commit 不含用户补丁、用户的改动也不丢。`base.py` / `evaluator.py` / `portfolio_eval.py` / `significance.py` / `director.py` 等用户文件一个字节未动。

---

## 18. 第 9/10/11 项与第二波修复（2026-11-15 第四轮，§13 全线收口）

用户「继续完成剩余所有的修复」。本节记录第 9、10、11 项与对登记册的第二波扫尾；
**专家级全流程复审另见 `user_workspace/reports/expert_pipeline_review_20261115.md`**。

### 第 9 项：gross/net 与成本口径
- `portfolio_eval.py`：`ls_t` 改为净收益序列 `ls - turnover*cost_rate` 的 HAC t，保留 `ls_t_gross`，
  新增 `net_mean_return` / `net_std_return` / `net_sharpe` / `net_hit_rate`；`timing_returns()` 同步。
- `evaluator.py`：shrinkage 的 `n_obs` 由 T×N 改 T；gate 的 `rebalance_bars` 与 fitness 对齐；
  方向用 IC 时间序列均值而非 pandas `corrwith`。
- `significance.py`：DSR 的 `var_term<=1e-9` 改 fail-closed（不再 clamp 出 `z→∞` 假通过）；
  `sigma_sr` 从 trial population 估计；`n_trials > len(pvals)` 时用 `p=1.0` 补齐历史试验。
- `tests/test_layer1_9_regression.py` 10 项。

### 第 10 项：A 级泄漏
| 编号 | 处理 |
|---|---|
| A1 | `evaluate()` 默认不读 OOS；新增 `evaluate_final()`；`director._attach_final_oos_metrics()` 在冻结 shortlist 上一次性读验证期，只作 gate evidence；`val_read_count=1` |
| A2 | 静态拒绝负 `shift/diff/pct_change/rolling/ewm/expanding` 与 `bfill`；`causality_probe()` 截尾 5 根 bar 重算；`OperatorRegistry.register()` 注册期因果探针；`_ts_ref(n<0)` 抛错 |
| A3 | `load_factor_signals()` 默认拒绝从评估窗口推断方向；方向优先来自组合权重 / in-sample db / metadata 预注册；仅显式 `allow_inferred_orientation=True` 走 legacy；summary 加 `orientation_sources` / `orientation_window` |
| A4 | `budget.resolve()` 三个来源 union；corrupt snapshot fail-closed；`apply_promotion_gates()` 补历史 p 值；`reaudit_factor_db.py` 改全库批量 BH |
| A5 | `_safe_divide()` 零分母→NaN、inf→NaN；`_validate_metric_inputs()` 拒绝常数/爆炸/少于两个有限值；回测剔除无截面离散度因子日并 `rank(method="average")` |

### 第 11 项：可复现与运维
- `X-02`：`verify_manifest()` 对缺 hash fail-closed（返回 `missing`）；pipeline resume 先加载用户算子再校验，
  不可复现即拒绝续跑；两条路径都真正 `seed_everything()`；`seed_applied` 不再只是「声明了 seed」；
  `_data_files()` 锚定 `PROJECT_ROOT`。
- `G-05`：`manifest.derive_rngs()` 派生实例级 `random.Random` / `np.random.Generator`；
  `BaseFactorMiner.__init__` 注入；GP/RL 全部随机调用与 `sample_operator_params(rng=...)` 走私有 RNG；
  无 seed 时回退全局模块，保持旧行为。
- `E-02/K-06`：final guard 必须同时给 start/end（否则拒绝，不再烧掉 `0000-01-01..9999-12-31`）；
  `_normalise()` 拒绝垃圾值、保留日内时间、tz→UTC；`HoldoutLedger` 边界可比较化；
  `period_key` 带 timeframe/horizon 摘要；`inspect` 单侧窗口直接返回 2、guard 异常转退出码；
  `backtest` 同样校验单侧。
- `E-04`：`HoldoutLedger` / `ExperimentTrace` / `FdrLedger` 改为「每路径进程内锁 + `O_CREAT|O_EXCL`
  跨进程锁文件 + 决策前重读」；trace 改 append-only 日志、last-wins 去重，新增 `num_attempts`；
  未知键逐字段丢弃而非丢整行；corrupt ledger 直接抛错；director 用 `max(num_trials,num_attempts)` 做分母。
- `X-06`：`create_run` 拒绝 run_id 撞车；`TERMINAL_STATUSES` 去掉 `failed`（失败可 resume）；
  `STAGE_ORDER` 生效、终态不被 artifact 重开；`write_artifact` 原子文本写；新增 `save_manifest()`。
- `X-01/X-03/X-04/X-07`：OOS warmup 切片失败即拒绝发布快照；一次性 guard 移到数据校验之后且 resume 幂等、
  异常写 status；成本不一致改为显式告警 + walk_forward 默认取 `gate_cost_rate`；
  `_file_dates` 不再 `normalize()` 到当日午夜；0 folds 直接让 `validated` 阶段失败。
- `A-03/A-04/A-05/A-06/A-08`：回测任务并发上限 2、幽灵任务读时迁移为 `interrupted`、atexit 兜底、
  run_id 路径白名单；下载进度回调改 `run_coroutine_threadsafe(main_loop)` 且失败不再伪装 100%；
  `ws_manager` 快照广播 + 移除死连接；`source_file` 取 `.name`；TaskStore 记录补全并裁剪 factors/metrics。
- `K-04/K-07/K-08`、`S-01/S-02`、`T-01/T-04`、`D-08`：CLI 显式参数 > config > 默认、
  权重校验、`--no-expand-combinations` 真正接通 `run_backtest`；`--miner/--user-dir` 不再静默覆盖 config；
  启动校验拒绝 bool 迭代数、falsy 非法类型、拼错顶层键（对 `MyCustomRL` 的 `rl_config` 例外）、
  非法 seed/holdout mode；插件注册进 `sys.modules`、模块名去冲突、捕获 `SystemExit`、
  缺 workspace 降为 warning；`configAdvancedGP.json` 的 `miner`→`paradigm`；
  `update_data.py` 后验证过滤 stream；`get_data_info()` 读 date 列而非 RangeIndex。
- `tests/test_layer3_reproducibility.py` 38 项。

### 验证与状态
- 全量 `pytest -q` → **598 passed / 1 skipped / 75 subtests passed**（126s）；`compileall`、
  `ci.yml` / `docker-compose.yml` YAML 解析通过。
- 首轮全量曾 2 个失败：`test_cli_smoke` 的 `MyCustomRL` 合法使用 `rl_config` 被新校验误杀（改为仅原生 RL 判错），
  `test_director_gating` 的 MagicMock `num_attempts` 被 `int()` 成 1 抬高分母（加 isinstance 守卫）。均已修。
- **§13 第 0–11 项至此全部结束**。登记册中仍未修的约 60 条按「已确认 bug / 方法论风险 / 需确认意图」
  在 `expert_pipeline_review_20261115.md` §8 重新分级，并给出第 4 层最小修复集（§9）。
- **与用户并行 P0/P1 未提交改动的关系**：本轮若干修复（X-01 warmup 断言、G-05 实例 RNG、
  director 预算分母）直接建立在这些改动之上，逐文件隔离已不再可行；本轮提交将二者一并纳入，
  并在提交说明中显式标注。
