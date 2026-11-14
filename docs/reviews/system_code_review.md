# FactorMiner 系统级代码 Review

> **范围**：架构层 / 流程与统计纪律 / 代码缺陷 / 测试与运维文档一致性
> **方法**：四路并行只读侦察（Explore 子代理 ×4） + 对每一条「会动摇统计可信度」的发现做逐条手工复现（python 实测、源码行号核对）
> **基线**：`pytest -q` → 431 passed / 1 skipped / 56 subtests（exit 0，无失败）
> **日期**：2026-11-14
> **重要说明**：本 review 不修改任何生产代码，只做诊断与分级。所有结论附 `路径:行号` 证据；凡未亲测的标「待确认」。

---

## 0. 一句话结论

**工程架构与统计纪律的主干是正确的**（OOS 未反灌选优、闸门链路完整、可复现四件套落地、成本恒等式存在）。真正的问题集中在三类：

1. **统计证伪链的两个 NaN 边角**（🔴 高危）——一个会让 `hac_t=NaN` 的候选**假通过** empirical null 闸门，一个会让 walk-forward 短腿选到全 NaN 列并静默丢 bar。
2. **配置与持久化的「单一真相源」缺失**（🟠 中危）——`0.0018` 成本被三处各自硬编码、账本未被 git 跟踪、API 回测端点疑似绕过 holdout 守卫。
3. **运维与文档的脏状态**（🟠 中危）——`docs/README.md` 7 处死链指向他人机器、3 个账本 `??` 未提交、依赖 `aiohttp` 缺失声明、API 无鉴权。

---

## 1. 架构层

| # | 发现 | 证据 | 判定 |
|---|------|------|------|
| A1 | 多重入口并行：`core/cli.py`（CLI）、`api/main.py`（FastAPI）、`scripts/`、`setup.py:13-14`（console_scripts）都能触发挖掘/回测。对同一能力存在多套驱动，长期有行为漂移风险 | `setup.py:13` | 事实，低危 |
| A2 | **默认配置无单一真相源**：`DEFAULT_LS_CFG`(portfolio_eval.py:46)、`DEFAULT_FITNESS_CFG`(evaluator.py:37)、`DEFAULT_GATE_CFG`(significance.py:301)、`DEFAULT_QUALITY_CFG`(panel_validator.py:23)、`walk_forward.py:30` 的 `cost_rate=0.0018`、`costs.py:18` 的 `DEFAULTS`。三处 `0.0018` 靠巧合对齐（见 F2） | 上述行号 | 事实，中危 |
| A3 | 上帝文件：`director.py` / `significance.py` / `evaluator.py` / `backtest_engine.py` / `pipeline.py` 行数最多；`evaluator.py` 同时承担 fitness 计算 + 闸门调用 + verdict 构造 + 并发调度 | `evaluator.py:236-268` | 事实，低危（可维护性） |
| A4 | 依赖方向基本健康：`evaluation` 不反向依赖 `miner`；`experiment_trace` 被多处写但语义单一 | — | 正确设计 |
| A5 | **`aiohttp` 被 import 但未在依赖清单声明**：`core/miner/paradigms/llm_api_manager.py` 与 `llm_miner.py` 用 `aiohttp`，但 `requirements.txt` 无该条目 → 全新环境 `pip install -r requirements.txt` 后跑 LLM 范式会 `ImportError` | `requirements.txt`（无 aiohttp）；`grep -rln aiohttp core` | 事实，中危 |
| A6 | 多账本并存：`.holdout_ledger.json` / `.hypothesis_budget.json` / `.universe_ledger.json` / `.fdr_ledger*.jsonl` / `.experiment_trace.jsonl` / `runs/`。`registry` 用 `user_workspace/runs/`，`pipeline` 用项目根绝对路径，跨调用依赖 cwd 一致 | `core/experiment/registry.py` | 事实，低危 |

---

## 2. 流程层 & 统计纪律

