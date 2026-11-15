"""Build the true out-of-sample validation report.

Collects the six backtest summary.csv files (in-sample 2024 / leaked OOS /
strict OOS, each with and without costs), the per-factor in-sample RankIC from
the library metadata, and the recomputed 2025H1 RankIC from factor_db_oos, then
writes a CSV comparison plus a Markdown report.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List

import pandas as pd

from core.backtest.backtest_engine import infer_orientation
from core.storage.factor_storage import LocalFactorStorage

FACTOR_IDS = [
    "fac_c25e28c3", "fac_8af952ab", "fac_b66d17ce",
    "fac_099a7eef", "fac_f20a1349", "fac_2017455e",
]

RUNS = [
    ("IS 2024, with costs", "backtest_is_2024"),
    ("IS 2024, zero cost", "backtest_is_2024_nocost"),
    ("OOS 2025H1 (leaked direction), with costs", "backtest_oos_2025H1"),
    ("OOS 2025H1 (leaked direction), zero cost", "backtest_oos_2025H1_nocost"),
    ("STRICT OOS 2025H1, with costs", "backtest_oos_strict"),
    ("STRICT OOS 2025H1, zero cost", "backtest_oos_strict_nocost"),
]

REPORT_DIR = os.path.join("user_workspace", "reports")


def collect_runs() -> pd.DataFrame:
    rows: List[Dict] = []
    for label, folder in RUNS:
        path = os.path.join(REPORT_DIR, folder, "summary.csv")
        if not os.path.exists(path):
            print(f"  [WARN] missing {path}")
            continue
        s = pd.read_csv(path).iloc[0].to_dict()
        rows.append({
            "scenario": label,
            "scope": s.get("scope"),
            "strict_oos": s.get("strict_oos", False),
            "total_return_pct": s.get("total_return_pct"),
            "zero_cost_return_pct": None,
            "sharpe": s.get("sharpe"),
            "max_drawdown_pct": s.get("max_drawdown_pct"),
            "total_costs": s.get("total_costs"),
            "num_trades": s.get("num_trades"),
        })
    return pd.DataFrame(rows)


def collect_factor_ic() -> pd.DataFrame:
    is_store = LocalFactorStorage("factor_db")
    oos_store = LocalFactorStorage("factor_db_oos")
    rows: List[Dict] = []
    for fid in FACTOR_IDS:
        meta_path = os.path.join("factor_db", "metadata", f"{fid}.json")
        meta = json.load(open(meta_path, encoding="utf-8-sig")) if os.path.exists(meta_path) else {}
        is_df = is_store.load_factor_values(fid)
        oos_df = oos_store.load_factor_values(fid)
        is_ic = float(meta.get("metrics", {}).get("RankIC", float("nan")))
        oos_ic = (
            oos_df.dropna(subset=["factor", "forward_return"])["factor"]
            .corr(oos_df.dropna(subset=["factor", "forward_return"])["forward_return"], method="spearman")
            if oos_df is not None else float("nan")
        )
        is_sign = infer_orientation(is_df) if is_df is not None else float("nan")
        oos_sign = infer_orientation(oos_df) if oos_df is not None else float("nan")
        rows.append({
            "factor_id": fid,
            "IS_RankIC": round(is_ic, 4),
            "OOS_RankIC": round(float(oos_ic), 4),
            "IC_decay_ratio": round(abs(float(oos_ic) / is_ic), 2) if is_ic else None,
            "IS_direction": int(is_sign),
            "OOS_direction": int(oos_sign),
            "direction_flipped": bool(is_sign != oos_sign),
        })
    return pd.DataFrame(rows)


def _scenario_value(runs: pd.DataFrame, scenario: str, column: str) -> float:
    sel = runs.loc[runs["scenario"] == scenario, column]
    if sel.empty or pd.isna(sel.iloc[0]):
        raise ValueError(f"回测结果缺失: {scenario}.{column}")
    return float(sel.iloc[0])


def build_conclusions(runs: pd.DataFrame, ics: pd.DataFrame) -> List[str]:
    """Derive every number in the conclusions section from the collected data.

    The previous version hard-coded the figures of one historical run into
    the prose, so re-running the script on new backtests produced a report
    that still described the old data.
    """
    zero_oos = _scenario_value(runs, "STRICT OOS 2025H1, zero cost", "total_return_pct")
    zero_is = _scenario_value(runs, "IS 2024, zero cost", "total_return_pct")
    cost_oos = _scenario_value(runs, "STRICT OOS 2025H1, with costs", "total_return_pct")
    leaked = _scenario_value(runs, "OOS 2025H1 (leaked direction), with costs", "total_return_pct")
    sr_oos = _scenario_value(runs, "STRICT OOS 2025H1, with costs", "sharpe")
    sr_is = _scenario_value(runs, "IS 2024, with costs", "sharpe")

    is_mean = float(ics["IS_RankIC"].abs().mean())
    oos_mean = float(ics["OOS_RankIC"].abs().mean())
    if is_mean <= 0:
        raise ValueError("样本内 |RankIC| 均值为 0，无法计算衰减比")
    decay_pct = oos_mean / is_mean * 100.0
    n_flip = int(ics["direction_flipped"].sum())
    n_factors = int(len(ics))

    cost_drag = zero_oos - cost_oos          # 零成本收益 − 含成本收益
    leak_inflation = leaked - cost_oos       # 泄漏版相对严格版虚高
    zero_verdict = "亏损" if zero_oos < 0 else "盈利"
    alpha_note = (
        "说明亏损来源于因子本身无 alpha，而非交易成本"
        if zero_oos < 0 else "零成本为正，不能据此断言因子无 alpha"
    )
    sr_relation = "劣于" if sr_oos < sr_is else "不劣于"
    oos_verdict = (
        "**不具备上线条件**" if cost_oos < 0 else "**需进一步验证后才可上线**"
    )

    return [
        "",
        "## 3. 结论",
        "",
        f"1. **零成本依然{zero_verdict}**：严格 OOS 零成本 {zero_oos:.2f}%、"
        f"样本内零成本 {zero_is:.2f}%，{alpha_note}"
        f"（成本再侵蚀约 {cost_drag:.2f}pt）。",
        f"2. **样本外表现{sr_relation}样本内**：严格 OOS Sharpe {sr_oos:.2f}，"
        f"样本内 {sr_is:.2f}；|RankIC| 从 {is_mean:.4f} 衰减至 {oos_mean:.4f}"
        f"（{decay_pct:.0f}%），且 {n_flip}/{n_factors} 个因子方向翻转"
        f"{' → 典型过拟合/拟合噪声' if n_flip > 0 and decay_pct < 100 else ''}。",
        "3. **方向冻结的影响**：原引擎方向从被评估快照自身推断。"
        f"泄漏版 OOS 为 {leaked:.2f}%，严格版 {cost_oos:.2f}%，"
        f"虚高 {leak_inflation:.2f}pt。"
        "现通过 `--orientation-db-root` 支持方向冻结，`strict_oos` 标记写入 summary。",
        f"4. 这 {n_factors} 个因子{oos_verdict}；`INSPECTED` 状态应视为未通过验证。",
        "",
        "## 4. 建议下一步",
        "",
        "- 用 P0 的 OOS-fitness + 新算子库 + 末层线性组合**重新挖掘**，"
        "并以 `test_period` 明确留出，再走同一套严格 OOS 回测流程。",
        "- 把「方向从样本内冻结 + 成本感知 fitness」作为挖掘闸门，避免再次产出此类因子。",
        "- 修 `Turnover` 归一化 bug 后开启 fitness 的换手惩罚。",
    ]


def main() -> int:
    runs = collect_runs()
    ics = collect_factor_ic()
    if runs.empty or ics.empty:
        print("[ERROR] 回测结果或因子 IC 数据为空，拒绝生成含写死结论的报告", flush=True)
        return 1
    try:
        conclusions = build_conclusions(runs, ics)
    except ValueError as exc:
        print(f"[ERROR] 结论与数据不一致: {exc}", flush=True)
        return 1

    runs.to_csv(os.path.join(REPORT_DIR, "oos_validation_comparison.csv"), index=False)
    ics.to_csv(os.path.join(REPORT_DIR, "oos_validation_factor_ic.csv"), index=False)

    def f(v, pct=False):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "n/a"
        return f"{v:.2f}%" if pct else f"{v:.2f}"

    lines = [
        f"# 真样本外验证报告：{len(ics)} 个 INSPECTED 因子（2025H1）",
        "",
        "## 方法",
        "",
        f"- 因子逻辑来自 `factor_db` 中 {len(ics)} 个 GP(`json_ast`) 因子的持久化 AST，",
        "  用 `FactorCompiler.compile_for_live_trading()` 在 20 币 1d 行情上重算 2025H1 值；",
        "  warm-up 从 2024-07-01 起（仅供滚动窗口预热，不进入评估区间）。",
        "- 重算结果写入独立 `factor_db_oos/`，原 `factor_db` 不变。",
        "- 回测走 `core.cli backtest`（ml4t-backtest，事件驱动、次日收盘成交）。",
        "- **严格 OOS**：`--db-root factor_db_oos --orientation-db-root factor_db`，",
        "  因子值来自 2025H1、多空方向从 2024 样本内冻结，避免前视泄漏。",
        "- 对照：泄漏版 OOS（方向从 OOS 快照自身推断）、样本内 2024、以及各自的零成本版本。",
        "",
        "## 1. 逐因子 IC 衰减与方向稳定性",
        "",
        "| 因子 | 样本内 RankIC | OOS RankIC | 衰减比 | 样本内方向 | OOS方向 | 方向翻转 |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in ics.iterrows():
        lines.append(
            f"| {r['factor_id']} | {r['IS_RankIC']:+.4f} | {r['OOS_RankIC']:+.4f} | "
            f"{r['IC_decay_ratio']} | {r['IS_direction']:+d} | {r['OOS_direction']:+d} | "
            f"{'**是**' if r['direction_flipped'] else '否'} |"
        )
    n_flip = int(ics["direction_flipped"].sum())
    lines += [
        "",
        f"样本内 |RankIC| 均值 **{ics['IS_RankIC'].abs().mean():.4f}** → OOS **{ics['OOS_RankIC'].abs().mean():.4f}**"
        f"（衰减至 {ics['OOS_RankIC'].abs().mean()/ics['IS_RankIC'].abs().mean()*100:.0f}%）；"
        f"**{n_flip}/{len(ics)} 个因子方向翻转**。",
        "",
        "## 2. 回测对比（统一 CLI / 同一批因子 / 相同成本参数）",
        "",
        "| 场景 | 区间 | 严格OOS | 总收益 | Sharpe | 最大回撤 | 总成本 | 成交数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, r in runs.iterrows():
        lines.append(
            f"| {r['scenario']} | {r['scope']} | {r['strict_oos']} | "
            f"{f(r['total_return_pct'], True)} | {f(r['sharpe'])} | "
            f"{f(r['max_drawdown_pct'], True)} | {f(r['total_costs'])} | {int(r['num_trades'])} |"
        )
    # Section 3/4 are generated from the data above; no historical numbers
    # are baked into the prose any more (T-05).
    lines += conclusions

    out_md = os.path.join(REPORT_DIR, "oos_validation_2025H1.md")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nWritten: {out_md}")
    print(f"Written: {os.path.join(REPORT_DIR, 'oos_validation_comparison.csv')}")
    print(f"Written: {os.path.join(REPORT_DIR, 'oos_validation_factor_ic.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
