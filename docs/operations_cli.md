# 生产 CLI 运维手册

生产闭环由七条命令组成：**refresh → doctor → experiment**（产出）→
**factors**（诊断）→ **promote**（发布）→ **holdout plan/reveal**（一次性确认）。
它们共用同一份配置（`user_workspace/configs/*.json`），彼此只通过数据、run 库与账本解耦。

```bash
# 一次性跑完（推荐；定时任务就调这一条）
venv/Scripts/python.exe -m core.cli pipeline --config user_workspace/configs/configExperiment.json

# 分开跑
venv/Scripts/python.exe -m core.cli refresh  --config .../configExperiment.json
venv/Scripts/python.exe -m core.cli doctor   --config .../configExperiment.json
venv/Scripts/python.exe -m core.cli experiment --config .../configExperiment.json
```

## 0. 数据根只有一个（`data_feeds.data_root`）

`refresh / doctor / experiment / mine` 都从 **`data_feeds.data_root`** 解析数据目录，
相对路径锚定在进程 cwd（项目根），缺省 `data`。命令行 `--data-root` 会覆盖它，
并**写回内存里的配置**，因此：

- 体检的目录 == 挖掘读的目录 == 刷新写的目录，三者不可能再各说各话；
- `pipeline --data-root /mnt/market` 会把同一个根一路传到 experiment，
  不会出现"doctor 检查了 A、实际挖的是 B"。
- 若 `data_root` 指向的不是 `cwd/data`，`RealDataClient` **不会**在缺文件时偷偷下载
  （下载器只会写 `cwd/data`），而是直接报错让你先 `refresh --data-root`。

## 1. `refresh` — 把本地数据补到 T-1

- OHLCV：复用 `scripts/update_data.py` 的规划/下载/裁剪逻辑（同一份实现，不另写一套）。
  它按每个标的**最后一根已收盘 bar** 之后开始补，并裁掉未收盘的尾根。
- crypto streams：配置里声明了 `data_feeds.crypto_streams` 时，同时补
  `funding_rate / open_interest / basis`（从各自文件末日增量抓取）。
  `--skip-streams` 可跳过。
- `--dry-run` 只打印计划。
- 退出码：`0` 全部最新；`1` 有标的没补上（脚本/流各算一份）。

> 为什么必须先刷新：挖掘面板一旦落后，后面所有统计都建立在过期数据上，
> 而"看起来跑完了"不会有任何报错。

## 2. `doctor` — 开跑前的体检

11 项检查，`FATAL` 阻断、`WARN` 只提示：

| 检查 | FATAL 条件 |
|---|---|
| `config` | 文件缺失/不可解析/`validate_mining_startup` 拒绝 |
| `universe` | 配置里的标的没有数据文件或文件不可读 |
| `coverage` | 某标的覆盖不到 `mine_period` / `test_period` 的边界 |
| `freshness` | 最新已收盘 bar 落后 T-1 超过 1 天（WARN） |
| `crypto_streams` | 声明了未知 stream（FATAL）；文件缺失（WARN） |
| `data_quality` | 未声明 `data_quality.strict`（WARN） |
| `holdout` | 账本不可读；生产环境下 mode≠strict（FATAL） |
| `holdout_window` | 预注册窗口未就绪（WARN，附 ETA） |
| `environment` / `storage` | 存储目录不可写 |

```bash
--json      # 机器可读，便于告警系统解析
--strict    # 把 WARN 也当失败（CI 用）
--miner GP  # 覆盖 config 的 paradigm（缺省与 experiment 一致：config → GP）
```

`experiment` 也支持 `--preflight`，在开跑前执行同样的检查并在 FATAL 时退出。

## 3. `mine` — 单范式挖掘（可复现）

```bash
venv/Scripts/python.exe -m core.cli mine --miner GP \
  --config user_workspace/configs/configExperiment.json \
  --seed 20260101 [--data-root data]
```

