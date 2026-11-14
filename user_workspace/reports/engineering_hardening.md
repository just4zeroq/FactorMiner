# 工程加固与组合回测闭环

> 完成时间：2026-11-14
> 范围：第一批（回测闭环）+ 第二批（可信度加固）
> 测试：238 passed（新增 56 项，排除环境缺 `httpx2` 的 `test_factor_analytics.py`）

## 1. 为什么现在做这些

P0 / P0.5 / P1 三轮验证得到一个稳定结论：**单因子在 20 币、日频、18bp 往返成本下几乎不可能同时过成本闸门与 DSR**。全库 84 个因子重审 0 通过，不是闸门坏了，而是"单因子 + 1 日目标"这条路本身没有边际。

出路只有组合、多周期或扩池。而组合这条路此前是断的：

- `AlphaPool` 在挖掘末尾拟合出的权重，写进 `metadata.logic_reference` 后就没人读了；
- 回测适配器 `load_factor_signals` 对多因子做的是 `groupby(...).mean()`，等于把学到的权重抹平成等权；
- 回测只有 CLI，`api/main.py` 里没有任何 backtest 端点，Web 只有 4 个页面。

也就是说：**权重学出来了，但进不了净值曲线；净值曲线算得出来，但看不见。** 第一批就是补这一段。

## 2. 第一批：把"因子 → 组合 → 净值"接通

### 2.1 AlphaPool 权重贯通

- `FactorMinerDirector._save_excellent_factors` 改为**整批预分配 factor_id**，合成因子落库时写入 `member_factor_ids`，把 sub-AST 与库中因子 id 对上。
- `backtest_engine.resolve_signal_weights()` 递归展开 `type == "linear_combination"` 的因子，成员权重按 `parent_w × member_w` 相乘，最多 4 层；缺 `member_factor_ids` 的旧记录退回"不透明单因子"并告警。
- 信号合成从等权 `mean()` 改为 `Σ(w_i · oriented_rank_i) / Σ|w_i|`。除以 `Σ|w|` 而不是 `Σw`，避免正负权重相消时分母趋零放大噪声。
- CLI 新增 `--weights`（支持 `id=w,id=w` 与位置列表）与 `--no-expand-combinations`；`--factors` 缺省时自动发现库里所有带快照的因子（原来硬编码的 6 个 INSPECTED 已被重审降级，硬编码等于跑一套死数据）。

回归测试 `tests/test_backtest_weights.py`（7 项）覆盖：展开、权重缩放、缺成员 id 的降级、位置权重长度校验、零权重等价于单因子、负权重等价于反向因子。

### 2.2 回测 API 与 Web 页面

- `api/backtest_runs.py`：线程池执行 + 文件系统为准的状态机。每次运行落 `status.json` / `summary.json` / `equity.csv` / `trades.csv`，**服务重启后历史运行仍可查**（内存 dict 做不到这点）。
- 端点：`POST /api/backtest`、`GET /api/backtest/runs`、`GET /api/backtest/runs/{run_id}`。净值曲线按 1500 点降采样后返回。
- 前端 `web/src/pages/Backtest.tsx`：左侧因子多选（只列有快照的）、右侧参数面板（区间/手续费/滑点/资金费/调仓/多空腿数/初始资金）、三种权重模式（等权、按 |RankIC|、自定义）、ECharts 双轴净值 + 回撤、8 项指标卡、生效权重表（可跳 Inspector）、历史运行列表。三语（zh/en/de）已补齐。

端到端实测（3 个因子、20 币、2024 全年、3 多 3 空、5 日调仓）：

| 指标 | 值 |
|---|---|
| 区间 | 2024-01-01 .. 2024-12-31 |
| 标的数 | 20 |
| 累计收益 | -14.97% |
| 最大回撤 | 35.20% |
| Sharpe | -0.30 |
| 成交笔数 | 285 |
| 总成本 | 46,313 |

这是随机挑选的三个未验证因子，结果本身没有研究价值，**验证的是链路**：选择 → 排队 → 展开权重 → 事件驱动回测 → 净值落盘 → 前端读取，全通。

### 2.3 任务持久化与取消

此前 `TaskManager.tasks = {}` 是纯内存 dict：重启即丢，长任务只能杀进程，而 TODO 里写的"启停控制（完成）"只在"启"这一半成立。

- `api/task_store.py`：镜像到 `.workbuddy/tasks.json`。写入节流 5 秒（progress tick 每秒多次，不能每次都落盘），状态迁移强制 flush。加载时把上次的 `running` 降级为 `interrupted`，避免幽灵任务。日志裁剪到 60 条、任务保留最新 60 个。
- `POST /api/tasks/{id}/cancel` 置 `cancel_requested`；`progress_callback` 在每个 epoch 边界 `check_cancelled()` 抛 `MiningCancelled`（epoch 是唯一安全的停止点，不会留下半写因子）。前端 Launchpad 表格与详情弹窗都加了停止按钮，并按 `停止中…` 反馈状态。

12 项测试覆盖 flush/reload 往返、running→interrupted 降级、终态保持、日志裁剪、损坏文件容错、节流语义、取消幂等。

## 3. 第二批：让"可信"可度量

### 3.1 相关性软去重

`DiversityFilter` 原来只有源码 MD5。`rank(close)` 与 `zscore(close)` 的 MD5 完全不同，但截面排序一模一样——它们是同一个假设，却各占一个试验名额，抬高 DSR 分母。

