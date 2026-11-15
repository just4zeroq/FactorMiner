# FactorMiner 全模块复审：离「生产可挖掘因子」还差什么

日期：2026-11-15　范围：`core/` `api/` `scripts/` `user_workspace/configs|custom_miners` `web/src`
方法：逐模块通读 + 用上一轮真实运行产物（`user_workspace/runs/run_20261115T052211Z_61ffe5`）实测复算
基线：全量 633 passed / 1 flaky（已修）/ 78 subtests

---

## 0. 一句话结论

**系统现在能完整跑完、能正确拒绝，但"0 晋升"这个结论目前不可信 —— 因为闸门里有一个结构性错误的检验（empirical-null），它对任何与资产无条件漂移相关的因子都给 p≈0.95。** 修掉它之后才第一次具备"能否挖出 alpha"的判定能力。除此之外，真正卡住产出的是 **搜索面（20 资产 × OHLCV × 3 代）与经济门槛（18bp 往返成本 vs 实测 2bp/bar 净收益）的量级不匹配**，不是代码质量。

---

## 1. 第 1、2 层修复完成情况（本轮已交付，commit `7891532`）

| 编号 | 问题 | 状态 | 证据 |
|---|---|---|---|
| E-03 | HAC t 用正态尾（有限样本偏激进） | 已修 | `significance.py:pvalue_from_t(df)` → Student-t，`evaluator.py:657` 传 `df=n_ic-1` |
| E-04 | `empirical_p` 无 +1 校正，n_perm 次可报 p=0 | 已修 | `significance.py:241` `(count+1)/(n+1)` |
| — | `evaluate_final()` 是死代码（真实 OOS 走 director 私有路径） | 已澄清 | 补 docstring 说明"唯一公开一次性 OOS 入口、与 director 同源" |
| G-01 | RL advantage 用循环内运行均值（有偏） | 已修 | `rl_miner.py:update_model` 先集批再算 `baseline=mean(batch)` |
| G-02 | LLM 失败静默 mock 入库 | 已修 | `llm_miner.py` 无 api_manager 且未开 `llm_allow_mock` → `RuntimeError`；mock 候选打 `synthetic_mock` |
| M-05 | NN 早停保存的权重与报告 loss 不同一参数点 | 已修 | `nn.py:train` 用更新后参数重算 loss 再快照 |
| M-04 | `_ts_decay_linear` 非满窗口取错权重段 + NaN 污染 | 已修 | `operator_runtime.py` 取头部 `seg`，`isfinite` 掩码 |
| D-05 | 未收盘 bar 进入挖掘面板 | 已修（仅面板侧） | `real_client.py:_drop_unclosed_last_bar` |
| D-06 | 13 份配置无 `data_quality.strict` | 已修 | 13/13 `strict=true`，`loader` 对 critical 直接 raise |
| D-07 | 组合因子 `member_factor_ids` 未持久化 | 已修 | `expressions.py:to_dict/from_dict` + 逐元素复算测试 |
| K-05 | Web 无 OOS 参数 | **只修了一半** → 见 §2 P0-2 |

---

## 2. 本轮复审新确认、且已修的两条（commit `8295964`）

### P0-2　Web 的「strict OOS」开关被 API 静默丢弃（真实、已修）

- `web/src/pages/Backtest.tsx:250-251` 一直 POST `holdout_mode` / `allow_holdout_reuse`；
- `api/backtest_runs.py:65-66,216-218` 一直在消费这两个键；
- 但 `api/main.py:129 BacktestRequest` **没有声明它们**，Pydantic 默认 `extra="ignore"`
  在 API 边界直接丢弃 → 所有 UI 发起的"严格 OOS"回测实际按 `explore` 跑，`allow_reuse` 恒 False。
- 上一轮的测试 `test_oos_params_forwarded_to_engine` 直接调 `_execute`，绕过了模型层，所以没抓到。
- 修复：模型补两字段（默认 `None` / `False`，保持 fail-safe），新增字段保真测试。

