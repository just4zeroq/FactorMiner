# FactorMiner 全流程专家复审（修复后，2026-11-15）

> 角色：量化因子挖掘研究者视角。复审对象为 **§13 第 0–11 项全部修复完成后**的工作区。
> 方法：只读静态复核 + 对现有产物的实测（84 份 metadata/parquet、13 份配置、
> holdout ledger、freshness/window 规划、全量 pytest）。**未执行挖掘、下载、新回测、
> 未调用 `holdout_guard.reveal`、未修改 factor_db**。
> 结论一律标注【已确认 bug】/【方法论风险】/【需确认意图】，并给出文件:行号或实测数字。

---

## 0. 本次复审的基线（已修复，不再重复登记）

| 层 | 已修内容 | 验证 |
|---|---|---|
| 第 0 层 | WS 全局鉴权/反代、CLI 退出码、假成功下载/补洞、健康检查、前端 `pct()`、setup.py、configRL 键 | `654c74a` / `e403827` |
| 第 0 层收尾 | 下载按末根时间戳分页、空返回有界推进、报告结论插值、CI smoke | `93c2ad2` |
| 第 1 层 7/8 | 13 配置 `entry_price=next_close`、`1M` 年化、`_ts_rank` 方向、岭回归标准化 | `93c2ad2` / `6ddf3cf` |
| 第 1 层 9 | `ls_t` 改净收益 HAC t、有效观测数用 T、gate rebalance 对齐、DSR clamp fail-closed、cross-trial σ(SR)、BH family 补历史试验 | 本轮 `tests/test_layer1_9_regression.py` 10 项 |
| 第 2 层 10 | A1 切断每代 OOS fitness；A2 静态负位移/bfill + 动态因果探针 + 注册期算子探针；A3 默认拒绝从评估窗口学方向；A4 budget union + corrupt fail-closed + 批量 BH；A5 拒绝常数/爆炸因子、`_safe_divide` 零分母改 NaN | 本轮 `tests/test_layer2_leaks.py` 19 项 |
| 第 3 层 11 | X-01/02/03/04/06/07、E-02/04/06、K-04/06/07/08、A-03/04/05/06/08、S-01/02、T-01/04、D-08、G-05、X-05 | 本轮 `tests/test_layer3_reproducibility.py` 38 项 |

**全量回归：598 passed / 1 skipped / 75 subtests passed（126s）**；`compileall`、
CI YAML、docker-compose YAML 均通过。

---

## 1. 结论摘要

**好消息**：结构性泄漏链已被切断。搜索过程不再读验证期、负 `shift`/后向填充被静态+动态双重拦截、
方向不再从评估窗口现场推断、BH-FDR 的 family 不再是 1、常数与爆炸因子在评估入口被拒。
现在**如果跑一次新实验**，它的 gate 结论在方法论上第一次具备「可以被挑战」的资格。

**坏消息，且是本轮最重要的判断**：

> **仓库里现存的 84 份 factor_db 结论仍然是旧的、不可作为可交易证据。**
> 实测：`factor_db/metadata/*.json` 84 份、`values/*.parquet` 84 份，
> **`p_adj == p_value` 84/84**（说明入库时的 BH family 仍是 1）、
> **84/84 生命周期全为 `DISCOVERED_UNVERIFIED`**。
> 这些 metrics 是 `reaudit --apply` 在逐因子闸门下覆盖写回的，修复后的批量 BH
> **尚未对库重跑**；而 `entry_price` 已从 `current_close` 统一改成 `next_close`（T-03），
> 标签口径整体平移一根 bar，历史 IC/RankIC 与新口径**不可比**。
> 唯一基线在 `metadata_pre_reaudit/`。

**第二个硬约束**：`user_workspace/.holdout_ledger.json` 目前只有 1 条 reveal，
但区间是 **2025-07-01 .. 2026-11-13**（~500 天）。滚动预注册从 **2026-11-14** 起算，
`python -m core.experiment.windows` 实测 **0/30 bars、ETA 2026-12-13**。
也就是说：**在 2026-12-13 之前，任何「严格正向验证」都拿不到足够的新 bar**，
这不是算术错误，而是「一次性 holdout 已烧掉几乎全部历史」的经济代价。
仓库里任何声称「已通过严格 OOS」的既有产物，都需要重新审读其窗口是否真的在 burned 区间之外。