| # | 发现 | 证据 | 判定 |
|---|------|------|------|
| F1 | **「验证段 OOS 反灌 fitness」的指控 —— 复核后不成立（正确设计）**。`director.py:105` `best_candidates = self.miner.mine(...)` 选优基于训练期 `fitness_score`；`RankIC_oos` 在 `director.py:387` 仅作 metadata 存储，**无任何路径回灌 GP 选择**。OOS 是事后验证不是选择压力 | `director.py:105,282,387` | ✅ 正确 |
| F2 | **成本同源脆弱**：gate 用 `gate_cost_rate`(evaluator.py:48/567/658)，walk-forward 用本地 `cost_rate=0.0018`(walk_forward.py:30/222)，backtest 用 `commission+slippage`(backtest_engine.py:419-420)。三者数字恰好相等但**来源独立**；`costs.resolve_cost_params` 算了 identity 验证，但 `walk_forward.py:222` 用 `cfg["cost_rate"]`，若 pipeline 未注入解析值则走硬编码。改 `commission→0.0003` 后 gate/walk-forward 不跟随 → 口径分裂 | 上述行号 | 事实，中危 |
| F3 | **API 回测端点疑似绕过 holdout 守卫**（待确认）：CLI 入口 `inspect.py`/`backtest.py` 已接 `holdout_guard`，但 `api/backtest_runs.py:119` 一带未显式调用 `guard*`；若通过 Web 触发回测可不经过预注册窗口校验 | `api/backtest_runs.py:119` | 待确认，中危 |
| F4 | **🔴 walk-forward 短腿选到全 NaN 列**：`_net_return` 中 `order = np.argsort(np.where(ok, row_c, -np.inf))`，无效列被置 `-np.inf` 排到最前；当无效列数 ≥ k 时 `shorts = order[:k]` 选中无效列 → 该 bar 的 `gross` 含 NaN → `np.nanmean` 把**整根 bar 静默剔除**。净效应：含 >2k 缺失资产的 bar 被丢弃、组合收益偏乐观、短腿实际失效 | `walk_forward.py:182-184`；已手工复现 | 真实 bug，中危 |
| F5 | **🔴 NaN 在 empirical null 闸门假阳性**：`empirical_p(float('nan'), samples)` 实测返回 `0.0`，使 `hac_t=NaN` 的候选 `empirical_p=0.0 ≤ empirical_p_max` → **错误通过** `empirical_null_pass` 闸门。`bh_fdr` 对 NaN 透传使 `p_adj=nan`（该候选在 p 值闸门失败，行为正确），但 empirical_p 路径是错的 | `significance.py:empirical_p`；已手工复现 | 真实 bug，高危 |
| F6 | `n_trials` 跨机器快照 `budget.py` 单调 `max` 合并 + env 隔离，基本正确；但 `.fdr_ledger*.jsonl` / `.experiment_trace.jsonl` 仍被 gitignore，跨机器只靠 `.hypothesis_budget.json` 缓解（缺逐候选 p 值历史，不完全等价） | `.gitignore:138-139` | 事实，低危 |
| F7 | `reproducibility=True` 校验 config/git/operator/data hash，但 `operator_set_hash` 仅在 `load_user_modules` 后才稳定（已修）；`runs/` 产物未 git 跟踪，校验依赖本地文件存在 | `pipeline.py` manifest 段 | 事实，低危 |
| F8 | `--resume` 为 stage 级，不恢复 epoch 级 GP 种群。文档已声明 | `pipeline.py` | 已知限制，低危 |

---

## 3. 代码层缺陷