### P1-6　h>1 重叠收益折减（M-08）对正常配置是空操作（真实、已修）

- `DEFAULT_FITNESS_CFG["gate_rebalance_bars"] = 1`（`evaluator.py:57`）；
- 旧代码 `horizon = max(1, int(cfg.get("gate_rebalance_bars") or self.gate_rebalance_bars))`
  里 `cfg` 是**合并了默认值**的字典，`cfg.get(...)` 恒为 1（真值），`or` 永不触发；
- 全库 13 份配置只有 `configNN_temporal_10min_template.json` 用 `target.horizon_bars=5`
  且**没有任何配置写 `fitness.gate_rebalance_bars`** → M-08 对它们全部无效；
- 修复：改用构造期已解析的 `self.gate_rebalance_bars`（显式 fitness 键 > target spec），
  并补「只写 target.horizon_bars」路径的回归测试（此前测试恰好都写显式键，掩盖了 bug）。

---

## 3. 全模块复审新发现（未修，按严重度分级）

### P0 —— 会让结论或数据本身失真

**P0-a　`empirical_p` 闸门结构性错误，是当前"0 晋升"的直接原因**（详见 §4，最优先）

**P0-b　下载器"假成功"：落盘失败仍返回 `success: True`**
- `batch_downloader.py:228` 调 `_save_data_with_merge`，`:233-247` 无条件返回 `success: True`，
  完全不看返回的 `save_result['success']`；
- `_save_data_with_merge` 在合并异常时会**静默另存**为 `...-new-<ts>.feather`（`:300-303`），
  主文件没更新、无人读的孤儿文件产生，而调用方以为成功。
- 影响：定时刷新脚本可以连续"成功"而数据一条不更新。

**P0-c　补 gap 会把别的交易所数据写进 binance 目录**
- `data_downloader.py:51-52`：`get_exchange_instance()` 无参调用时把 `self.exchange_id = 'binance'`
  **原地改写**；
- `_download_missing_data()`（`:544`）正是无参调用它 → 之后 `_merge_with_existing_data`
  用 `self.exchange_id` 拼路径（`:371`）→ 非 binance 交易所的补全结果并进 binance 文件。
- 影响：跨交易所数据串味，且是静默的。

**P0-d　未收盘 bar 会被窗口规划器当成"已完成"**
- 实测：`data/binance/futures/BTC_USDT_USDT-1d-futures.feather` 末根为 **2026-11-15**，
  而当前时间 `2026-11-15 10:12 UTC` —— 该 bar 覆盖 [11-15, 11-16)，**尚未收盘**
  （volume 114,623 vs 常态 ~281,000，可交叉验证）。
- `windows.py:common_timeline()`（`:146-183`）只做文件名交集，**没有任何收盘判定**，
  `plan_windows` 据此可把含该 bar 的窗口标成 `ready`，一旦 `reveal` 就烧掉一个 holdout 窗口。
- D-05 的 `_drop_unclosed_last_bar` 只保护挖掘面板，**没有覆盖窗口规划 / freshness / reveal**。
- 影响："每次都刷新到最新"这条纪律本身变成了泄漏源。

**P0-e　NN 挖掘器用测试集选 head**
- `user_workspace/custom_miners/my_custom_nn.py:217-235`：`_score_heads` 在 test split 上
  `evaluate_on`，并把**测试集 metrics** 写回 `head.metrics`（`final_metrics` 来自 test），
  `update_model`/选择按该 `fitness_score` 排序 → 测试集参与搜索，正是 A1 级泄漏，
  核心 evaluator 里已切断，但随仓库分发的 NN 矿工里仍在。

**P0-f　搜索目标事实上≈|IS RankIC|**
- 默认 `turnover_penalty=0`、`shrinkage_k=30`（1187 bar 时 shrink≈0.99）、
  `tradeable_floor` 仅在 LS 亏损时生效、`coverage_floor=0.2` 很少触发
  → `fitness_score ≈ 100 × |RankIC_in|`（`evaluator.py:588`）。