**第三个硬约束**：Web 回测 `web/src/**` 中 `orientation_db_root` / `holdout_mode` /
`allow_holdout_reuse` 的出现次数实测为 **0**。因此 Web 触发的回测恒为
`purpose="explore"`、`strict_oos=false`，界面上的收益**不能**被读作严格 OOS 结果。

---

## 2. 数据环节

**现状（实测）**：`freshness_report('data')` →
`files=81, assets_1d=20, streams=60, stale=1, latest=2026-11-13,
stale_symbols=['BTC/USDT:USDT'], stale_by_timeframe={'5m': 1}, unreadable.count=0`。
13 份配置全部 `target.entry_price=next_close`；其中
`configNN_temporal_10min_template.json` 的 `horizon_bars=5`，其余 12 份为 1。

| 级别 | 问题 | 证据 |
|---|---|---|
| 【已确认 bug】 | `data_quality.strict` **0/13 配置声明**，全部走默认 `False`。审查报告 §「C5」提到的「配置引用无数据周期」尚未收口：面板质量门默认不拦，坏 OHLC / 断层 / 未收盘 bar 仍可进入挖掘 | 本次实测 0/13；`core/data_feed/panel_validator.py` 由 `quality_cfg=work.get("data_quality")` 传入 |
| 【已确认 bug】 | `loader.py:288` `data.fillna(method='bfill')` 无条件后向填充。对含 `returns` / 指标列的表是**直接前视偏差**；`log/ma_ratio/price_position/volume_ratio` 的除零 ±inf 也不清理 | `core/data_feed/loader.py:288`；A5 的因子级闸门只能拦结果，拦不住输入污染 |
| 【已确认 bug】 | 1 个 5m 文件 stale（BTC/USDT:USDT）；`update_data.py` 的验证已按 T-04 过滤 stream，但 stale 文件仍在库 | freshness summary |
| 【方法论风险】 | `h>1`（唯一一份 `horizon_bars=5`）的重叠收益在 HAC/DSR 里仍按独立观测处理；`targets.py` 的 forward return 定义本身正确，问题在下游把 T 个重叠标签当 T 个独立样本 | `configNN_temporal_10min_template.json` + `_effective_obs()` 现按时间观测数 T 计数，但未对重叠做 Newey-West 之外的额外折减 |
| 【方法论风险】 | `build_funding()` 的 `rate` 文档是「Daily perpetual funding rate」，却按**每根 bar**铺成常数网格；1h/5m 回测会把日费率重复收 24/288 次。`use_real_funding=True` 走真实 8h 序列可缓解，但默认路径仍是常数 | `core/backtest/backtest_engine.py:456-487`；`--funding` 默认 0.0001 |
| 【需确认意图】 | `naming.safe_symbol` 把 `/`、`:` 都压成 `_` 不可逆；`parse_data_filename` 的 futures 后缀校验会误拒币本位交割 `BTC/USD:BTC` | 报告 §6 已登记，需作者确认命名契约 |

**判断**：数据链路的「假成功/死循环/分页缺 bar」已修好，但**输入侧的因果卫生还没做完**
（bfill、未收盘 bar、质量门默认关闭）。在 bfill 修掉之前，A2 的因果探针只能保证
「因子计算本身 point-in-time」，不能保证「喂给因子计算的数据本身 point-in-time」。

---

## 3. 搜索环节

