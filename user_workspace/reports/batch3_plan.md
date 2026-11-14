# 第三批梳理：CI / 容器化 / 数据自动更新

> 定位：决策材料，不是实施记录。前两批（回测闭环、可信度加固）已完成并推送。
> 结论先行：CI 与 Docker 是"别人能不能跑起来"的问题，一周能收；数据更新是"能不能持续产出结论"的问题，但真正的瓶颈不是补历史，而是**等未来**。

## 1. 现状盘点

**数据侧**（`data/binance/futures/*-1d-futures.feather`）：

| 项 | 值 |
|---|---|
| 标的数 | 20（BTC/ETH/SOL/BNB/XRP/DOGE/ADA/AVAX/LINK/DOT/LTC/TRX/ATOM/NEAR/APT/ARB/OP/INJ/FIL/ETC） |
| 周期 | 1d（另有 5m 等若干） |
| 覆盖 | 多数 2023-01-01 起，最新到 2026-11-13 |
| 与今天（2026-11-14）差距 | 1 天 |

**已有能力**（不用再造）：

- `SmartBatchDownloader.download_ohlcv_batch(..., download_mode='merge'|'fill_gap'|'overwrite')` —— 增量合并，`fill_gap` 会先查覆盖范围再只补缺口。
- `GapFiller.auto_fill_gaps / scan_for_gaps / execute_fill_tasks` —— 缺口扫描与补齐。
- `GET /api/data_coverage` —— 单标的新鲜度查询。
- API 的 `/api/download_data` —— 异步批量下载 + WebSocket 日志。

**缺的三件**：

1. **定时触发**：全库没有 `schedule` / `cron` / APScheduler，更新靠人记得。
2. **失败重试与告警**：单标的失败只写一行 UI 日志，跑完没人知道。
3. **新鲜度可见**：`/api/data_coverage` 是单标的点查，没有一个"整个库现在瘸不瘸"的视图。20 个标的里有 3 个卡在两个月前，界面上看不出来。

## 2. 数据更新：三条路

### 方案 A：外部调度 + CLI（推荐）

新增 `scripts/update_data.py`：

```
读 config 的 data_feeds.pairs
  → 对每个 (symbol, timeframe, trade_type)
      → 查本地最后一根 K 线
      → download_ohlcv_batch(download_mode="fill_gap", start=最后日期-3, end=T-1)
      → 重试 3 次（2s/8s/30s 退避）
  → 汇总：成功 / 跳过 / 失败
  → 写 user_workspace/reports/data_update/YYYY-MM-DD.json
  → 对更新后的文件跑 panel_validator，把 critical 打进同一份报告
```

调度：Windows 任务计划（本机）或 Linux `cron`，每天 08:30（Binance 日线在 08:00 收盘，留 30 分钟）。

- 优点：进程隔离（下载崩了不影响 API）、零新依赖、日志天然分开、可以手工重跑。
- 缺点：换机器要重新配；调度状态不在 Web 里。

### 方案 B：API 内置 APScheduler

- 优点：一个界面看完；配置集中在 `config.json`。
- 缺点：新增依赖；API 重启要恢复调度（又是一套持久化）；下载线程与 Web 进程同生命周期，长任务期间 API 会变慢。
- 适合：单机长期常开、且希望"打开网页就能看到下一次几点跑"。

### 方案 C：GitHub Actions 定时

- 不适合这个场景：产物是二进制 feather，塞进仓库会把 git 撑爆；artifact 只保留 90 天。
- 除非把数据层整体搬去对象存储，那是另一个工程。

**建议：A 做调度，B 只做"视图"**——API 提供 `GET /api/data/freshness` 与一个只读的 `next_run_at`，不负责真的触发。这样既有可观测性，又不把下载塞进 Web 进程。

## 3. 关键设计：把"等数据"变成系统状态

P0.5 正向验证需要 2026-12 之后的新区间。这件事的本质是**日历问题**，不是数据问题：

