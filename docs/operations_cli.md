# 生产 CLI 运维手册

三条命令构成一条无人值守的流水线：**refresh → doctor → experiment**。
它们共用同一份配置（`user_workspace/configs/*.json`），彼此只通过数据与账本解耦。

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
- `--fail-if-no-promotion`：闸门没有晋升任何因子时返回 1 —— 给告警用；
  **默认关闭**，因为"0 晋升"在信号弱的阶段是正常结果，不是故障。
- 每次运行都会写 `user_workspace/reports/pipeline_<UTC时间戳>.json`：
  `stages[]{stage,exit_code,detail}`、`doctor` 全量结果、`run_id`、`candidates`、
  `promoted`、`exit_code`。告警系统读这个文件比抓 stdout 稳。

退出码：`0` 正常；`1` doctor FATAL 或 experiment 失败；`2` 用法错误。
**refresh 单独失败不会中止挖矿**（用本地已有的数据继续跑），但 summary 里会记录，
所以"数据没补全"仍然可以被监控到。

## 5. 生产环境开关

```bash
export FACTORMINER_PRODUCTION=1
```

置位后 `holdout_guard.guard()` 拒绝：
- `holdout_mode ∈ {audit, off}`
- `allow_holdout_reuse=True`（无论 CLI 还是 Web）

理由：这两个开关是研究期便利设施，在生产里静默生效等于把一次性 holdout 变成可复用的验证集。
拒绝发生在 `guard()` 内部，因此 CLI、实验流水线、API 三条入口同时受约束。

## 6. 定时任务

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
- `stages[refresh].exit_code != 0` → 提醒（可能仍挖矿，但面板开始落后）。

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `doctor: coverage FATAL ... ends 2026-06-30` | 该标的数据只到 6 月 | 先 `refresh`；若交易所没有该标的历史则从 universe 移除 |
| `doctor: universe FATAL ... no file` | 配置里有未下载的标的 | `refresh` 或 `download --symbols ...` |
| `holdout_window: 0 ready` | 预注册窗口还没攒够 bar | 正常，看 `eta`；不要手工揭示 |
| `refresh: 1 pair(s) not current` | 网络或交易所限频 | 重试；连续失败看 `--retries` 与代理配置 |
| `data_root ... differs from the downloader's` | `--data-root` 指向非 `cwd/data`，且该根下缺文件 | 先 `refresh --data-root <同一个根>`；不要指望挖掘器自动下载 |
| `pipeline` 退出 1 但 experiment 成功 | 你开了 `--fail-if-no-promotion` | 关掉该开关，或接受这是"本轮无晋升" |
