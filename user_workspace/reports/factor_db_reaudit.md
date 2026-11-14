# 因子库全库重审

- 因子总数：**84**，可重审：**84**，跳过：**0**
- DSR 的 `n_trials` 取 **84**（库内因子数，作为搜索预算下界）
- 判决使用与线上完全一致的 `_build_verdict` + `apply_promotion_gates`；
  无 OOS 快照的因子，两道 OOS 闸门按“不适用”处理，而不是默认通过。

| 因子 | 原状态 | 新状态 | 模式 | IS RankIC | OOS RankIC | HAC t | p_adj | DSR | 净收益/期 | 拦截原因 |
|---|---|---|---|---|---|---|---|---|---|---|
| fac_c25e28c3 | INSPECTED | DISCOVERED_UNVERIFIED | panel | -0.0700 | -0.0576 | 3.44 | 0.000584 | 0.000 | -0.000316 | dsr_pass,cost_adj_positive,ls_significant |
| fac_8af952ab | INSPECTED | DISCOVERED_UNVERIFIED | panel | -0.0678 | -0.0555 | 3.52 | 0.000438 | 0.000 | -0.000515 | dsr_pass,cost_adj_positive,ls_significant |
| fac_b66d17ce | INSPECTED | DISCOVERED_UNVERIFIED | panel | -0.0662 | -0.0545 | 3.19 | 0.00142 | 0.004 | +0.000257 | dsr_pass |
| fac_099a7eef | INSPECTED | DISCOVERED_UNVERIFIED | panel | -0.0500 | -0.0254 | 3.69 | 0.000225 | 0.000 | -0.000258 | dsr_pass,cost_adj_positive |
| fac_c0e166ec | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0483 | +0.0000 | 3.56 | 0.000372 | 0.037 | +0.000688 | dsr_pass |
| fac_0452ee64 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0473 | +0.0000 | 3.78 | 0.000155 | 0.056 | +0.000861 | dsr_pass |
| fac_93702eee | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0443 | +0.0000 | 3.12 | 0.00182 | 0.000 | -0.000295 | dsr_pass,cost_adj_positive,ls_significant |
| fac_f20a1349 | INSPECTED | DISCOVERED_UNVERIFIED | panel | -0.0435 | -0.0573 | 3.40 | 0.000673 | 0.000 | -0.000607 | dsr_pass,cost_adj_positive,ls_significant |
| fac_aa433fb6 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0434 | +0.0000 | 3.70 | 0.000219 | 0.001 | -0.000128 | dsr_pass,cost_adj_positive,ls_significant |
| fac_2017455e | INSPECTED | DISCOVERED_UNVERIFIED | panel | -0.0431 | -0.0013 | 3.23 | 0.00123 | 0.000 | -0.000758 | dsr_pass,cost_adj_positive,ls_significant,oos_strength |
| fac_d519ab1f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0426 | +0.0000 | 3.60 | 0.000324 | 0.001 | -0.000091 | dsr_pass,cost_adj_positive,ls_significant |
| fac_9849305f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0425 | +0.0000 | 3.45 | 0.000555 | 0.000 | -0.000449 | dsr_pass,cost_adj_positive,ls_significant |
| fac_b9ea9558 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0410 | +0.0000 | 3.44 | 0.000576 | 0.000 | -0.000886 | dsr_pass,cost_adj_positive,ls_significant |
| fac_7430f062 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0410 | +0.0000 | 3.44 | 0.000576 | 0.000 | -0.000906 | dsr_pass,cost_adj_positive,ls_significant |
| fac_a4693216 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0410 | +0.0000 | 3.76 | 0.00017 | 0.000 | -0.000858 | dsr_pass,cost_adj_positive,ls_significant |
| fac_10e17c69 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0402 | +0.0000 | 3.68 | 0.000237 | 0.000 | -0.000929 | dsr_pass,cost_adj_positive,ls_significant |
| fac_17b454e2 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0400 | +0.0000 | 3.61 | 0.000302 | 0.000 | -0.000954 | dsr_pass,cost_adj_positive,ls_significant |
| fac_45a73eb7 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0400 | +0.0000 | 3.39 | 0.000689 | 0.000 | -0.000466 | dsr_pass,cost_adj_positive,ls_significant |
| fac_e4b5c794 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0396 | +0.0000 | 3.28 | 0.00104 | 0.000 | -0.000413 | dsr_pass,cost_adj_positive,ls_significant |
| fac_6a942af7 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0392 | +0.0000 | 3.33 | 0.000876 | 0.000 | -0.000459 | dsr_pass,cost_adj_positive,ls_significant |
| fac_b09337b6 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0391 | +0.0000 | 3.31 | 0.000927 | 0.000 | -0.000521 | dsr_pass,cost_adj_positive,ls_significant |
| fac_1796332d | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0391 | +0.0000 | 2.76 | 0.00581 | 0.002 | +0.000048 | dsr_pass |
| fac_1fd8f2b5 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0390 | +0.0000 | 3.52 | 0.000433 | 0.000 | -0.000850 | dsr_pass,cost_adj_positive,ls_significant |
| fac_1664907f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0390 | +0.0000 | 3.52 | 0.00044 | 0.000 | -0.000850 | dsr_pass,cost_adj_positive,ls_significant |
| fac_75df0ea2 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0390 | +0.0000 | 3.28 | 0.00103 | 0.000 | -0.000632 | dsr_pass,cost_adj_positive,ls_significant |
| fac_d4eb9579 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0390 | +0.0000 | 3.28 | 0.00103 | 0.000 | -0.000632 | dsr_pass,cost_adj_positive,ls_significant |
| fac_66d326f5 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0387 | +0.0000 | 3.28 | 0.00103 | 0.000 | -0.000579 | dsr_pass,cost_adj_positive,ls_significant |
| fac_cf0e7829 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0386 | +0.0000 | 3.25 | 0.00117 | 0.000 | -0.000612 | dsr_pass,cost_adj_positive,ls_significant |
| fac_8b81f24a | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0385 | +0.0000 | 3.48 | 0.000493 | 0.000 | -0.001273 | dsr_pass,cost_adj_positive,ls_significant |
| fac_e3bea5e6 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0383 | +0.0000 | 3.46 | 0.000549 | 0.000 | -0.000993 | dsr_pass,cost_adj_positive,ls_significant |
| fac_d2f36abf | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0382 | +0.0000 | 3.21 | 0.00131 | 0.000 | -0.000637 | dsr_pass,cost_adj_positive,ls_significant |
| fac_6ecf3d3f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0377 | +0.0000 | 3.33 | 0.000855 | 0.000 | -0.001336 | dsr_pass,cost_adj_positive,ls_significant |
| fac_df68a0dc | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0376 | +0.0000 | 3.43 | 0.000594 | 0.000 | -0.001073 | dsr_pass,cost_adj_positive,ls_significant |
| fac_9485da46 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0375 | +0.0000 | 3.06 | 0.00221 | 0.000 | -0.000665 | dsr_pass,cost_adj_positive,ls_significant |
| fac_dd87a975 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0375 | +0.0000 | 3.17 | 0.00153 | 0.000 | -0.000580 | dsr_pass,cost_adj_positive,ls_significant |
| fac_729391a2 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0371 | +0.0000 | 3.12 | 0.00179 | 0.000 | -0.000573 | dsr_pass,cost_adj_positive,ls_significant |
| fac_a1aef3c9 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0369 | +0.0000 | 2.78 | 0.00535 | 0.000 | -0.000701 | dsr_pass,cost_adj_positive,ls_significant |
| fac_504664c1 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0368 | +0.0000 | 2.85 | 0.00435 | 0.000 | -0.000765 | dsr_pass,cost_adj_positive,ls_significant |
| fac_5c9f121d | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0366 | +0.0000 | 3.00 | 0.00271 | 0.001 | -0.000162 | dsr_pass,cost_adj_positive,ls_significant |
| fac_582a1342 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0365 | +0.0000 | 3.02 | 0.00255 | 0.000 | -0.000638 | dsr_pass,cost_adj_positive,ls_significant |
| fac_380a1000 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0365 | +0.0000 | 2.66 | 0.00785 | 0.000 | -0.000769 | dsr_pass,cost_adj_positive,ls_significant |
| fac_924f3f3c | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0363 | +0.0000 | 2.65 | 0.00813 | 0.002 | +0.000072 | dsr_pass |
| fac_657ee061 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0362 | +0.0000 | 3.14 | 0.00171 | 0.000 | -0.001011 | dsr_pass,cost_adj_positive,ls_significant |
| fac_983c0910 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0360 | +0.0000 | 2.99 | 0.00281 | 0.000 | -0.000662 | dsr_pass,cost_adj_positive,ls_significant |
| fac_7f9b208e | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0357 | +0.0000 | 2.93 | 0.00342 | 0.001 | -0.000124 | dsr_pass,cost_adj_positive,ls_significant |
| fac_9e86ea01 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0356 | +0.0000 | 3.18 | 0.00148 | 0.000 | -0.001101 | dsr_pass,cost_adj_positive,ls_significant |
| fac_29d31826 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0350 | +0.0000 | 3.17 | 0.00151 | 0.000 | -0.001035 | dsr_pass,cost_adj_positive,ls_significant |
| fac_5a7cfae9 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0349 | +0.0000 | 3.15 | 0.00166 | 0.000 | -0.001142 | dsr_pass,cost_adj_positive,ls_significant |
| fac_478ba7bc | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0343 | +0.0000 | 2.72 | 0.00648 | 0.000 | -0.000825 | dsr_pass,cost_adj_positive,ls_significant |
| fac_98e70178 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0333 | +0.0000 | 2.38 | 0.0174 | 0.000 | -0.000520 | dsr_pass,cost_adj_positive,ls_significant |
| fac_ce249d7f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0321 | +0.0000 | 2.28 | 0.0224 | 0.000 | -0.000255 | dsr_pass,cost_adj_positive,ls_significant |
| fac_30adaf6c | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0318 | +0.0000 | 2.83 | 0.0046 | 0.000 | -0.000752 | dsr_pass,cost_adj_positive,ls_significant |
| fac_2e4c1650 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0318 | +0.0000 | 2.30 | 0.0212 | 0.001 | -0.000209 | dsr_pass,cost_adj_positive,ls_significant |
| fac_115ee998 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0315 | +0.0000 | 1.66 | 0.0971 | 0.000 | -0.001033 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_6142ff09 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0312 | +0.0000 | 2.25 | 0.0246 | 0.003 | +0.000105 | dsr_pass |
| fac_35ca856d | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0311 | +0.0000 | 2.62 | 0.00882 | 0.000 | -0.000567 | dsr_pass,cost_adj_positive,ls_significant |
| fac_5fcf1e5a | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0307 | +0.0000 | 2.28 | 0.0226 | 0.000 | -0.000355 | dsr_pass,cost_adj_positive |
| fac_a2fcbcc9 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0288 | +0.0000 | 2.35 | 0.0187 | 0.000 | -0.000428 | dsr_pass,cost_adj_positive |
| fac_14caba6f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0282 | +0.0000 | 1.98 | 0.0476 | 0.000 | -0.000233 | hac_t_significant,dsr_pass,cost_adj_positive |
| fac_2b41dc8a | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0276 | +0.0000 | 2.38 | 0.0172 | 0.000 | -0.001145 | dsr_pass,cost_adj_positive,ls_significant |
| fac_7a34cc77 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0263 | +0.0000 | 1.93 | 0.053 | 0.000 | -0.000922 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_7d35d4bf | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0241 | +0.0000 | 1.57 | 0.118 | 0.000 | -0.000506 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_9f134276 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0232 | +0.0000 | 1.71 | 0.0866 | 0.000 | -0.000216 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_1e6b47e4 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0185 | +0.0000 | 1.51 | 0.132 | 0.003 | +0.000119 | hac_t_significant,fdr_pass,dsr_pass |
| fac_74cd05ea | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0177 | +0.0000 | 1.41 | 0.157 | 0.000 | -0.000237 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_40b72386 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0175 | +0.0000 | 1.42 | 0.155 | 0.000 | -0.000900 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_0cde8952 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0163 | +0.0000 | 1.19 | 0.233 | 0.000 | -0.000917 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_2dd92785 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0143 | +0.0000 | 1.14 | 0.256 | 0.001 | -0.000175 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_6b4244cc | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0114 | +0.0000 | 0.82 | 0.413 | 0.001 | -0.000105 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_fd8eaa92 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0091 | +0.0000 | 0.63 | 0.529 | 0.000 | -0.000577 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_8a1fd732 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0078 | +0.0000 | 0.68 | 0.495 | 0.002 | +0.000006 | hac_t_significant,fdr_pass,dsr_pass |
| fac_1162a04e | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0070 | +0.0000 | 0.56 | 0.572 | 0.000 | -0.000398 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_65fb1710 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0065 | +0.0000 | 0.37 | 0.713 | 0.002 | +0.000087 | hac_t_significant,fdr_pass,dsr_pass |
| fac_08924ba4 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0059 | +0.0000 | 0.20 | 0.84 | 0.001 | +0.000005 | hac_t_significant,fdr_pass,dsr_pass |
| fac_8603f13a | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0051 | +0.0000 | 0.39 | 0.695 | 0.000 | -0.000656 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_fd338d02 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | -0.0048 | +0.0000 | 0.26 | 0.798 | 0.000 | -0.001113 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_e50b6ce4 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0043 | +0.0000 | 0.33 | 0.738 | 0.000 | -0.000436 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_c76fcda1 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0035 | +0.0000 | 0.32 | 0.745 | 0.009 | +0.000375 | hac_t_significant,fdr_pass,dsr_pass |
| fac_b9920d73 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0032 | +0.0000 | 0.24 | 0.809 | 0.006 | +0.000288 | hac_t_significant,fdr_pass,dsr_pass |
| fac_5e6fadce | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0032 | +0.0000 | 0.34 | 0.737 | 0.000 | -0.000211 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive,ls_significant |
| fac_f832720b | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0007 | +0.0000 | 0.09 | 0.926 | 0.000 | -0.000291 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_30710a28 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0000 | +0.0000 | 1.59 | 0.112 | 0.000 | +0.000000 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_3f1d483f | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0000 | +0.0000 | 1.62 | 0.106 | 0.000 | +0.000000 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |
| fac_af41c257 | DISCOVERED | DISCOVERED_UNVERIFIED | panel | +0.0000 | +0.0000 | 1.62 | 0.106 | 0.000 | +0.000000 | hac_t_significant,fdr_pass,dsr_pass,cost_adj_positive |

## 结论
- 通过闸门：**0** / 84。
- 拦截原因以 `dsr_pass` 为主：库内因子都是在没有搜索预算记录的情况下挖出来的，按 84 次试验折减后，策略 Sharpe 普遍达不到多重检验的门槛。
- 这是预期结果：闸门的作用不是保留存量，而是阻止不合格因子继续占用 `INSPECTED`。

## 分母敏感性

`n_trials` 是多重检验的分母。84 个因子高度相关，真实独立试验数应小于 84，但没有人记录过当时的搜索预算，所以下面给出不同分母下的通过率，用于判断结论是否被分母单独决定。

| n_trials | 通过数 |
|---|---|
| 84 | 0 / 84 |
| 20 | 0 / 84 |
| 6 | 0 / 84 |
| 1 | 1 / 84 |

即使把分母降到 1（完全不做多重检验），通过数也只有 1 个 —— 拦住它们的并不只是 DSR，还有成本后为负与 `ls_t` 不显著。
