# 第三批：数据更新、CI 与容器化

日期：2026-11-14
范围：第三批（运维闭环），三个决策已按用户确认执行——外部任务计划 + CLI、公开 git 安装 ml4t-backtest、实验窗口只在界面显示进度。

---

## 1. 数据新鲜度视图（`/data`）

**问题**：`/api/data_coverage` 一次只查一个标的，需要手动选好标的才知道有没有数据。三个标的停更两个月，界面完全看不出来。

**做法**：`core/data_feed/freshness.py` 直接扫 `data/<exchange>/<trade_type>/*.feather`，只读 `date` 列，算每根末日期与 lag。

- 阈值按周期区分：`1d` 允许 2 天，日内（`1h`/`15m`/`5m`/`1m`…）1 天。日线只该缺"今天这根还在走"，日内缺一天就是真的断了。
- `GET /api/data/freshness` 返回 `summary` + `items` + `experiment`；前端 `FreshnessPanel` 默认折叠，只显示摘要行，点开才展开按周期分组的明细表。

**真实库第一次跑出来的结果（35 个文件）**：

| 周期 | 文件数 | 滞后 |
|---|---|---|
| 1d | 20 | 0（全部 lag=1，末根 2026-11-13） |
| 1m / 15m / 5m | 4 / 2 / 2 | 全部 |
| 12h / 1h / 2h / 3m / 4h / 6h / 8h | 各 1 | 全部 |

第一版汇总只报一个数字，看起来像"15 个文件滞后 → 库坏了"。实际上 15 个全是 2025-08 就停更的探索性日内文件，日线库是齐的。所以汇总改成两维：总数 + 按周期拆分，标的去重（原来 `BTC/USDT:USDT` 会因为多个周期重复出现 9 次）。这个信息反而有用——那些日内文件是应该清理或补更的历史遗留，现在它自己暴露出来了。

接口还顺手修了一个隐患：`annotate_freshness` 原先只认内存里的 `_last_ts`，报告一旦 JSON 落盘再读回来就没有 lag 了。现在缺失时回落到解析 `last` 字符串。

## 2. 预注册窗口进度

`user_workspace/experiment_window.json`：

```json
{"label":"P0.5 正向验证","start":"2026-12-01","min_bars":90,"timeframe":"1d","note":"..."}
```

接口按"最慢的那个标的"算已累积根数（只有每个标的都有，窗口才干净），输出 `accumulated / remaining / coverage / ready / eta`。当前实测：

```
0 / 90 bars, coverage 0.0, ready false, eta 2027-02-28, as_of 2026-11-13
```

按用户决定，只显示进度，不做到期提醒 automation。

## 3. 增量数据更新（`scripts/update_data.py`）

按"外部任务计划 + CLI"实现，脚本本身不带调度。

- **只补尾部**：每个标的从自己最后一根 K 线开始（留 1 根重叠，靠 merge 去重），到 T-1 为止。T-1 是默认截止日——今天那根还在走。
- **幂等**：已经最新就空跑，退出 0。调度器提前触发、重复触发都不会造成重复下载。
- **裁剪越界数据**：下载器是按 `limit` 向前取，末批会一路取到今天。落盘后统一裁掉晚于截止日的行。真实补数一次（2025-01-01→2025-01-31）**裁掉 652 根**，最终文件正好 31 根、末根 2025-01-31。没有这层，每次无人值守更新都会往库里塞一根未收盘日线。
- **失败可见**：3 次指数退避重试；单标的失败不影响其他标的；最后有 `pending/new/partial/failed` 残留就退出 1，并逐条打印原因。
- **报告**：`user_workspace/reports/data_updates/update-<ts>.json` + 固定名 `latest.json`，含每个标的的 `attempts / trimmed_rows / new_last / new_rows`。

### 过程中修掉的两个真 bug

1. **新标的被静默跳过**：`run_plan` 只处理 `pending`，`new`（本地没有文件的标的）直接 `continue`，但汇总只检查 `failed`，于是"什么都没下载"也打印 `done`、退出 0。这是无人值守任务最危险的失败模式——看起来成功了。现在 `new` 与 `pending` 同样处理，且 `pending/new` 残留一律算失败。
2. **落盘目录写死**：`DataDownloader` 里三处 `data_path("data", ...)` 硬编码，`--data-root` 只管了规划和校验。已改为 `self.data_root`（默认 `data`），`SmartBatchDownloader(data_root=...)` 透传。真实下载已验证写到指定目录，没有污染生产库。