| 级别 | 问题 | 证据 |
|---|---|---|
| 【已确认 bug】 | RL 的 REINFORCE advantage 用「含自身、逐步累积的运行均值」`np.mean(rewards)`，第一个样本贡献恒为 0，梯度系统性有偏；`self.baseline` 赋值后从未读取 | `core/miner/paradigms/rl_miner.py:172`、`:187` |
| 【已确认 bug】 | LLM 整批失败静默降级为 `df['close'].pct_change(1)` / `df['volume']*df['close']` 两个 mock 因子并正常入库 | `core/miner/paradigms/llm_miner.py:127-128` |
| 【已确认 bug】 | `_safe_pearson` 在 <10 点或 denom=0 时返回 `None`，`max(best, abs(c))` 跳过 → 常数/短样本因子 `mic=0` 一律放行；满池替换路径不查冗余 | `core/miner/pool.py:60-70`、`:136-217` |
| 【已确认 bug】 | 未知 terminal 静默返回全 0 面板（A5 常数因子的**输入侧触发点**）；AST 节点多余 key 无条件 `**params` 触发 TypeError | `core/miner/operator_runtime.py:600-622` |
| 【已确认 bug】 | `_ts_decay_linear` 的 NaN 窗口分母用满段权重、非满窗口取尾部大权重段，衰减形状不可比 | `core/miner/operator_runtime.py:106-117` |
| 【已确认 bug】 | GP 交叉子代 `parent_ids` 写成 `List[List[str]]` 且装祖辈列表；`_mutate` 的 else 分支对终端常量 `tgt.update(...)` 抛 ValueError | `gp_miner.py`（报告 G-03/G-04） |
| 【已确认 bug】 | NN early-stopping 记录**更新前** W 的 loss 却保存**更新后** 的 `best_parameters`，且无 validation split | `user_workspace/custom_miners/my_custom_nn.py:267-309` |
| 【已确认 bug】 | `ParallelEvaluator.evaluate_final()` 是**死代码**：全仓库仅定义、测试调用，生产路径一次都没用。真实一次性 OOS 走的是 `director._attach_final_oos_metrics()` → `evaluator._compute_oos_metrics()` 私有方法 | `grep -rn evaluate_final core/` → 仅 `evaluator.py:148,182` + 测试 |
| 【方法论风险】 | 名人堂排序缺 `fitness_score` 时默认 0.0，自定义 hook 返回 dict 的候选可能排到真实低分因子之上，又被 `_finalize` 过滤，名人堂与可组合集合互相矛盾 | `base.py`（报告 G-06） |
| 【方法论风险】 | LLM 挖矿的 `llm_nondeterministic` 只在 manifest 里记一笔；非确定性的 API 结果没有 prompt/response 快照，seed 无法复现该范式 | `manifest.build_manifest` 的 notes |

**判断**：搜索的**治理层**（不读 OOS、不允许未来函数、必须给 seed、候选必须非退化）已经到位；
但**算子质量与范式实现**仍是 2024 年原型水平。其中 RL 的 advantage 与 LLM 的静默 mock
会直接生产「看起来能跑、实际是噪声」的候选，优先级高于其余算子细节。

`evaluate_final()` 虽是死代码，但当前一次性 OOS 路径本身是正确的
（`director.run()` 只在 `miner.mine()` 返回后调一次 `_save_excellent_factors`，
实测 `grep -rn _save_excellent_factors core/` 只有 `director.py:107` 一个调用点，
即每个 run 恰好一次）。建议把 `evaluate_final` 接进 director 或删除，避免两套语义漂移。

---

## 4. 验证环节