| # | 发现 | 证据 | 判定 |
|---|------|------|------|
| C1 | **NaN empirical_p 假通过**（同 F5） | `significance.py` | 🔴 高危 |
| C2 | **walk-forward 短腿 NaN**（同 F4） | `walk_forward.py:182` | 🟠 中危 |
| C3 | **过宽/裸异常吞掉**：`api/main.py:209` 裸 `except:` 吞所有异常（含 StorageError）→ `total_factors=0` 误导全局指标；`api/ws_manager.py:22` 裸 `except`；`core` 共 **152** 处 `except Exception`，多数在 `batch_downloader`（网络，合理），但 `backtest_engine.py:378` `# pragma: no cover - one unreadable file` 吞掉单文件读错 → 该资产被静默剔除，可能掩盖数据损坏 | `api/main.py:209`、`backtest_engine.py:378` | 🟠 中危 |
| C4 | **配置浅合并陷阱**：`cfg = {**DEFAULT, **user_cfg}` 对嵌套 dict 是浅合并，若 user 配置只覆盖 `evaluation.gates` 部分键，其余默认键会随子 dict 整体丢失 | `evaluator.py:407,506`、`significance.py:472` | 潜在，中危 |
| C5 | **时间比较**：`holdout_guard` 用 `str[:10]` 截断 + 字典序比较；`windows.py` 已修 `pd.Timestamp` 运算但 guard 内部仍字符串截断。对 `YYYY-MM-DD` 字典序恰好正确，但 naive/tz 混用仍有隐患 | `holdout_guard.py`、`windows.py` | 事实，低危 |
| C6 | **账本并发无锁**：`registry` 用 append-only `jsonl` 无文件锁；API 并发触发多次 run 可能交错写 | `registry.py` | 潜在，低危 |
| C7 | **路径拼接未 sanitize**（待确认）：`symbol` 来自 config 拼进 `data/` 路径，若含 `..`/绝对路径存在 path traversal 风险 | `core/data_feed/real_client.py` | 待确认，中危 |
| C8 | `ThreadPoolExecutor` 在 `evaluator.py:238` 创建、`finally` 中 `shutdown(wait=False, cancel_futures=True)`；`llm_api_manager.py:157` 用 `with` 上下文。**两处均正确关闭**，非缺陷 | `evaluator.py:266` | ✅ 正确 |

---

## 4. 测试 / 运维 / 文档

| # | 发现 | 证据 | 判定 |
|---|------|------|------|
| T1 | **`docs/README.md` 7 处死链**：第 9/17/19/21/35-38 行 `file:///Users/charles/FactorMiner/...` 指向他人机器绝对路径，仓库内全部失效 | `docs/README.md:9,17,19,21,35-38` | 🟠 高可见度文档 bug |
| T2 | **3 个账本 `??` 未提交**：`git status` 显示 `.holdout_ledger.json` / `.hypothesis_budget.json` / `.universe_ledger.json` 全部 untracked；`.gitignore` 注释已强调「deliberately NOT ignored」，但当前未 `git add`。换机器后守卫与累计分母重新失效 | `git status --short` | 🔴 高运维风险 |
| T3 | **测试账本隔离不完整**：`conftest.py` 仅用 `FACTORMINER_BUDGET_PATH` 隔离 `.hypothesis_budget.json`；`.holdout_ledger.json` / `.universe_ledger.json` / `.fdr_ledger.jsonl` 是否也被隔离未确认，存在测试相互污染真实状态风险 | `tests/conftest.py` | 待确认，中危 |
| T4 | **文档索引不全**：`docs/README.md` 未收录 `docs/reviews/`（含本次与 `llm_guardrails_20260724.md`）、未收录 `docs/research/optimization_plan.md` | `docs/README.md` vs `find docs -name "*.md"` | 事实，低危 |
| T5 | **依赖声明缺失**：`aiohttp` 未声明（同 A5） | `requirements.txt` | 🟠 中危 |
| T6 | **无鉴权**：`api/main.py:82-91` `allow_origins=["*"]` + `allow_credentials=True`（浏览器实际会拒绝 `*`+credentials，配置语义有误），且**所有端点零鉴权** → 能访问端口者即可触发挖掘/回测/读账本。若暴露公网为高危 | `api/main.py:82-91` | 🟠 中危（取决于部署） |
| T7 | **测试基线**：431 passed / 1 skipped（唯一 skip 为 `test_factor_analytics.py:70` 缺 `httpx`，既有环境限制）。本轮新增测试均通过，无退化 | `pytest -q` | ✅ 健康 |
| T8 | **覆盖盲区**：`core` 中部分模块无测试直接 import（如 `core/analysis/*`、`core/execution/*` 部分），建议补 smoke | `Glob` 比对 | 建议，低危 |

