# 加密原生特征库验证报告

> 数据来源：**真实交易所数据**
> 标的池：20 个（ADA, APT, ARB, ATOM, AVAX, BNB, BTC, DOGE, DOT, ETC, ETH, FIL …）
> 区间：2023-01-01 → 2026-11-13，窗口阶梯 [12, 24, 48]

## 1. 数据与流覆盖

| 流 | 非空观测数（跨标的总和） |
|---|---|
| `funding_rate` | 28,209 |
| `open_interest` | 480 |
| `basis` | 28,210 |
| `long_liquidations` | — (未提供) |
| `short_liquidations` | — (未提供) |

> 说明：Binance 公共接口的 funding 可长期回溯；open interest 仅保留最近约 30 天，因此 OI 类特征的有效样本明显短于价格类；liquidation 无免费接口，需自备数据文件。

## 2. 特征库规模

- 模板数：29（funding / open_interest / basis / liquidation / interaction）
- 本次可用 AST 数（模板 × 窗口）：56
- 最小每期资产数：5
- 样本不足（有效 IC 期数 < 30）而排除出排名的特征：25 个（多为 OI/爆仓类，受数据保留期限制）

## 3. 单特征横截面 RankIC（按 |mean RankIC| 排序）

> 多重检验：本次共评估 56 个特征（即 56 次试验），BH-FDR(q≤0.05) 通过 **0** 个。单看原始 t 值会高估显著性，闸门以 q 值为准；同一模板不同窗口高度相关，BH 在正相关下偏保守——即便如此仍无一通过。

| 排名 | 类别 | 窗口 | 公式 | mean RankIC | ICIR | NW t | BH q | 有效期数 |
|---|---|---|---|---|---|---|---|---|
| 1 | basis | 48 | `ts_rank(basis)` | +0.0178 | +0.076 | +2.79 | 0.197 | 1358 |
| 2 | basis | 24 | `ts_rank(basis)` | +0.0161 | +0.068 | +2.60 | 0.197 | 1370 |
| 3 | basis | 48 | `div(ts_zscore(basis), add(abs(ts_realized_vol(close)), 1e-09))` | -0.0158 | -0.063 | -2.21 | 0.197 | 1333 |
| 4 | basis | 12 | `ts_rank(basis)` | +0.0154 | +0.064 | +2.37 | 0.197 | 1376 |
| 5 | funding | 12 | `sign(funding_rate)` | -0.0153 | -0.062 | -2.51 | 0.197 | 1256 |
| 6 | basis | 48 | `ts_zscore(basis)` | -0.0147 | -0.061 | -2.24 | 0.197 | 1358 |
| 7 | basis | 24 | `ts_zscore(basis)` | -0.0141 | -0.060 | -2.19 | 0.197 | 1370 |
| 8 | funding | 24 | `ts_rank(funding_rate)` | +0.0141 | +0.056 | +2.05 | 0.217 | 1359 |
| 9 | funding | 48 | `ts_rank(funding_rate)` | +0.0139 | +0.055 | +2.06 | 0.217 | 1352 |
| 10 | basis | 12 | `div(ts_delta(basis), add(abs(close), 1e-09))` | -0.0132 | -0.055 | -2.03 | 0.217 | 1363 |
| 11 | funding | 12 | `ts_delta(funding_rate)` | -0.0124 | -0.049 | -1.76 | 0.293 | 1376 |
| 12 | basis | 24 | `div(ts_delta(basis), add(abs(close), 1e-09))` | -0.0124 | -0.049 | -1.76 | 0.293 | 1345 |
| 13 | basis | 24 | `div(ts_zscore(basis), add(abs(ts_realized_vol(close)), 1e-09))` | -0.0123 | -0.051 | -1.76 | 0.293 | 1357 |
| 14 | funding | 48 | `sub(funding_rate, ts_ema(funding_rate))` | -0.0116 | -0.045 | -1.68 | 0.322 | 1380 |
| 15 | basis | 12 | `ts_zscore(basis)` | -0.0109 | -0.046 | -1.64 | 0.332 | 1376 |
| 16 | funding | 12 | `ts_rank(funding_rate)` | +0.0108 | +0.043 | +1.52 | 0.359 | 1364 |
| 17 | funding | 24 | `sub(funding_rate, ts_ema(funding_rate))` | -0.0108 | -0.042 | -1.56 | 0.359 | 1380 |
| 18 | funding | 12 | `div(ts_delta(funding_rate), add(abs(funding_rate), 1e-09))` | -0.0100 | -0.041 | -1.43 | 0.389 | 1376 |
| 19 | funding | 12 | `sub(funding_rate, ts_ema(funding_rate))` | -0.0099 | -0.039 | -1.40 | 0.389 | 1380 |
| 20 | funding | 24 | `ts_zscore(funding_rate)` | -0.0091 | -0.037 | -1.40 | 0.389 | 1370 |

## 4. 分类汇总

| 类别 | 特征数 | 平均 |RankIC| | 最大 |RankIC| | 平均 ICIR |
|---|---|---|---|---|
| basis | 16 | 0.0100 | 0.0178 | 0.041 |
| funding | 15 | 0.0094 | 0.0153 | 0.037 |

> 未纳入汇总的类别：interaction, liquidation, open_interest —— 有效 IC 期数不足 30（数据保留期或流未提供）。

## 5. GP 挖矿接入检查

- 自动识别的加密流 terminals：funding_rate, open_interest, basis
- 初始种群中加密特征播种数：14 / 20

## 6. 结论与边界

- 该报告验证的是**加密原生流的接入、对齐与特征可评估性**；未计交易成本、未做样本外切分，因此表中 RankIC 是"第一眼读数"，不是可上线结论。
- 因子要进入 `INSPECTED` 仍需通过 Phase 1 的统计闸门（BH-FDR / DSR / OOS 强度）。
- 备注：加密 7x24 连续、无涨跌停，但资产池随上市时间变化；横截面 IC 只在当期有数据的标的上计算，已通过 `min_assets` 过滤极窄截面。

## 7. 端到端 Director 运行（真实加密流）

- 标的数：20；迭代 2 轮 × 种群 10；cross-asset；1d
- 数据配置：`crypto_streams=[funding_rate, basis, open_interest]`（GP 自动扩充 terminals）
- 返回候选：4；生命周期分布：{'DISCOVERED_UNVERIFIED': 3, 'INSPECTED': 1}

| fitness | RankIC | 表达式 |
|---|---|---|
| 5.0404 | 0.0445 | `abs(mul(basis, close))` |
| 4.9652 | -0.0411 | `cs_rank(ts_var(volume))` |
| 2.3882 | -0.0180 | `ts_rsi(cs_rank(funding_rate))` |