| 级别 | 问题 | 证据 |
|---|---|---|
| 【已确认 bug】 | `pvalue_from_t()` 把 Newey-West HAC t 当 N(0,1) 求双侧 p；小样本/多滞后下 HAC t 尾部更厚 → raw p 系统性偏小，BH/Holm 之后仍偏松 | `core/evaluation/significance.py:378-385` |
| 【已确认 bug】 | `empirical_p = mean(null >= value)` 无 `+1` 连续性校正，`n_perm=100` 时可返回 0.0，配合 `empirical_p_max` 闸门产生虚假确定 | `significance.py:217-232` |
| 【已确认 bug】 | `gate_calibration.py` 的 `p_value` 与 `null_tstats` 从同一份 in 样本置换生成，却当两道独立闸门同时要求通过；随后用被压低的检出率「证明」闸门功效不足，构成循环论证；`DEFAULT_TRIALS=182` 与真实 `n_trials=348` 错配 | 报告 X-08 |
| 【已确认 bug】 | `inspector/metrics.py` 的 Lag IC Decay 四条分支口径互异（累计前瞻 / 过去收益前移 / 单期平移），`reporter.py:66` 统一打印成 "RankIC across Lags"；cross_asset 分支丢弃 `target_spec` | `reporter.py:66` |
| 【已确认 bug】 | `inspector/reporter.py` 把失败/空序列渲染成全 0 的「干净」表格，空 IC 序列的 `pos_ratio` 被硬凑为 100%/0% | 报告 S-05 |
| 【已确认 bug】 | 84 份库内 metrics 的 `p_adj == p_value`（84/84），批量 BH 修复**未对库重跑** | 实测，见 §1 |
| 【方法论风险】 | holdout ledger 只有 1 条 reveal 却覆盖 2025-07-01..2026-11-13；滚动窗口 0/30 bars、ETA 2026-12-13。在此之前任何「严格正向验证」结论都不存在 | 实测 + `python -m core.experiment.windows` |
| 【需确认意图】 | 回测 CLI 在产出之前就 `ledger.reveal()`；只要传 `--orientation-db-root` 就置 `purpose=final`，即使它等于 `db_root` | 报告 K-05；本轮刻意未改 |

**判断**：验证环节的**机制**（一次性 ledger、BH family、DSR、因果探针、manifest/resume 校验）
已基本完整，剩下的是**统计近似的精度**（HAC t 的参考分布、置换 p 的校正）
和**历史产物的重算**。这两件事必须在新实验之前做完，否则新实验会带着偏松的 p 值通过闸门。

---

## 5. 组合因子环节

| 级别 | 问题 | 证据 |
|---|---|---|
| 【已确认 bug】 | `LinearCombinedFactor.from_dict()` 不读 `member_factor_ids`，且只还原含 `"op"` 的 dict → 组合因子回读无法重算，落库后只能靠 `resolve_signal_weights` 猜成员 | `core/miner/expressions.py:257-264` |
| 【已确认 bug】 | 落库分流 5 个 `isinstance` 分支无 `else`，自定义 expression 静默不落库但 `latest_factor_ids` 已 append；`FactorMetadata` 的 `logic_reference`/`parent_ids`/`data_lineage` 全不传 | 报告 M-07 |
| 【已确认 bug】 | `_admit_with_replacement` 满池路径不查冗余；`evaluate_admission` 不校验单因子 RankIC；mutual-IC 用「各自 IC 序列的 Pearson」而 docstring 声称是 AlphaGen 式因子值截面相关 | `pool.py:136-217`；【需确认意图】该定义 |
| 【方法论风险】 | `walk_forward.py:_fit_ridge` 已有 `_standardise()` 但**没有不惩罚截距**，与 `fit_scale_aware_ridge()` 的口径仍不一致；同一批权重在 gate、walk-forward、组合层三处的经济含义可能不同 | `walk_forward.py:34`；报告 §17 已登记 |
| 【方法论风险】 | 组合权重的方向来自拟合权重，但 `resolve_signal_weights()` 展开后 `combination_members` 会被跳过二次定向（A3 已修）；若组合成员本身在 in-sample 库里没有 snapshot，`load_factor_signals` 仍会排除该因子且**不报警** | `backtest_engine.load_factor_signals` |

**判断**：M-02 的岭回归标准化已经解决最致命的量纲问题（实测小尺度列不再被压掉 97%）。
但**组合因子的持久化契约**（成员 id、权重、方向、来源）仍是断点：
一个因子可以被存进去、被回测展开，却无法从 `from_dict` 完整还原。

---

## 6. 回测环节