`--seed` 会把种子写进 `experiment.seed`，director 据此固定全局 RNG（GP/RL 采样、
LLM 离线兜底），所以同一条命令可以逐位复现。缺省无种子 = 每次不同（研究用）。
`mine` 只挖矿、不跑闸门与回测，产出写进 `factor_db`；要完整链路请用 `pipeline`。

> `scripts/run_mine.py` 已降级为**转发 shim**（打印 DEPRECATED 后调用 `pipeline`）。
> 旧 crontab 仍能跑，但请改指向 `core.cli pipeline`。

## 4. `pipeline` — 一条命令跑完

```
refresh → doctor（FATAL 则中止）→ experiment → 写 summary
```

- `--skip-refresh` / `--skip-doctor`：跳过某一步（离线调试用）。
- `--dry-run`：只做 refresh 计划 + doctor，不挖矿。
- `--data-root`：默认 **不传**，即沿用 `data_feeds.data_root`；显式传值才覆盖。
- `--stale-is-fatal`：refresh 失败或 `doctor` 的 `freshness` 为 WARN 时，
  在开跑前中止并返回 1（summary 里写 `stale`）。默认关闭——保留"用本地最新数据继续跑"的旧纪律。
- `--fail-if-no-promotion`：闸门没有晋升任何因子时返回 1 —— 给告警用；
  **默认关闭**，因为"0 晋升"在信号弱的阶段是正常结果，不是故障。
- `--set path=value`：点号路径覆盖配置（可重复），**在算 config hash 之前**生效，
  所以 manifest 记录的永远是实际跑的那份配置。
- `--wf-scope promoted|all`：walk-forward 默认只验证过了闸门的因子；0 晋升时该阶段
  记录为 `skipped` 而不是让整轮失败。
- `--stop-after-candidates N` / `--max-wallclock 秒`：在 epoch 边界停下并正常收尾
  （写报告、关 stage），不会留下半代种群。
- `--max-mem-mb`：单候选内存上限，超限标 `resource` 并跳过，不拖垮整轮。
- `--stop-after-promoted N`：sweep 模式下累计晋升达到 N 就不再调度后续 cell。
- `--webhook URL`：结束时把 summary JSON POST 过去（失败只告警，不改退出码）。
- 每次运行都会写 `user_workspace/reports/pipeline_<UTC时间戳>.json`：
  `stages[]{stage,exit_code,detail}`、`doctor` 全量结果、`run_id`、`candidates`、
  `promoted`、`exit_code`。告警系统读这个文件比抓 stdout 稳。

退出码：`0` 正常；`1` doctor FATAL 或 experiment 失败；`2` 用法错误。
**refresh 单独失败不会中止挖矿**（用本地已有的数据继续跑），但 summary 里会记录；
需要硬停时加 `--stale-is-fatal`。

## 5. `factors` — 为什么这轮 0 晋升

实验把每个候选的闸门判定写在 `<run>/factor_db/metadata/*.json` 的
`metrics.gate_checks` / `metrics.gate_pass`。命令行直接读它，不重算：

```bash
venv/Scripts/python.exe -m core.cli factors --run-id run_20261115T052211Z_61ffe5
venv/Scripts/python.exe -m core.cli factors --run-id <id> --show-rejects --json
```

输出：候选数 / 通过数、各生命周期状态、**按闸门统计的失败次数**（例如
`dsr_pass=71, empirical_null_pass=66`）、**Stage-A 漏斗**（多少候选进了置换零分布、
各被哪条粗筛拦下）、按 fitness 排序的明细，并带一列 **`expression`**（AST 直接渲染，
不再需要翻 metadata）：

```
factor_id      status                  gate    hac_t    p_adj      dsr    emp_p      fit  expression
fac_86485292   DISCOVERED_UNVERIFIED   FAIL    6.854 2.864e-10 8.9e-09  0.01639    2.401  log1p_abs(ts_ema(ts_corr(add(close, high...
```

- `--expr-width N`：表达式列宽（0 = 不截断）。
- `--json`：每个因子的 HAC t / p_adj / DSR / empirical_p / 失败项 / 完整表达式。
- `--compare runA,runB`：跨 run 并排比较，输出 `newly_passing / newly_failing /
  appeared / disappeared`（按 logic_hash 对齐，改名但等价的表达式仍能认出来）。