## 4. CI（`.github/workflows/ci.yml`）

- `backend`：Python 3.12（与验证用的 venv 3.12.1 一致）、pip 缓存（按 `requirements*.txt` 做 key）、`pytest tests/ -q`，并在跑测试前打印 `ml4t.backtest` 的实际路径，避免"装了个空壳"。
- `frontend`：Node 20、`npm ci`（有 lock）、`npm run build`（内部先 `tsc -b`，类型错误会让 job 失败）。
- `requirements.txt` 的 `ml4t-backtest` 从 `>=0.1.0` 改为固定提交：

  ```
  ml4t-backtest @ git+https://github.com/just4zeroq/backtest.git@f0143a86813b65261f2123ea2175604042ad02be
  ```

  仓库 pyproject 里 `name = "ml4t-backtest"`、hatchling + hatch-vcs，可直接按 URL 安装；固定 SHA 是为了 CI 与本地用同一份执行语义，升级必须显式改这一行。
  注意：本地开发机其实没有 pip 安装它，而是 `PYTHONPATH` 指到 `D:\code\github\backtest\src`（命名空间包 `ml4t`，`__file__` 为 None）。CI 走 pip 安装是另一条路径，第一次跑 CI 时要看那行 `ml4t.backtest ->` 的输出确认。

## 5. 容器化

- `Dockerfile` 多阶段、两个 target：`frontend`（node:20-slim → `npm ci` + `vite build`）、`api`（python:3.12-slim + git + libgomp1，uvicorn **单 worker**）、`web`（nginx:1.27-alpine 托管 dist）。
- 单 worker 是刻意的：`TaskManager` 与 WebSocket 广播都在进程内存里，多 worker 会各答一份任务列表。
- `web/nginx.conf`：`/assets` 一年不可变缓存、`index.html` 不缓存、未知路径回落 `index.html`（`/backtest`、`/inspector` 这类前端路由没有对应文件）。
- `docker-compose.yml`：`api` 8000、`web` 5173（与 `npm run dev` 同端口，前端里写死的 `http://localhost:8000` 因此不用改）；四个状态目录 bind mount；容器内 `FACTORMINER_NO_PROXY=1`（宿主代理的 127.0.0.1 在容器里不可达），并给了 `host.docker.internal` 的反向示例。
- `.dockerignore` 排掉 `data/`、各 `factor_db*/`、`venv/`、`node_modules`、`*.feather`——数据量比镜像大得多，且不该进层。

**未验证项**：本机 `docker` CLI 存在但 daemon 没起（`npipe:////./pipe/dockerDesktopLinuxEngine` 连不上），所以只做了 `docker compose config` 的语法/结构校验，镜像没有真正构建。第一次在有 daemon 的机器上执行 `docker compose up -d --build` 时，重点看两处：`pip install -r requirements.txt` 里 ml4t 的 git 安装，以及 nginx 下 `/backtest` 的直接访问。

## 6. 操作手册

```bash
# 每天看还差多少，不下载
python scripts/update_data.py --dry-run

# 补到 T-1（必须用装了 ccxt 的 venv）
venv/Scripts/python.exe scripts/update_data.py

# 单标的、指定截止日
venv/Scripts/python.exe scripts/update_data.py --symbols BTC/USDT:USDT --until 2026-11-13
```

调度：

- Linux：`10 8 * * * cd /srv/FactorMiner && venv/bin/python scripts/update_data.py`
- Windows 任务计划：程序 `D:\...\venv\Scripts\python.exe`，参数 `scripts\update_data.py`，起始目录仓库根目录。

退出码：`0` 全部到截止日（含"本来就最新"）；`1` 有标的没补到（看 stderr 与 `latest.json`）；`2` 配置错误或意外异常；`130` 手动中断。

## 7. 测试

新增 `tests/test_freshness.py`（23 项）与 `tests/test_update_data.py`（17 项），覆盖：符号回显、扫描/非规范文件名跳过、按周期判定滞后、阈值覆盖、实验窗口三种状态与"最慢标的决定窗口"、报告汇总与去重、规划（current/pending/new）、尾部裁剪、新标的确会被下载（该 bug 的回归测试）、重试后成功、持续失败只报告不抛出、短下载标 partial。