| 级别 | 问题 | 证据 |
|---|---|---|
| 【已确认 bug】 | Web 端完全没有 `orientation_db_root` / `holdout_mode` / `allow_holdout_reuse`，`grep` 命中 0 → Web 回测恒 `purpose="explore"`、`strict_oos=false`，UI 收益易被误读为严格 OOS | `web/src/**` 实测 0 命中；`api/main.py:886` |
| 【已确认 bug】 | 常数资金费按每 bar 收日费率（见 §2）；表单写「资金费/日」但后端按 bar 计 | `backtest_engine.py:456`；报告 W-08 |
| 【已确认 bug】 | `run_backtest` 的 `resolve_combinations` 本轮已接通 CLI 的 `--no-expand-combinations`，但关闭时 `weight_list=[1.0]*len` **完全丢弃显式 `--weights`** | `backtest_engine.py:242-244` |
| 【已确认 bug】 | `api/backtest_runs.py` 未传 `holdout_mode` / `allow_holdout_reuse` 给 `run_backtest` 的 guard 之外的任何一层；`_execute` 的 `purpose` 判据只看 `orientation_db_root` | K-05 同根因 |
| 【方法论风险】 | `summary.csv` 的成本口径 `total_costs` 单位为 USD、双边计费正确，但 `daily_funding_rate` 字段名暗示按日，实际按 bar | `backtest_engine.py:688` |
| 【需确认意图】 | `--allow-holdout-reuse` 的语义是「仅重算已发表数字」，但界面/CLI 没有强制关联已发表结果，审计行只有 note | `holdout_guard.py` |

**判断**：回测的**撮合与方向**已经可信（NEXT_BAR 滞后 1 根、方向来自 in-sample 或预注册、
双边计费、最大回撤用全曲线）。剩余风险集中在**成本口径**与**Web 的 OOS 语义暴露**。

---

## 7. 分模块复审（本轮修复 → 剩余）

| 模块 | 本轮已修 | 剩余最高优先级 |
|---|---|---|
| `core/data_feed/` | 分页、假成功、健康检查、`get_data_info`、update_data stream 过滤 | `loader.bfill`（D-07）、未收盘 K 线（D-05）、tz 双次转换（D-06）、stream 非原子写（D-08 余项） |
| `core/miner/` + `paradigms/` | 岭回归标准化、实例 RNG、注册期因果探针、`_safe_divide`、负 `_ts_ref`、`_ts_rank` | G-01 REINFORCE、G-02 LLM mock、M-03 admission、M-04 decay、M-05 NN early stop、M-06 未知 terminal |
| `core/evaluation/` | OOS 隔离、因果探针、常数/爆炸闸门、净收益 t、DSR/BH、guard 单侧、ledger 跨进程锁 | E-03 HAC t 参考分布、empirical_p +1、`evaluate_final` 死代码 |
| `core/experiment/` | resume verify、seed_applied、cwd 独立、registry 生命周期、guard 幂等、0 folds 失败、bar-space 日期、成本一致性告警 | X-08 循环论证、walk_forward 截距口径、budget 跨进程锁（X-05 余项） |
| `core/backtest/` + `commands/` + `cli.py` | 方向严格化、`rank(average)`、组合开关、权重校验、CLI 优先级、单侧窗口、run_experiment 覆盖语义 | K-05 reveal 时机、A-06 剩余路径校验、`--config` 与 `orchestrator` 的成本单源 |
| `api/` | WS 广播清理、下载进度线程桥、幽灵任务回收、并发上限、路径校验、TaskStore 归一化 | A-02 async 路由内同步 IO（整站 DoS）、A-07 日志串台、`get_data_coverage` 隐式 null |
| `web/src/` | 端点统一、hook cleanup、指标单位 | W-04 轮询竞态、W-05 不检查 `response.ok`、W-06 参数白名单、W-07 状态样式、W-08 OOS 开关 |
| `scripts/` / 配置 / 部署 | 结论插值、非零退出、CI smoke、T-01/T-04 | T-06 Docker 未 COPY user_workspace、T-07 依赖版本冲突、T-08 其余 19 个脚本零 smoke |
| `tests/` | 新增 layer0/1/2/3 共 76 项断言 | 无端到端「真实挖掘→闸门→回测」集成测试；`test_cli_smoke` 只跑 1 代小样本 |

---

## 8. 三级结论清单（本轮新增，按严重度）