- 也就是说**搜索阶段没有任何成本/换手/稳健性压力**，全部压力留给最后一道闸门。
  这在"闸门可信"时只是低效；在 P0-a 存在时等于把生死完全交给一个坏检验。

### P1 —— 会产生偏差或不可运营

- **P1-a** `significance.py:378` HAC t 的 `p_value` 仍用 `df=n-1`，但 `ls_t` 用的是
  `lags=max(5, hold)` 的 NW 估计，两者 lag 口径不一致，p 值口径需统一。
- **P1-b** `evaluator.py:716` DSR 传 `n_trials=1`，真实试验数在 `apply_promotion_gates`
  里另算；两处口径分离，审计时容易只看 verdict 的 `dsr` 字段误判。
- **P1-c** 治理面：`holdout_mode ∈ {audit, off}` 与 `allow_holdout_reuse=True` 现在
  **可以从 Web 表单直接下发**（P0-2 修复后）。生产环境应硬性禁止，或至少强制写入
  `holdout_access.jsonl` 并告警。
- **P1-d** walk-forward 的 `ridge_alpha=5.0`、`min_train_frac=0.4` 是全局常数，
  没有做每折的标准化单位检查，跨资产量纲差异靠调用方自觉。
- **P1-e** 资金费率在 gate 侧仍是 `mode: constant`（`metrics.cost_identity.funding`），
  只有回测走 `crypto_streams` → 闸门与回测的成本口径不完全同源。
- **P1-f** `annualisation` 已能按日历推断，但 `data_feeds.timeframe` 之外没有把
  "资产类别"写进 manifest，跨类别（crypto 365 vs 股票 252）混跑时靠推断。

### P2 —— 工程整洁度

- `mine.py:112/162` 用 `metrics.get('fitness_score', 0)` 排序，缺失时按 0 参与排序，
  与 `base.py:466` 的 `-1e9` 哨兵不一致。
- `tests/test_cli_smoke.py` 子进程 30s 超时在全量并发下会抖（实测 ~29.8s），已改 90s。
- `runtime` dataset 里仍有 1 个 5m、1 个 1m 文件（819/820 是 1d），命名与清理策略未定。

---

## 4. 核心发现：empirical-null 闸门是错的（实测复现）

### 现象

上一轮真实运行 `run_20261115T052211Z_61ffe5`，71 个候选，`n_promoted=0`。闸门失败分布：

```
dsr_pass             71/71  (100%)   ← 唯一 100% 失败
empirical_null_pass  66/71  ( 93%)
fdr_pass             46/71  ( 65%)
cost_adj_positive    43/71  ( 61%)
hac_t_significant    41/71  ( 58%)
oos_consistent       38/71  ( 54%)
```

其中最强的候选 `fac_4cf127fa`：`hac_t=3.46`、`p_adj=0.0020`（BH 通过）、
`cost_adj_return=0.00021`、**`empirical_p=0.95`（失败）**、`dsr=0.00018`（失败）。

一个 HAC t=3.46 的因子被置换检验判成"比随机还差 95%"，两个口径差三个数量级 —— 必有一个错。

### 复现（用该 run 存下的真实因子值）

```python
F, R = panels of fac_4cf127fa          # 1187 × 20
t_obs = |newey_west_tstat(ic_series(F, R))|              # 3.461  （与 run 一致）
null  = target_permutation_tstats(nan_to_num(F), nan_to_num(R), n_perm=60)  # 时间轴整行置换
|t| 分位: p0=2.79  p50=4.74  p90=5.65  p100=6.05
empirical_p = 0.9508      # 与 run 的 0.95 完全一致
```

**零分布的中位数是 4.74，比观测值 3.46 还大** —— 这不是零分布，它自带了真实 alpha。

### 根因