全量：`pytest tests/ -q` → **280 passed, 1 skipped**（新增 40 项）。

## 8. 数据清理（2026-11-14）

用户要求清掉"有问题的数据"。分三批执行，共删除 **21 个文件 / 46.88 MB**，20 个日线文件与 `BTC-5m` 全部保留。逐文件清单（路径、字节数、行数、sha256 前 16 位、删除理由）在 `user_workspace/reports/data_cleanup_20261114.json`。

**第一批（15 个，17.17 MB）**：显式列给用户确认后删除。

- 10 个 2025-07-20→2025-08-19 那次实验的孤儿切片：BTC 的 12h/1h/2h/3m/4h/6h/8h/15m、BCH 的 15m/5m。没有任何配置引用这些周期/标的。
- 4 个 1m：`BTC-1m` 结构损坏（多余的 `level_0`/`index` 列、末行 `date=NaT`、索引非升序）；`ASR-1m` 16.4% 零成交、`ASTR-1m` 18.7% 零成交、`AGT-1m` 随机山寨币探针。
- 1 个手工备份残留 `BTC-1d-....feather.bak-20251114`。

**第二批（4 个，29.69 MB）**：`find` 才发现，第一轮的扫描器根本没看见它们。

`SUI-1m`(20.7 MB/778k 行)、`1INCH-1m`(7.6 MB/347k 行)、`1000CAT-1m`(1.3 MB/61k 行)、`A2Z-1m`(1.0k 行) 的列只有 `['open','high','low','close','volume']`——**没有时间列**。pandas 写 feather 时会丢弃非默认索引，这几份数据因此没有时间轴，对任何时序管线都不可用。它们比 `BTC-1m` 更彻底地坏，而库里所有报表都说"正常"。

**第三批（2 个，16 KB）**：`crypto_streams/` 下 1h 的 BTC `basis`/`funding_rate`，同样是 2025-07/08 实验残留，无配置启用 `crypto_streams`。

### 为什么会有第二批：扫描器的两个盲点（已修）

1. **只走两层目录**：`data/<exchange>/<trade_type>/*.feather`，`crypto_streams/` 子目录（62 个文件）完全不可见。新增 `_candidate_files()` 走两层 + 一层子目录，流文件带 `stream` 字段返回。
2. **静默跳过读不出 `date` 的文件**：`scan_data_root` 直接 `continue`，于是"文件存在但无法解析"比"文件不存在"更隐蔽。新增 `scan_library()` 返回 `(items, skipped)`，报告里出 `summary.unreadable{count, paths, details}`，界面用红色块列出前 3 条路径与原因。

顺带修掉两个连带问题：`assets_1d`/`stale` 只统计 OHLCV（流文件不该被算成"一个标的"），`update_data.plan_updates` 也跳过流文件——否则 BTC 的 `basis` 文件会被当成 BTC 的价格文件，导致漏补 OHLCV。

### 保留但需要知道的两件事

- **`BTC-5m`（217k 行，末根 2025-08-19）仍在**：`config.json`、`configAdvancedGP.json`、`demo_config.json` 都用 5m，删除会让这些模板无本地数据。它因此是面板上唯一的 `stale`（5m×1）。注意它的 `mine_period` 是 2025-07-20→2025-08-01，对这个窗口来说数据是完整的——"滞后"是按今天算的，不是按配置窗口算的。若希望面板彻底干净，可删掉它并让配置按需重新下载。
- **20 个 `open_interest` 流只有 25 行**（2026-10-21→2026-11-14，行数完全一致）：币安只提供约 30 天 OI 历史，不是文件损坏。当前没有配置启用 `crypto_streams`，但一旦启用 OI，特征在 2023–2025 的窗口里会几乎全空。这属于数据源上限，需要单独决定（要么只用最近窗口，要么把 OI 从特征集里去掉），不是清理能解决的。

清理后：**81 个文件 / 20 日线 + 1 个 5m + 60 个原生流 / 1 个滞后 / 0 个不可解析**。

## 9. 仍未做

- 跨轮因子面值正交检查（不同 run 之间只比 archive 内 40 个签名）。
- 回测归因层：NN 特征归因、中性化、Brinson。
- P0.5 正向验证要等 2027-02-28 之后窗口攒够 90 根。