**【已确认 bug · 会改变结论真伪】**
1. 84/84 库内 `p_adj == p_value`，批量 BH 未对库重跑 + `entry_price` 口径已变（§1）。
2. `evaluate_final()` 死代码，两套一次性 OOS 语义并存（§3）。
3. `pvalue_from_t` 用正态尾近似 HAC t；`empirical_p` 无 +1 校正（§4）。
4. `loader.py:288` 无条件 `bfill`（§2）。
5. RL advantage 运行均值有偏；LLM 整批失败静默 mock（§3）。
6. `LinearCombinedFactor.from_dict` 丢失成员 id（§5）。
7. Web 无 OOS 开关，回测恒 `explore`（§6）。
8. 资金费按 bar 收日费率；`resolve_combinations=False` 丢弃显式权重（§6）。

**【方法论风险 · 需口径决策】**
9. `h>1` 重叠收益按独立观测处理（§2）。
10. holdout 已烧 500 天，滚动窗口 0/30 bars、ETA 2026-12-13（§1/§4）。
11. walk-forward 与 gate 的岭回归截距口径不一致（§5）。
12. `gate_calibration` 同一样本生成 p 值与 null，循环论证（§4）。
13. 0/13 配置声明 `data_quality.strict`（§2）。

**【需确认意图】**
14. mutual-IC 的定义（IC 序列相关 vs 因子值截面相关）。
15. K-05 reveal 时机与 `--orientation-db-root == --db-root` 的语义。
16. `naming.safe_symbol` 不可逆压缩 / 币本位交割后缀校验。
17. W-08 Web 是否只做探索式回测（若是，应在 UI 显式标注「非 OOS」）。

---

## 9. 第 4 层建议（下一轮最小修复集）

1. **口径收口**：`pvalue_from_t` 改用 `scipy.stats.t` 或按 HAC 有效自由度近似；
   `empirical_p` 加 `(count+1)/(n+1)`；删除或接通 `evaluate_final`。
2. **数据因果卫生**：删掉 `loader.py:288` 的 `bfill`，改 ffill/limit 或显式 NaN；
   下载器裁掉未收盘最后一根；13 份配置显式声明 `data_quality.strict=true`。
3. **范式正确性**：RL advantage 改 batch mean（先收集 rewards 再算梯度）；
   LLM 失败改为 `raise` / 返回空候选，禁止 mock 入库；`_safe_pearson` 返回 `None` 即拒绝。
4. **组合持久化**：`from_dict` 读写 `member_factor_ids`，落库补 `logic_reference`/`parent_ids`/`data_lineage`；
   补「从 factor_db 回读组合因子 → compute → 与原始值逐元素相等」的回归测试。
5. **Web 契约**：在回测表单加 `orientation_db_root` / `holdout_mode` / `allow_holdout_reuse`，
   并在结果卡上把「严格 OOS」与「探索式 in-sample」显式区分；`response.ok` 与输入正则白名单。
6. **重算基线**：用修复后的批量 BH + 新 `next_close` 标签对 `metadata_pre_reaudit/`
   做一次全库重算，产出新的 `factor_db` 结论；**不要把旧 84 份 metrics 当作证据**。
7. **端到端集成测试**：一次真实 20 资产 × 1d 的短跑（mine→gate→wf→combine→OOS→backtest），
   断言 `val_read_count==1`、`n_trials` 单调、`p_adj != p_value`（family>1）、
   回测 `orientation_sources` 全部来自 in-sample。

---

## 10. 现在可以/不可以相信什么

| 结论 | 状态 |
|---|---|
| 「框架内置 rolling/shift、forward return、NEXT_BAR 撮合、双边成本、HAC/BH 公式」 | ✅ 可信（复核正确） |
| 「新跑一次实验，搜索过程不接触验证期、方向不来自评估窗口、因子非退化」 | ✅ 机制可信（A1–A5 + 回归测试） |
| 「新跑一次实验，p 值精度足够用于发表级判定」 | ⚠️ 仍偏松（E-03 / empirical_p） |
| 「factor_db 里 84 个因子显著/不显著」 | ❌ 不可信，须按 §9.6 重算 |
| 「Web 上看到的回测收益是严格 OOS」 | ❌ 不成立，Web 恒为 explore |
| 「P0.5 正向验证已完成」 | ❌ 不成立，0/30 bars，ETA 2026-12-13 |
