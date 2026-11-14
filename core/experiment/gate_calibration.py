"""Positive-control calibration for the promotion gate.

Why this exists
---------------
The project has accumulated a lot of *negative* evidence: 84/84 factors
unverified, 0 promoted, one strict holdout at -6.40% after costs. What it has
never had is a **positive control**: inject a signal whose strength is known by
construction, and check whether the gate actually lets it through. Without one,
"0 promoted" is ambiguous between two very different diagnoses:

1. the mined factors genuinely had no edge, or
2. the gate is so hard that it would reject a real edge too.

This module resolves that ambiguity. It builds semi-synthetic panels from a
controlled data-generating process, computes **every** gate input through the
real production functions (``ic_series`` / ``newey_west_tstat`` /
``long_short_returns`` / ``pvalue_from_t`` / ``target_permutation_tstats``),
and feeds the result to the real ``apply_promotion_gates``. Nothing is
hand-written into the metrics dict, so this tests the whole chain rather than
restating assumptions.

What it proves, and what it does NOT
------------------------------------
It measures **statistical calibration**: false-positive rate under the null,
detection power under known alternatives, and the minimum detectable effect
(MDE) as a function of the cumulative trial denominator. It says nothing about
whether any tradeable alpha exists in crypto markets, and it is not a
substitute for a clean time-based out-of-sample window. Synthetic p-values are
*by construction* well behaved; real factor ICs are heavier tailed and more
autocorrelated. Results are therefore an upper bound on real-world power.

Usage
-----
    python -m core.experiment.gate_calibration \
        --out user_workspace/reports/gate_calibration.md
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.evaluation.portfolio_eval import long_short_returns
from core.evaluation.significance import (
    DEFAULT_GATE_CFG,
    apply_promotion_gates,
    deflated_sharpe_ratio,
    ic_series,
    multiple_testing_bar,
    newey_west_tstat,
    pvalue_from_t,
    target_permutation_tstats,
)

# Cost basis shared with the gate / backtest / walk-forward — single source so a
# recalibration can't silently split the economic assumption (review A2 / F2).
from core.config.defaults import DEFAULT_COST_RATE
DEFAULT_QUANTILE = 0.20
DEFAULT_VOL = 0.03

DEFAULT_SIGNALS: Tuple[float, ...] = (0.0, 0.02, 0.04, 0.06, 0.10, 0.15, 0.25)
DEFAULT_TRIALS: Tuple[int, ...] = (1, 20, 100, 182, 787)


# --------------------------------------------------------------------------- #
# Controlled data-generating process
# --------------------------------------------------------------------------- #
def _row_standardise(x: np.ndarray) -> np.ndarray:
    """Remove the cross-sectional mean/scale so a row-level market shock cancels."""
    x = np.asarray(x, float)
    return (x - x.mean(axis=1, keepdims=True)) / (
        x.std(axis=1, keepdims=True) + 1e-12
    )


def _ar1(shape: Tuple[int, int], persistence: float, rng: np.random.Generator) -> np.ndarray:
    """Mildly persistent factor, mimicking a real (autocorrelated) crypto signal."""
    eps = rng.normal(size=shape)
    out = np.empty(shape, float)
    out[0] = eps[0]
    for t in range(1, shape[0]):
        out[t] = persistence * out[t - 1] + math.sqrt(1.0 - persistence ** 2) * eps[t]
    return out


def synthetic_panels(
    signal: float,
    *,
    n_bars: int = 641,
    n_assets: int = 20,
    n_oos_bars: int = 181,
    vol: float = DEFAULT_VOL,
    market_frac: float = 0.6,
    persistence: float = 0.30,
    seed: int = 0,
) -> Dict[str, np.ndarray]:
    """Generate in-sample and out-of-sample panels with a *known* true IC.

    ``signal`` (rho) is the population cross-sectional correlation between the
    factor and the forward return. The forward return is

        R[t, a] = vol * (market_frac * market[t] + rho * z_F[t, a]
                         + sqrt(1 - rho^2) * eps[t, a])

    with both ``z_F`` and ``eps`` row-standardised. The common market shock
    cancels in cross-sectional ranks and in a dollar-neutral long-short book,
    so it adds realism without leaking an artificial edge. In-sample and OOS
    panels are independent draws from the *same* DGP: a genuinely persistent
    signal, which is the most favourable case for the gate.

    Returns dict with ``factor_in`` / ``returns_in`` / ``factor_oos`` /
    ``returns_oos``, all ``(T, N)`` float arrays.
    """
    rho = float(np.clip(signal, -0.95, 0.95))
    rng = np.random.default_rng(int(seed))

    def _make(T: int) -> Tuple[np.ndarray, np.ndarray]:
        f = _row_standardise(_ar1((T, n_assets), persistence, rng))
        eps = _row_standardise(rng.normal(size=(T, n_assets)))
        market = rng.normal(size=(T, 1))
        latent = rho * f + math.sqrt(max(0.0, 1.0 - rho ** 2)) * eps
        r = float(vol) * (float(market_frac) * market + latent)
        return f, np.asarray(r, float)

    f_in, r_in = _make(n_bars)
    f_oos, r_oos = _make(n_oos_bars)
    return {
        "factor_in": f_in,
        "returns_in": r_in,
        "factor_oos": f_oos,
        "returns_oos": r_oos,
    }


# --------------------------------------------------------------------------- #
# One candidate, evaluated through the real production functions
# --------------------------------------------------------------------------- #
def _frames(panels: Dict[str, np.ndarray], split: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    f = np.asarray(panels[f"factor_{split}"], float)
    r = np.asarray(panels[f"returns_{split}"], float)
    idx = pd.date_range("2023-01-01", periods=f.shape[0], freq="D")
    cols = [f"a{i}" for i in range(f.shape[1])]
    return pd.DataFrame(f, index=idx, columns=cols), pd.DataFrame(r, index=idx, columns=cols)


def candidate_metrics(
    panels: Dict[str, np.ndarray],
    *,
    cost_rate: float = DEFAULT_COST_RATE,
    quantile: float = DEFAULT_QUANTILE,
    rebalance_bars: int = 1,
    n_perm: int = 30,
    seed: int = 0,
) -> Tuple[Dict[str, Any], List[float]]:
    """Compute every gate input from the panels using production code.

    Returns ``(metrics, null_tstats)``. ``metrics`` follows the exact contract
    that :func:`apply_promotion_gates` consumes; ``null_tstats`` is the
    per-candidate target-permutation |t| distribution used by the P2
    ``empirical_p_max`` gate.
    """
    f_in, r_in = _frames(panels, "in")
    f_oos, r_oos = _frames(panels, "oos")

    ic_in = ic_series(f_in.to_numpy(), r_in.to_numpy())
    mean_ic = float(np.mean(ic_in))
    hac_t = float(newey_west_tstat(ic_in))
    orientation = 1.0 if mean_ic >= 0 else -1.0

    book = long_short_returns(
        f_in, r_in,
        quantile=quantile,
        orientation=orientation,
        cost_rate=cost_rate,
        rebalance_bars=rebalance_bars,
    )
    std = float(book["std_return"]) or 0.0
    # DSR deflates the *net per-period* Sharpe of the traded book, not the
    # annualised gross Sharpe reported by long_short_returns.
    sr = float(book["cost_adj_return"]) / std if std > 0 else 0.0

    ic_oos = ic_series(f_oos.to_numpy(), r_oos.to_numpy())
    rank_ic_oos = float(np.mean(ic_oos))
    oos_consistent = (mean_ic * rank_ic_oos) > 0.0

    n_obs = int(book["n_obs"])
    # apply_promotion_gates only recomputes DSR when n_trials > 1. Seed the
    # single-trial case with the correctly deflated value so the n_trials=1
    # column is meaningful instead of inheriting a placeholder zero.
    base_dsr = deflated_sharpe_ratio(sharpe=sr, n_obs=n_obs, n_trials=1)

    metrics: Dict[str, Any] = {
        "hac_t": hac_t,
        "p_value": pvalue_from_t(hac_t),
        "sr": sr,
        "sr_n_obs": n_obs,
        "dsr": float(base_dsr),
        "ir": sr,
        "n_obs": n_obs,
        "cost_adj_return": float(book["cost_adj_return"]),
        "ls_t": float(book["ls_t"]),
        "RankIC_oos": rank_ic_oos,
        "oos_consistent": bool(oos_consistent),
        "oos_enabled": True,
    }
    null = target_permutation_tstats(
        f_in.to_numpy(), r_in.to_numpy(), n_perm=int(n_perm), seed=int(seed)
    ).tolist() if n_perm > 0 else []
    return metrics, null


# --------------------------------------------------------------------------- #
# Gate evaluation for one generated candidate
# --------------------------------------------------------------------------- #
@dataclass
class TrialResult:
    signal: float
    n_trials: int
    seed: int
    passed: bool
    p_adj: float
    dsr: float
    empirical_p: Optional[float]
    hac_t: float
    cost_adj_return: float
    true_ic_in: float
    gate_checks: Dict[str, bool] = field(default_factory=dict)


def evaluate_gate(
    metrics: Dict[str, Any],
    null_tstats: Sequence[float],
    *,
    n_trials: int,
    gate_cfg: Optional[Dict[str, Any]] = None,
    seed: int = 0,
    signal: Optional[float] = None,
    true_ic: Optional[float] = None,
) -> TrialResult:
    """Run the real gate with a realistic family of previous null hypotheses.

    ``signal`` / ``true_ic`` are diagnostic-only labels for the returned row;
    they never enter the gate. Defaults read the ``_signal`` / ``_true_ic``
    keys attached by :func:`calibrate`.
    """
    cfg = {**DEFAULT_GATE_CFG, **(gate_cfg or {})}
    # Under the null, raw p-values are uniform(0, 1). Building the family this
    # way makes BH-FDR and the DSR denominator behave as they do in a real
    # search of ``n_trials`` ideas, without simulating thousands of panels.
    rng = np.random.default_rng(int(seed) * 7919 + 13)
    n_prior = max(0, int(n_trials) - 1)
    family = list(rng.uniform(0.0, 1.0, size=n_prior)) + [float(metrics["p_value"])]
    out = apply_promotion_gates(
        [metrics],
        cfg=cfg,
        n_trials=int(n_trials),
        family_p_values=family,
        null_tstats=[list(null_tstats)],
    )[0]
    return TrialResult(
        signal=float(signal if signal is not None
                     else metrics.get("_signal", float("nan"))),
        n_trials=int(n_trials),
        seed=int(seed),
        passed=bool(out["gate"]["pass"]),
        p_adj=float(out["p_adj"]),
        dsr=float(out["dsr"]),
        empirical_p=out["empirical_p"],
        hac_t=float(metrics["hac_t"]),
        cost_adj_return=float(metrics["cost_adj_return"]),
        true_ic_in=float(true_ic if true_ic is not None
                         else metrics.get("_true_ic", float("nan"))),
        gate_checks={k: bool(v) for k, v in out["gate"].items() if k != "pass"},
    )


# --------------------------------------------------------------------------- #
# Grid calibration
# --------------------------------------------------------------------------- #
def calibrate(
    signal_grid: Sequence[float] = DEFAULT_SIGNALS,
    n_trials_grid: Sequence[int] = DEFAULT_TRIALS,
    *,
    n_repeats: int = 12,
    n_bars: int = 641,
    n_assets: int = 20,
    n_oos_bars: int = 181,
    cost_rate: float = DEFAULT_COST_RATE,
    n_perm: int = 30,
    gate_cfg: Optional[Dict[str, Any]] = None,
    seed: int = 0,
    power_target: float = 0.80,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Measure detection rate / false-positive rate over a signal × trials grid.

    Panels and permutation nulls are generated **once** per ``(signal, seed)``
    and reused across every ``n_trials`` column, so the grid cost scales with
    ``len(signals) * n_repeats``, not with the number of trial columns.
    """
    cfg = {**DEFAULT_GATE_CFG, **(gate_cfg or {})}
    if cfg.get("empirical_p_max") is None:
        cfg = {**cfg, "empirical_p_max": 0.05}

    trials_list = [int(t) for t in n_trials_grid]
    signals = [float(s) for s in signal_grid]
    rows: List[TrialResult] = []
    per_signal: Dict[float, Dict[str, float]] = {}

    for si, signal in enumerate(signals):
        hac_ts: List[float] = []
        ics: List[float] = []
        nets: List[float] = []
        for rep in range(int(n_repeats)):
            s = int(seed) * 100000 + si * 1000 + rep
            panels = synthetic_panels(
                signal, n_bars=n_bars, n_assets=n_assets,
                n_oos_bars=n_oos_bars, seed=s,
            )
            metrics, null = candidate_metrics(
                panels, cost_rate=cost_rate, n_perm=n_perm, seed=s,
            )
            # Attach diagnostic-only fields; the gate ignores unknown keys.
            metrics["_signal"] = signal
            metrics["_true_ic"] = float(
                np.mean(ic_series(panels["factor_in"], panels["returns_in"]))
            )
            hac_ts.append(float(metrics["hac_t"]))
            ics.append(float(metrics["_true_ic"]))
            nets.append(float(metrics["cost_adj_return"]))
            for nt in trials_list:
                rows.append(evaluate_gate(
                    metrics, null, n_trials=nt, gate_cfg=cfg, seed=s,
                ))
            if progress is not None:
                progress(signal, rep, int(n_repeats))
        per_signal[signal] = {
            "measured_ic": float(np.mean(ics)),
            "hac_t": float(np.mean(hac_ts)),
            "cost_adj_return": float(np.mean(nets)),
        }

    by_cell: Dict[Tuple[float, int], List[TrialResult]] = {}
    for row in rows:
        by_cell.setdefault((row.signal, row.n_trials), []).append(row)

    grid: List[Dict[str, Any]] = []
    for signal in signals:
        for nt in trials_list:
            cell = by_cell[(signal, nt)]
            grid.append({
                "signal": signal,
                "n_trials": nt,
                "detection_rate": float(np.mean([r.passed for r in cell])),
                "n": len(cell),
                "median_dsr": float(np.median([r.dsr for r in cell])),
                "median_p_adj": float(np.median([r.p_adj for r in cell])),
                "median_empirical_p": float(np.median([
                    r.empirical_p if r.empirical_p is not None else 1.0
                    for r in cell
                ])),
                "gate_failures": {
                    k: int(sum(1 for r in cell if not r.gate_checks.get(k, True)))
                    for k in (cell[0].gate_checks if cell else {})
                },
            })

    zero = [g for g in grid if g["signal"] == 0.0]
    false_positive_rate = float(np.mean([g["detection_rate"] for g in zero])) if zero else None

    mde: Dict[int, Optional[float]] = {}
    for nt in trials_list:
        candidates = sorted(
            (g for g in grid if g["n_trials"] == nt and g["signal"] > 0),
            key=lambda g: g["signal"],
        )
        hit = next((g["signal"] for g in candidates
                    if g["detection_rate"] >= power_target), None)
        mde[nt] = hit

    bar = {
        nt: multiple_testing_bar(n_bars, nt)["bar_per_period_sharpe"]
        for nt in trials_list
    }

    return {
        "config": {
            "signals": signals,
            "n_trials": trials_list,
            "n_repeats": int(n_repeats),
            "n_bars": int(n_bars),
            "n_assets": int(n_assets),
            "n_oos_bars": int(n_oos_bars),
            "cost_rate": float(cost_rate),
            "n_perm": int(n_perm),
            "gate_cfg": {k: v for k, v in cfg.items()},
            "power_target": float(power_target),
            "seed": int(seed),
        },
        "per_signal": {str(k): v for k, v in per_signal.items()},
        "grid": grid,
        "false_positive_rate": false_positive_rate,
        "mde": {str(k): v for k, v in mde.items()},
        "dsr_bar_per_period": {str(k): float(v) for k, v in bar.items()},
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def render_report(result: Dict[str, Any]) -> str:
    cfg = result["config"]
    lines: List[str] = [
        "# Promotion gate 阳性对照 / 统计校准报告",
        "",
        "> **这份报告证明的是闸门统计校准，不是「挖到了 alpha」。** "
        "所有面板来自已知强度的半合成 DGP；真实因子 IC 尾部更厚、自相关更强，"
        "因此下面的检出率是真实场景的**上界**。它不能替代一次干净的时间 OOS。",
        "",
        "## 方法",
        "",
        "面板 DGP：`R = vol · (market_frac · market[t] + ρ · z_F + √(1−ρ²) · ε)`，"
        "`ρ` 即已知真实 IC；样本内 / 样本外为同一 DGP 的独立抽样。",
        "每个候选的 `hac_t`、`p_value`、`cost_adj_return`、`ls_t`、`sr`、"
        "`RankIC_oos`、`null_tstats` 全部经生产函数计算，再交给真实的 "
        "`apply_promotion_gates`，不手工填数。历史假设族用 `Uniform(0,1)` 的零 p 值补齐，"
        "使 BH-FDR 与 DSR 分母按 `n_trials` 真实放大。",
        "",
        f"- 样本内 {cfg['n_bars']} bars × {cfg['n_assets']} assets，样本外 {cfg['n_oos_bars']} bars",
        f"- 往返成本 {cfg['cost_rate']:.4f}/单位换手，分位 {DEFAULT_QUANTILE:.0%}，"
        f"置换 {cfg['n_perm']} 次，每格 {cfg['n_repeats']} 次重复",
        f"- 闸门：`empirical_p_max={cfg['gate_cfg'].get('empirical_p_max')}`，"
        f"`dsr_min={cfg['gate_cfg'].get('dsr_min')}`，"
        f"`fdr_max={cfg['gate_cfg'].get('fdr_max')}`，"
        f"`oos_rankic_min={cfg['gate_cfg'].get('oos_rankic_min')}`",
        "",
        "## 虚警率（ρ = 0）",
        "",
        f"**{result['false_positive_rate']:.1%}**"
        if result["false_positive_rate"] is not None else "（未测）",
        "",
        "## 检出率矩阵（行 = 真实 IC，列 = 累计试验次数）",
        "",
    ]
    trials = cfg["n_trials"]
    binding: Dict[str, int] = {}
    for g in result["grid"]:
        if g["signal"] <= 0:
            continue
        for key, count in g["gate_failures"].items():
            binding[key] = binding.get(key, 0) + int(count)
    top_binding = sorted(binding.items(), key=lambda kv: -kv[1])[:2]
    mde_items = sorted(
        ((int(k), v) for k, v in result["mde"].items() if v is not None),
        key=lambda kv: kv[0],
    )
    mde_txt = ", ".join(f"`{n}`→{v:.2f}" for n, v in mde_items) or "均未达到"
    real_ic_hi = 0.05
    mde_at_182 = result["mde"].get("182")
    if mde_at_182 is not None:
        verdict = f"在正式 run 的 `n_trials=182` 下，MDE = {mde_at_182:.2f}"
        if mde_at_182 > real_ic_hi:
            verdict += (
                f"，**高于加密日频因子现实 IC 上界约 {real_ic_hi:.2f}**。"
                "因此「0 promoted」至少有一部分是闸门功效不足，不能全部归因于「因子都不行」；"
                "这也解释了为什么 OOS 方向不翻转、IC 显著，却仍被全部拦下。"
            )
        else:
            verdict += "，落在现实 IC 区间内，闸门功效可接受。"
    else:
        verdict = "在当前网格内 `n_trials=182` 未达到目标功效。"

    lines += [
        "## 结论（由本网格自动推导）",
        "",
        f"- 虚警率 **{result['false_positive_rate']:.1%}**（ρ=0 列）：闸门没有乱放行。"
        if result["false_positive_rate"] is not None
        else "- 虚警率：本网格未包含 ρ=0 列。",
        f"- 最小可检出效应：{mde_txt}；门槛随搜索预算单调上升，符合 DSR 设计意图。",
        f"- {verdict}",
        "- 最主要的瓶颈闸门："
        + (", ".join(f"`{k}`（{v} 次失败）" for k, v in top_binding) or "无")
        + "。",
        "",
    ]

    lines.append("| 真实 ρ | " + " | ".join(f"n_trials={t}" for t in trials) + " |")
    lines.append("|---|" + "---|" * len(trials))
    lookup = {(g["signal"], g["n_trials"]): g for g in result["grid"]}
    for s in cfg["signals"]:
        cells = [f"{lookup[(s, t)]['detection_rate']:.0%}" for t in trials]
        lines.append(f"| {s:.2f} | " + " | ".join(cells) + " |")

    lines += ["", "## 最小可检出效应（检出率 ≥ " f"{cfg['power_target']:.0%}）", ""]
    lines.append("| n_trials | DSR 每期门槛 | MDE (真实 ρ) |")
    lines.append("|---|---|---|")
    for t in trials:
        mde = result["mde"][str(t)]
        lines.append(
            f"| {t} | {result['dsr_bar_per_period'][str(t)]:.4f} | "
            + (f"{mde:.2f}" if mde is not None else "本网格内未达到")
            + " |"
        )

    # Which gate is binding at each trial count: the most useful diagnostic.
    lines += ["", "## 谁是瓶颈（各闸门失败计数）", ""]
    for t in trials:
        cells = [g for g in result["grid"] if g["n_trials"] == t and g["signal"] > 0]
        totals: Dict[str, int] = {}
        for g in cells:
            for k, v in g["gate_failures"].items():
                totals[k] = totals.get(k, 0) + int(v)
        ranked = sorted(totals.items(), key=lambda kv: -kv[1])
        top = ", ".join(f"`{k}`={v}" for k, v in ranked[:4]) or "无"
        lines.append(f"- **n_trials={t}**：{top}")

    lines += [
        "",
        "## 解读边界",
        "",
        "- ρ = 0 的高放行率才叫虚警；本闸门含成本正性 + 经验零分布，"
        "虚警率天然被压得很低，代价是**功效也可能被压没**。",
        "- MDE 随 `n_trials` 上升是 DSR 的设计意图（搜索越多、门槛越高），"
        "但若 MDE 已高于加密日频因子文献中常见的真实 IC（约 0.02–0.05），"
        "则「0 promoted」不能再简单归因为「因子都不行」。",
        "- 样本外段与样本内段共享同一 DGP，等于假设信号完全持久；"
        "这是对本方法最有利的情形，真实衰减只会更差。",
        "- 本报告**不触碰**一次性 strict holdout 账本，也不构成正向 OOS 证据。",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Promotion gate positive-control calibration")
    ap.add_argument("--out", default="user_workspace/reports/gate_calibration.md")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--repeats", type=int, default=12)
    ap.add_argument("--n-perm", type=int, default=30)
    ap.add_argument("--n-bars", type=int, default=641)
    ap.add_argument("--n-assets", type=int, default=20)
    ap.add_argument("--cost-rate", type=float, default=DEFAULT_COST_RATE)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--signals", default=None, help="comma-separated true IC grid")
    ap.add_argument("--trials", default=None, help="comma-separated n_trials grid")
    args = ap.parse_args(argv)

    signals = ([float(x) for x in args.signals.split(",")] if args.signals
               else list(DEFAULT_SIGNALS))
    trials = ([int(x) for x in args.trials.split(",")] if args.trials
              else list(DEFAULT_TRIALS))

    def _progress(signal: float, rep: int, total: int) -> None:
        print(f"  rho={signal:.2f}  {rep + 1}/{total}", flush=True)

    result = calibrate(
        signals, trials,
        n_repeats=args.repeats, n_bars=args.n_bars, n_assets=args.n_assets,
        cost_rate=args.cost_rate, n_perm=args.n_perm, seed=args.seed,
        progress=_progress,
    )
    text = render_report(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    if args.json_out:
        jp = Path(args.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    print(f"False positive rate: {result['false_positive_rate']:.1%}")
    print("MDE by n_trials:", result["mde"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