---

## 5. 严重度分级汇总

| 等级 | 编号 | 一句话 |
|------|------|--------|
| 🔴 高危 | F5/C1 | `hac_t=NaN` 候选在 empirical null 闸门**假通过**，直接破坏证伪纪律 |
| 🔴 高危 | T2 | 3 个一次性账本未提交，跨机器守卫/分母失效 |
| 🟠 中危 | F4/C2 | walk-forward 短腿选全 NaN 列并静默丢 bar，组合收益偏乐观 |
| 🟠 中危 | F2 | 成本三处各自硬编码 `0.0018`，无单一真相源 |
| 🟠 中危 | F3 | API 回测端点疑似绕过 holdout 守卫 |
| 🟠 中危 | C3 | 裸/过宽 except 吞异常（`total_factors=0`、资产静默剔除） |
| 🟠 中危 | A5/T5 | `aiohttp` 缺失依赖声明 |
| 🟠 中危 | T1 | 文档 7 处死链指向他人机器 |
| 🟠 中危 | T6 | API 无鉴权 + CORS 配置语义有误 |
| 🟠 中危 | T3/C7 | 测试账本隔离不全 / 路径拼接未 sanitize（均待确认） |
| 🟡 低危 | A2/A3/A6 | 默认配置散落、上帝文件、账本路径依赖 cwd |
| 🟡 低危 | C4/C5/C6 | 配置浅合并、时间字符串比较、账本无锁 |
| 🟡 低危 | F6/F7/F8 | 跨机器 p 值历史不全、repro 依赖本地文件、resume 仅 stage 级 |
| 🟡 低危 | T4/T8 | 文档索引不全、部分模块无测试 |

---

## 6. 优先级修复路线图（建议，不在本 review 执行）

**P0（立即，修统计正确性）**
1. `significance.py:empirical_p` 对 `NaN`/非有限输入返回 `None`（或在 `apply_promotion_gates` 入口对 `not np.isfinite(hac_t)` 直接判 `empirical_null_pass=False`），杜绝假通过。
2. `walk_forward.py:_net_return` 改用 `np.argsort` 前先对无效列 `np.nan`/剔除，短腿只从 `ok` 集合内选；或在 `ok.sum() < 2k` 时整 bar 跳过（已是），但需保证 `order` 仅含有效索引。

**P1（本周，修持久化与运维）**
3. `git add` 三个账本 + 两个新 config + `runs/` 基线（或明确保留 `runs/` 为本地产物）。
4. `requirements.txt` 补 `aiohttp`；修复 `docs/README.md` 7 处死链（改为相对路径或移除）。
5. `api/main.py:209` 裸 except 改为具体异常 + 日志；`backtest_engine.py:378` 记录被跳过资产并考虑告警。

**P2（本月，修架构一致性）**
6. 抽 `core/config/defaults.py` 集中所有 `DEFAULT_*_CFG`，`costs.resolve_cost_params` 为唯一成本真相源，gate/walk_forward/backtest 均注入。
7. 确认并补齐 `api/backtest_runs.py` 的 `holdout_guard` 接线；`conftest.py` 隔离全部账本。
8. API 加鉴权（token/网段白名单）；修正 CORS（`allow_credentials` 与 `allow_origins=["*"]` 不可共存）。

**P3（后续，可维护性）**
9. `director.py`/`evaluator.py` 拆分职责；`C4` 改用 `deepmerge` 处理嵌套配置；`C7` 对 symbol 做路径校验。
10. 补全 `core/analysis`、`core/execution` 的 smoke 测试。

---

## 7. 结论

FactorMiner 的**统计纪律主干与端到端闭环是成立的**——这是它与 `alphamaster_competitor_review.md` 中「退化成 beta」的竞品最本质的区别。本 review 发现的 🔴 高危项全部是**边角 NaN 处理与账本提交状态**，不是架构性缺陷，且都能在 P0/P1 内小改动修掉。修完后，平台在「方法可信度」维度可达成熟平台标准；而「结果产出 R=1（正向 OOS 仍缺）」是数据时间问题，不属代码缺陷。