## 6. `promote` — 把通过的因子发布到生产库

实验刻意写进 `<run>/factor_db`，避免坏 run 污染共享库；因此通过的因子还不在
`factor_db/` 里。发布命令把 **metadata + values 快照 + 引用的源码/权重/模型**
作为一组复制，并追加 `user_workspace/reports/promotions.jsonl` 审计：

```bash
venv/Scripts/python.exe -m core.cli promote --run-id <id> --factor-id fac_xxxx \
  --db-root factor_db --dry-run          # 先看会复制哪些文件
venv/Scripts/python.exe -m core.cli promote --run-id <id> --factor-id fac_xxxx
venv/Scripts/python.exe -m core.cli promote --run-id <id> --all-inspected --json
```

`--all-inspected` 批量发布该 run 里所有 `INSPECTED + gate_pass` 的因子：
逐个执行同一套校验，**任一失败不影响其他**，最后汇总 `promoted / failed`；
全部失败时退出码 1，部分成功时 0（看 `--json` 的 `failed` 数组）。

安全规则：

- 必须 `metrics.gate_pass=true` **且** `lifecycle_status=INSPECTED`；
- 目标已存在同 id 一律拒绝（含 `--force` 时也不覆盖）；
- `--force` 只用于绕过闸门状态检查，会在 lineage 与审计里记 `forced=true`；
- `metadata.data_lineage.promotion` 记下 run_id / 源库 / 时间 / 闸门指标。

## 6b. `factor-db` — 生产库本身的管理面

发布/回测之间原来没有任何命令能回答「库里现在有什么」，这正是空库连续三轮无人察觉的原因。

```bash
venv/Scripts/python.exe -m core.cli factor-db list                       # 库里有什么
venv/Scripts/python.exe -m core.cli factor-db show fac_xxxx              # 元数据+快照统计
venv/Scripts/python.exe -m core.cli factor-db corr --threshold 0.7        # 冗余对
venv/Scripts/python.exe -m core.cli factor-db combine --method hrp --dry-run
venv/Scripts/python.exe -m core.cli factor-db export --out exports --format parquet
venv/Scripts/python.exe -m core.cli factor-db decay --window 90 --apply   # 衰减标记
venv/Scripts/python.exe -m core.cli factor-db rm fac_xxxx --dry-run       # 软删除预览
```

- `list`：总数 / `gate_pass` 数 / 有值快照数、状态分布、每行的 `expression` 与覆盖期、
  所属 `run_id`。
- `corr`：在**截面 rank** 上算 Spearman/Pearson，列出 `|ρ| ≥ threshold` 的冗余对。
  组合前不做这一步，等于把同一个信号加权 N 次。
- `combine --method equal|ic_weight|ridge|hrp`：拟合成员权重（单成员上限 `--max-weight`，
  不可行时按 `1/n` 放宽并说明），`--dry-run` 只给权重；否则写一条
  `type=linear_combination` 的因子 metadata + 加权快照，可直接 `backtest`。
- `decay`：用快照里的 `factor` vs `forward_return` 算最近 N 个观测的 trailing RankIC，
  `≤ --threshold` 标 `DECAYED`（`--apply` 才写回，`previous_lifecycle_status` 留痕）。
- `rm`：**只做软删除** —— 把 metadata/values/源码/权重移进库内 `_deleted_<日期>/`，
  必须显式 `--yes`；不提供硬删除。

## 6c. `experiment --dry-run` / `--sweep` / `--shard` — 规模化

```bash
# 跑之前先看会产多少候选、要多久、DSR 门槛在哪
venv/Scripts/python.exe -m core.cli experiment --config <cfg> --dry-run

# 声明一次多假设扫描，并按 shard 切给多个进程
venv/Scripts/python.exe -m core.cli experiment --config <cfg> \
  --sweep sweep.json --shard 1/4 --dry-run
venv/Scripts/python.exe -m core.cli experiment --config <cfg> --sweep sweep.json --shard 1/4
```

