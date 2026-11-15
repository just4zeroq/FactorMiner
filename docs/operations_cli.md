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
`dsr_pass=71, empirical_null_pass=66`）、按 fitness 排序的明细。
`--json` 给出每个因子的 HAC t / p_adj / DSR / empirical_p / 失败项，供告警或看板消费。

## 6. `promote` — 把通过的因子发布到生产库

实验刻意写进 `<run>/factor_db`，避免坏 run 污染共享库；因此通过的因子还不在
`factor_db/` 里。发布命令把 **metadata + values 快照 + 引用的源码/权重/模型**
作为一组复制，并追加 `user_workspace/reports/promotions.jsonl` 审计：

```bash
venv/Scripts/python.exe -m core.cli promote --run-id <id> --factor-id fac_xxxx \
  --db-root factor_db --dry-run          # 先看会复制哪些文件
venv/Scripts/python.exe -m core.cli promote --run-id <id> --factor-id fac_xxxx
```

安全规则：

- 必须 `metrics.gate_pass=true` **且** `lifecycle_status=INSPECTED`；
- 目标已存在同 id 一律拒绝（含 `--force` 时也不覆盖）；
- `--force` 只用于绕过闸门状态检查，会在 lineage 与审计里记 `forced=true`；
- `metadata.data_lineage.promotion` 记下 run_id / 源库 / 时间 / 闸门指标。

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