实现：候选先过 MD5，再算**面值签名**（DataFrame 逐期截面 rank 后展平，Series 直接展平），与 archive 里最多 40 个签名比 |Pearson|，超过 `dedup_value_threshold`（默认 0.99）即拦截。反向信号 |corr|=1 同样算冗余，因为一个 `-1` 权重就能替代。

真实 20 币、2 代 GP 实测：

```
Diversity filter summary: threshold=0.990, archive=40, rejected_by_value=46, max_observed_corr=1.000
```

**46 个候选被拦（约 30%），且存在 corr=1.000 的完全重复。** 换句话说，此前每三轮里约有一轮的"新发现"是同一个信号换了个写法。代价是每代耗时从 26s 涨到约 68s（要多算一次 `compute`），换来的是更干净的多重检验分母。

一个实现细节：`filter_redundant` 收到的是 `self.data`（`RealDataClient` 对象），而 `candidate.compute()` 需要的是面板。第一版直接在 client 上 compute，全部抛异常、signature 全为 `None`，去重静默失效却不报错——`archive=0` 才暴露出来。现在由 `_factor_panel()` 统一取面板。

`dedup_value_threshold=0` 可关闭。

### 3.2 评估超时

`ParallelEvaluator` 原来用 `executor.map`（无超时、`with` 退出会 join 所有线程）。一个卡死因子能拖住整轮，而挖掘是无人值守跑的。

现在：`evaluation.timeout_seconds`（默认 120s/候选）× 批次大小 = 整批预算；`as_completed(timeout=budget)` 收集完成的，未完成的按 `status=timeout` 补齐并保持候选顺序；`shutdown(wait=False, cancel_futures=True)` 不阻塞。`timeout_seconds=0` 恢复无限等待。

### 3.3 挖掘前数据质量闸门

`DataHealthChecker` 只挂在下载链路上。**已在磁盘上的文件不会再被检查**——31 根 08:00 重复的 BTC 日线就是这么一路混进 strict holdout 的。事后才发现，说明检查点放错了位置。

`core/data_feed/panel_validator.py` 在首次 `get_data()` 时校验：

| 检查 | 级别 |
|---|---|
| 面板为 None / 无行 / 索引非升序 | critical |
| 重复时间戳（超过 `max_duplicate_ratio`） | critical |
| 收益率全 NaN | critical |
| K 线缺口（超过 `max_gap_ratio`，默认 5%） | warning |
| 覆盖率低于 `min_coverage`（默认 90%） | warning |
| 全空资产 / 常数资产 | warning |
| \|单期收益\| > `max_abs_return`（默认 100%） | warning |

`data_quality.strict=true` 时 critical 直接抛 `DataQualityError` 拒跑；默认只告警，避免把一次数据小瑕疵变成停机。真实 20 币面板实测：

```
Data quality gate passed: {fields: 5, bars: 366, duplicate_timestamps: 0,
  median_interval: 1 days, expected_bars: 366, missing_bars: 0, coverage: 1.0,
  return_obs: 7300, extreme_returns: 0}
```

### 3.4 假设预算上报

DSR 的基准是 `E[max SR] ~ sqrt(2 ln n_trials) / sqrt(n_obs)`——**它不是因子的属性，是搜索预算的属性**。787 次试验 × 610 观测对应年化 2.35 的门槛，这意味着一个真实年化 Sharpe 1.5 的策略无论 IC 多干净都过不了。这个事实此前只存在于报告里，代码从不打印。

现在每次挖掘结束打印：

```
Hypothesis budget: 99 cumulative test(s) across 1 run(s); DSR bar = 0.159 per-period SR (2.52 annualised)
```

99 tests 就对应 2.52 年化（因为 366 个观测比 610 少，`1/sqrt(n_obs)` 更大）。预算使用率 ≥80% 时告警并明确给出替代方案（拉长目标周期或收缩搜索空间）。`GET /api/gates/budget` 可查询。

## 4. 验证与清理

- 全量测试：**238 passed**，新增 56 项（权重 7、去重 11、超时 4、数据校验 13、任务存储 12、假设预算 9）。
- 真实数据验证：20 币 2024 全年、`MyCustomGP` 2 代，质量闸门通过、软去重生效、预算上报正确、37 个因子正常落库。
- **验证产生的副作用已清理**：37 个测试因子移到 `factor_db/_scratch_20261114/`（不参与 `list_metadata`），`.fdr_ledger.jsonl` 与 `.experiment_trace.jsonl` 中对应记录已剔除（原文件备份为 `.bak`）。因子库回到 84 个，账本回到 0 tests / 4 trials，研究预算未被测试污染。

## 5. 遗留

1. **P0.5 正向验证仍缺数据**：需要 2026-12 之后的新区间，2025-07→2026-11 已消耗。
2. **软去重只在轮内生效**，没有和历史上已入库因子做正交检查。跨轮去重目前靠 `archive_hashes`（MD5）与 `AlphaPool` 的 mutual-IC。要对全库做面值正交，需要重算 84 个因子的面板，成本较高，暂缓。
3. **回测页没有归因层**：无行业/市场中性化、无因子暴露分解、无 Brinson。
4. **第三批未做**：CI、容器化、数据自动更新调度。