`sweep.json`：

```json
{
  "name": "h5",
  "grid": {"target.horizon_bars": [5, 20], "rebalance_every": [1, 5]},
  "seeds": [20261114],
  "set": ["fitness.turnover_max=0.15"]
}
```

- 笛卡尔积展开，每个 cell 独立 run 目录、独立 `trial_family`（`<family>:<dims>`），
  互不稀释 DSR 分母。
- `--shard i/n` 按 `position % n == i-1` 切片；每个分片是独立进程，共享同一份
  append-only 账本（已有跨进程文件锁）。
- `--dry-run` 会逐个 cell 打印候选数/耗时/DSR 门槛与告警，不启动挖掘。
- `--stop-after-promoted N`：累计晋升达到 N 就停掉后续 cell。

**判定口径仍是「总时长 ÷ 分片数」以外的东西**：单机并行天花板只有约 1.7x，
真正的吞吐来自「少算」（Stage-A 漏斗）与「切片」（shard），不是线程。

## 7. `holdout plan|reveal` — 一次性确认窗口

`experiment_window.json` 预注册的滚动窗口必须**从命令行揭示**，否则等到 ETA 也用不上。
`plan` 只读，`reveal` 才写账本（`holdout_guard(purpose="final")`）：

```bash
venv/Scripts/python.exe -m core.cli holdout plan --json
venv/Scripts/python.exe -m core.cli holdout reveal --window 1 --dry-run
venv/Scripts/python.exe -m core.cli holdout reveal --window 1
```

- `--window N` 是 `plan` 里的序号；不传则选**第一个已就绪且未揭示**的窗口。
- 未就绪（bar 数不足）或已揭示都会拒绝；重复揭示需要 `--allow-holdout-reuse`，
  且只应用于复算已发布结果（会写审计）。
- 账本 key 用 `rolling_<start>_<end>`，因此揭示**不会**移动预注册锚点。
- `FACTORMINER_PRODUCTION=1` 时禁止 `audit/off` 与 reuse 覆盖。

## 7b. 一轮实验内部：漏斗、缓存、断点、指标

`experiment` 的挖掘阶段现在分三层，成本和统计量级都要按这个读：

| 层 | 内容 | 成本 | 谁进来 |
|---|---|---|---|
| Stage A 粗筛 | IC / 多空净收益 / 换手上限 / 覆盖率，全部已算好 | ~0（<50ms） | 全部候选 |
| Stage B 精算 | 目标置换零分布（`null_n_perm`） | ~4.6s/候选（97.8%） | 过粗筛的，最多 `fitness.prefilter_keep` |
| Stage C 全闸门 | HAC t / BH-FDR / DSR / OOS 强度 / 成本 | ms | Stage B 的幸存者 |

- 被 Stage A 拦下的候选**仍然入库**（可审计），但 `gate_pass=false` 且
  `metrics.prefilter_reject` 写明原因（`turnover_above_cap` / `rankic_below_floor` /
  `cost_adj_negative` / `coverage_below_floor` / `rank_cut`）。
  它们**不会**被闸门判为"empirical 未计算=通过"。
- `fitness.turnover_max`（默认 0）是**硬约束**：多空账本平均换手超过它就 fitness 归零；
  0.15 对应"18bp 往返下必然亏损"的那条线。
- `evaluation.max_workers > 1` 会被降为 1 并 WARN：实测线程池 0.58x（更慢）。
  要吞吐请用漏斗 + shard。
- `evaluation.cache: true` 打开跨 run 评估缓存（`user_workspace/.eval_cache/`），
  key = 表达式 + 面板指纹 + 经济口径指纹；面板或成本变了就是 miss，不会复用旧判定。
  实测第二轮 32/41 命中，挖矿 41s → 27s。
- 每个 run 写两份审计：`evaluations.jsonl`（每个候选一行，含 cache hit/miss）与
  `metrics.jsonl`（每个 stage 的耗时/候选数/晋升数），可被外部采集。