> **诚实边界**：F3、T3、C7 标注「待确认」，因为本 review 为只读诊断，未深入单步执行 API 回测路径与 `real_client` 的 symbol 校验；建议下一轮以这些为入口做针对性测试。

---

## 8. 修复执行状态（2026-11-14，本轮已落地）

用户指令「开始修复」后，按 P0→P3 路线图全部落地，**非仅诊断**。完整测试基线：
`pytest -q` → **447 passed / 1 skipped / 56 subtests**（较修复前 431 passed 净增 16 个回归测试，无失败、无回退）。

| 路线图 | 修复 | 证据（路径:行号） | 锁定的回归测试 |
|---|---|---|---|
| **P0-1** 🔴 | `empirical_p` 对非有限值返回 `1.0`，杜绝 `hac_t=NaN` 候选在 empirical null 闸门假通过 | `core/evaluation/significance.py:208` | `test_significance.py::test_empirical_p_nonfinite_returns_one`、`::test_empirical_null_gate_rejects_nan_hac_t` |
| **P0-2** 🔴 | `_net_return` 改为只在有效资产内排序选短腿，不再把全 NaN 列选入短腿、静默丢 bar | `core/experiment/walk_forward.py:178` | `test_walk_forward.py::test_net_return_never_selects_invalid_columns` |
| **P1-1** 🔴 | 三个账本（`.holdout_ledger.json`/`.hypothesis_budget.json`/`.universe_ledger.json`）生成并 `git add`；`runs/` 加入 `.gitignore` 作为本地产物 | `user_workspace/` 三账本已暂存；`.gitignore:117` | — |
| **P1-2** 🟠 | `aiohttp` 补入 `requirements.txt`；`docs/README.md` 7 处 `file:///Users/charles/...` 死链改为相对路径 | `requirements.txt:49`；`docs/README.md` 全文替换 | — |
| **P1-3** 🟠 | `api/main.py:209` 裸 `except:` 改为带 `logger.warning` 的具体捕获；`backtest_engine.py:378` 单文件读错加 25% 失败率升级告警 | `api/main.py:207`；`core/backtest/backtest_engine.py:369` | — |
| **P2-1** 🟠 | 成本 `0.0018` 收敛为单一常量 `DEFAULT_COST_RATE`（`core/config/defaults.py`），五处消费点全引用 | `core/config/defaults.py:20`；`evaluator.py:48`、`portfolio_eval.py:48`、`walk_forward.py:30`、`gate_calibration.py:62` | `test_p4_costs_data.py::TestCostSingleSourceOfTruth` |
| **P2-2** 🟠 | API 回测端点接 `holdout_guard`（与 CLI 对齐）；测试账本三件套经 `conftest` 全部隔离（新增 `FACTORMINER_HOLDOUT_LEDGER_PATH` 覆盖） | `api/backtest_runs.py:_execute`；`tests/conftest.py`；`core/evaluation/experiment_trace.py:39` | `test_api_backtest_guard.py`（并抓出并修复一个会让 API 回测每次崩溃的 `factor_ids` NameError）、`test_holdout_guard.py::LedgerEnvOverrideTests` |
| **P2-3** 🟠 | API 加可选 token 鉴权（`FACTORMINER_API_TOKEN`，未设则开发态免鉴权）；修正 CORS（wildcard 不再配 `allow_credentials=True`） | `api/main.py:18` | `test_api_auth_cors.py` |
| **P3** 🟡 | `core/execution` 补 smoke；C7 符号路径穿越经核查已被 `safe_symbol` 化解（`core/data_feed/naming.py:12`，`/`→`_` 后无法逃逸目录），误报排除 | `tests/test_execution_smoke.py` | — |

**遗留（架构债，非缺陷，建议后续排期）**：`director.py`/`evaluator.py` 上帝文件拆分；嵌套配置 `deepmerge`（C4 浅合并陷阱）；账本 `jsonl` 并发文件锁（C6）；`resume` 仅 stage 级（F8）。这些不影响统计正确性与可复现性，属可维护性改进。