`target_permutation_tstats()` 置换的是**收益矩阵的时间轴整行**，
每个资产的**无条件漂移（drift）被完整保留**。而 `fac_4cf127fa` 与漂移高度相关：

```
F̄（因子时间均值）× drift 的相关系数 = 0.722
```

于是"随机配对"后的 IC 序列并不以 0 为中心，而是围绕 `corr(F̄, drift)` 这个**真实 edge** 波动，
HAC t 的期望随之被抬到 4.7。观测值只要比"漂移暴露"弱，就会被判成"不如随机"。

补证（同一因子）：

| 零分布方案 | 中位 \|t\| | p95 \|t\| | 对观测 3.60 的结论 |
|---|---|---|---|
| 现状：整行置换 | 4.54–4.74 | 5.65–5.83 | p≈0.95，判死 ❌ |
| 行内去均值后再整行置换 | 4.54 | 5.83 | 仍判死 ❌（只去掉行均值，格点仍带 drift） |
| **行内跨资产置换**（保留每行截面结构，只断因子↔资产映射） | **0.70** | **1.27** | **p≈1/21，显著通过 ✅** |

### 影响与结论

1. `configExperiment.json` 现在开着 `null_n_perm=60` + `empirical_p_max=0.05`，
   而 `empirical_p` 的最小可达值是 `1/61≈0.016`；一旦按上表修正，判定才回到可用区间。
2. **只要该闸门不回退，任何与主流币漂移同向的因子都会被 `empirical_p≈0.2~0.95` 拒掉**
   —— 66/71 的失败率就是这个机制，而不是"没有 alpha"。
3. 因此：**不能再用"0 晋升"证明系统没有 alpha**。修完重跑，才是第一次真实判定。
4. 另需注意：`_build_null_tstats`（`director.py:136`）用 `nan_to_num(...,nan=0.0)` 填充因子面板，
   而观测 HAC t 走 `dropna` 口径 —— 零分布与观测值本就不在同一约定下（注释声称"同一约定"是错的）。
   生产侧应改为保留 NaN 并只在共同有效格点上计算，或统一 fill 策略。

---

## 5. DSR 这一道闸门是**对的**，且是当前最硬的墙

不要因为 DSR 100% 失败就去调低它。实测：

- 最佳候选每 bar 净收益 `0.00039`（约 3.9bp/bar），`cost_adj_return` 中位约 `0.0002`（2bp/bar），
  往返成本 `gate_cost_rate = 2×(4bp+5bp) = 18bp`；
- 即每 bar 净 2–4bp，扣完成本后年化 Sharpe 量级 ≈ 0.3–0.6；
- 71 次试验下 DSR 的期望最大 Sharpe 门槛（n=1187）≈ 每 bar 0.09（年化 ≈1.4）。

结论：**现有信号量级比"71 次试验的运气上限"低一个数量级**。这在数学上没错，说明的是
搜索面太小/信号太弱，而不是闸门太严。要跨过它，必须提高信号量级（更长周期、更多资产、
更强特征），而不是降低门槛。

---

## 6. 离生产挖掘因子还差什么 —— 分四层

### L1　让闸门可信（1–2 天，最高优先，阻塞其它一切）

1. 修 `target_permutation_tstats`：改为**行内跨资产置换**（或对 `returns` 做截面去均值 + 保留行内结构），
   并让零分布与观测值统一 NaN 约定（§4）。
2. 修完立刻用同一份 `factor_db_oos` 重算 71 个候选的 `empirical_p`，与旧值并列写进报告，
   确认判定翻转方向合理。
3. 统一 HAC/`ls_t`/DSR 的 lag 与 `n_trials` 口径（P1-a/P1-b），verdict 里不要留两套试验数。

### L2　提高信号量级（真正的 alpha 瓶颈）