- `--wf-scope promoted`（默认）：walk-forward 只验证过了闸门的因子；0 晋升时该 stage
  记录 `skipped + reason`，不会把整轮判失败。

### 试验族（trial family）

`experiment.trial_family` 是多重检验的**分区键**，必须在看数据之前写进配置并提交：

```json
"experiment": {
  "trial_family": "crypto_1d_top20_h5_v1",
  "family_declared_at": "2026-11-15",
  "family_budget": 5000
}
```

同一 family 共享 `n_trials` 与 BH 家族；不同 family（不同 horizon / universe / 范式）
独立计数。它写进 manifest（`trial_family` / `family_declared_at` / `family_budget`），
事后不能改 —— 否则就是 p-hacking。未声明时退回旧的全局单调分母。

### 增量挖掘

`gp.seed_from_db: true` + `gp.seed_top_k: 20`（+ `gp.seed_db_root: factor_db`）会把
生产库里 fitness 最高的已晋升 AST 注入第一代种群，而不是每次都从零开始。
种子只来自已过闸门的 in-sample 产物，不构成 holdout 泄漏。

## 8. 生产环境开关

```bash
export FACTORMINER_PRODUCTION=1
```

置位后 `holdout_guard.guard()` 拒绝：
- `holdout_mode ∈ {audit, off}`
- `allow_holdout_reuse=True`（无论 CLI 还是 Web）

理由：这两个开关是研究期便利设施，在生产里静默生效等于把一次性 holdout 变成可复用的验证集。
拒绝发生在 `guard()` 内部，因此 CLI、实验流水线、API 三条入口同时受约束。

## 9. 定时任务

Linux (cron，每天 08:10 UTC 补数据并跑一次)：

```cron
10 8 * * * cd /opt/FactorMiner && FACTORMINER_PRODUCTION=1 \
  venv/bin/python -m core.cli pipeline \
  --config user_workspace/configs/configExperiment.json \
  >> user_workspace/reports/cron.log 2>&1
```

Windows (任务计划程序)：

```powershell
$env:FACTORMINER_PRODUCTION = "1"
& "D:\code\github\FactorMiner\venv\Scripts\python.exe" -m core.cli pipeline `
  --config user_workspace/configs/configExperiment.json
```

建议的告警规则：
- `pipeline_*.json` 的 `exit_code != 0` → 立即告警；
- `doctor` 里 `freshness` 为 WARN 连续 2 天 → 告警（数据源/网络问题）；
- `stages[refresh].exit_code != 0` → 提醒（可能仍挖矿，但面板开始落后；`--stale-is-fatal` 下直接停）。
- 发布前核对 `factors --run-id <id>` 的拒绝原因；`promote` 只接受 `INSPECTED`。

## 10. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `doctor: coverage FATAL ... ends 2026-06-30` | 该标的数据只到 6 月 | 先 `refresh`；若交易所没有该标的历史则从 universe 移除 |
| `doctor: universe FATAL ... no file` | 配置里有未下载的标的 | `refresh` 或 `download --symbols ...` |
| `holdout_window: 0 ready` | 预注册窗口还没攒够 bar | 正常，看 `eta`；不要手工揭示 |
| `refresh: 1 pair(s) not current` | 网络或交易所限频 | 重试；连续失败看 `--retries` 与代理配置 |
| `data_root ... differs from the downloader's` | `--data-root` 指向非 `cwd/data`，且该根下缺文件 | 先 `refresh --data-root <同一个根>`；不要指望挖掘器自动下载 |
| `pipeline` 退出 1 但 experiment 成功 | 你开了 `--fail-if-no-promotion` 或 `--stale-is-fatal` | 看 summary 的 `no_promotion` / `stale` 字段 |
| `holdout reveal: no ready, unrevealed window` | 锚点之后还没攒够 bar | 看 `holdout plan` 的 ETA；不要手工指定日期绕过 |
| `promote: Refusing to promote ...` | 闸门未通过或目标已存在 | `factors --run-id` 看原因；不要用 `--force` 掩盖弱信号 |