- 2026-12-01 起算，日频累计 90 根 → 2027-03-01 才够一个季度样本；
- 在此之前无论跑多少轮挖掘，都只是在一个已经消耗过的区间里自我重复。

现在的做法是靠人记。应该写进配置：

```json
"experiment_window": {
  "label": "P0.5 正向验证",
  "start": "2026-12-01",
  "min_bars": 90,
  "timeframe": "1d"
}
```

`GET /api/data/freshness` 顺带算：`已积累 12/90 根，预计 2027-03-01 就绪`。到期由 automation 提醒。这把"什么时候能验证"从记忆变成了可查询的系统状态——也避免有人忍不住提前去翻那段数据（一旦翻过就不再是干净的 holdout）。

## 4. CI：最大阻塞点已确认

`.github/workflows/ci.yml` 计划：

```yaml
- Python 3.11
- pip install -r requirements.txt -r requirements-dev.txt
- pytest -q                       # 基线：240 passed, 1 skipped
- Node 20: npm ci && npm run build
```

**阻塞点**：`ml4t-backtest` 不在 PyPI，`requirements.txt` 里写的是 `ml4t-backtest>=0.1.0` 并注明"本地 editable 安装"。CI 装不上，`test_backtest_oos.py` / `test_backtest_weights.py` 会直接 import 失败。

三种解法：

| 解法 | 做法 | 代价 |
|---|---|---|
| **A. 从 git 装**（推荐） | `ml4t-backtest @ git+https://github.com/just4zeroq/backtest.git@<tag>` | 要求仓库公开；构建慢一点 |
| B. 私有 wheel | 构建 wheel 放 `vendor/` 或私有 index | 要维护版本 |
| C. 跳过 | `pytest -m "not requires_ml4t"` | 回测链路在 CI 里没有回归保护 |

顺带修掉一个真问题：`requirements.txt` 从未声明 `httpx`，而 `fastapi.testclient` 依赖它——本地一直是"整个 `test_factor_analytics.py` 收集失败"，被当成环境问题绕过了。现在该文件已改成缺 `httpx` 时跳过单个 API 测试，并新增 `requirements-dev.txt` 显式声明。

## 5. Docker

多阶段构建：

```
node:20-slim   → npm ci && npm run build → dist/
python:3.11-slim → pip install -r requirements.txt → 拷 dist 与 core/api
```

需要挂载的卷：`data/`（行情）、`factor_db/`（因子库）、`user_workspace/`（配置/报告/账本）。三个都是"必须持久化"的，容器本身无状态。

同样受 `ml4t-backtest` 分发方式制约——Docker 里不能用本地路径。

## 6. 建议的实施顺序

| 顺序 | 事项 | 前置 | 价值 |
|---|---|---|---|
| 1 | `GET /api/data/freshness`（全部标的最后日期 + 过期标红 + 实验窗口进度） | 无 | 立刻能看出库瘸在哪 |
| 2 | `scripts/update_data.py` + 任务计划 | 无 | 数据不再靠人记 |
| 3 | 确定 `ml4t-backtest` 分发方式（git vs wheel） | 需要你决定仓库是否公开 | 解锁 4、5 |
| 4 | `.github/workflows/ci.yml` | 3 | 回归从手动变自动 |
| 5 | `Dockerfile` + compose | 3 | 换机器一条命令起来 |

1 和 2 不依赖任何决策，可以立刻做；3 之后的事情取决于你怎么定。

## 7. 需要你决定的三件事

1. **调度放哪**：Windows 任务计划（这台机器）/ 服务器 cron / 还是干脆用 API 内置调度（接受它的缺点）？
2. **`ml4t-backtest` 怎么分发**：`just4zeroq/backtest` 是公开仓库可以直接 `pip install git+...` 吗？如果不能公开，要不要打成 wheel 放进 `vendor/`？
3. **实验窗口到期提醒**：要不要做成 automation（到点提醒"可以跑 P0.5 正向验证了"），还是只在 freshness 接口里显示进度？