| 现状实测 | 问题 |
|---|---|
| 20 资产、1d、1187 根 bar、`required_streams=[open,high,low,close,volume]` | 截面太小（N=20 的 rank IC 噪声大）；1d 上 18bp 成本吃掉大部分信号 |
| GP 3 代 × 40 群体 = 71 个候选入库 | 搜索预算比 DSR 假设的试验数还小，属于"用一次试验去撞多次试验的门槛" |
| 已下载 818 个 feather（758 合约资金费率）但**只用于回测成本** | funding / OI / basis 这些加密原生边际信息**没有进入搜索空间** |

最小行动集：
1. **把 universe 从 20 扩到 100+**（数据已在本地，`crypto_streams` 也有 758 合约），
   截面 IC 的信噪比随之提升；
2. **把 funding_rate / OI / basis 接成面板字段并进入算子空间**（`data_feeds.crypto_streams`），
   这是 crypto 相对股票唯一的超额信息源；
3. 试验 **4h 或 8h 周期**：同样 18bp 成本下，单 bar 收益幅度更大，成本占比下降；
4. 搜索预算提到 500+ 候选（GP 8–10 代 / 或并行多范式），并让 `n_trials` 真实累计。

### L3　工程健壮性（不修就会"成功但没数据/没结论"）

1. P0-b：`download_data` 必须 `if not save_result.get("success"): return {'success': False, ...}`，
   并**删除/告警** `-new-<ts>` 静默另存分支；
2. P0-c：`get_exchange_instance()` 不得原地改写 `self.exchange_id`（改为局部变量/显式参数）；
3. P0-d：`common_timeline` / `freshness` / `reveal` 统一走"bar 是否已收盘"判定（可复用
   `real_client._drop_unclosed_last_bar` 的逻辑抽到 `data_feed.bars` 公共函数）；
4. P0-e：`my_custom_nn.py` 的 head 选择只能在训练/挖掘 split 上做，test split 只用于
   `evaluate_final` 一次性 OOS；
5. P1-e：闸门侧的资金费率也切到 `crypto_streams`，否则 gate 与 backtest 不同源。

### L4　运营化（能不能"每天产出"）

1. **调度**：目前靠手工 `scripts/run_mine.py`；需要定时任务（先刷新→挖掘→闸门→报告）+
   失败告警（Email/IM），否则"最新数据"纪律靠人肉保证；
2. **数据新鲜度硬门**：`plan_windows` 的 `ready` 必须以"末根 bar 已收盘"为前提（L3-3），
   否则调度再准也会烧窗口；
3. **治理**：生产环境禁 `holdout_mode∈{audit,off}` 与 `allow_holdout_reuse`（P1-c），
   或强制写 `holdout_access.jsonl` + 告警；
4. **可复现**：manifest 已有 `git_sha / config_hash / operator_set_hash / data_snapshot(逐文件 digest) / seed`
   —— 这一层**基本达标**，只需在调度侧把 run 目录归档策略定下来；
5. **上线前**：Docker 仍未实构建（仅 `docker compose config` 校验）；API 必须单 worker；
   Web 回测参数（含本轮新接的 OOS 开关）需要一次端到端验收。

---

## 7. 一句话回答"能生产了吗"

- **能跑、能报错、能拒绝**：是。第 0–2 层修复后，退出码/WS/单位/入场价这些"假成功"问题已清除，
  证据链（manifest / ledger / trace / fdr）齐备。
- **能产出可交易因子**：**不能**。理由不是代码质量，而是三件事同时不成立：
  ① 最关键的 `empirical_p` 闸门实现错误（§4，已复现）；
  ② 搜索面与经济门槛差一个数量级（§5，DSR 100% 失败是真实信号太弱）；
  ③ 加密原生信息（funding/OI/basis）没进搜索空间（§6-L2）。
- **最小可行路径**：先修 §4（1–2 天）→ 扩 universe + 接加密原生字段（§6-L2 的 1、2）→
  扩试验预算 → 重跑一次完整实验。只有到这一步，"0 晋升"才第一次成为关于 alpha 的结论。
