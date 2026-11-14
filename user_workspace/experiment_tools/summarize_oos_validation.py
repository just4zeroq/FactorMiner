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


def main() -> None:
    runs = collect_runs()
    ics = collect_factor_ic()

    runs.to_csv(os.path.join(REPORT_DIR, "oos_validation_comparison.csv"), index=False)
    ics.to_csv(os.path.join(REPORT_DIR, "oos_validation_factor_ic.csv"), index=False)

    def f(v, pct=False):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "n/a"
        return f"{v:.2f}%" if pct else f"{v:.2f}"

    lines = [
        "# 真样本外验证报告：6 个 INSPECTED 因子（2025H1）",
        "",
        "## 方法",
        "",
        "- 因子逻辑来自 `factor_db` 中 6 个 GP(`json_ast`) 因子的持久化 AST，",
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
        f"**{n_flip}/6 个因子方向翻转**。",
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
    lines += [
        "",
        "## 3. 结论",
        "",
        "1. **零成本依然亏损**：严格 OOS 零成本 -7.37%、样本内零成本 -10.41%，"
        "说明亏损来源于因子本身无 alpha，而非交易成本（成本仅再侵蚀约 3.8pt）。",
        f"2. **样本外更差**：严格 OOS Sharpe -1.14，劣于样本内 -0.55；"
        f"|RankIC| 整体衰减至 {ics['OOS_RankIC'].abs().mean()/ics['IS_RankIC'].abs().mean()*100:.0f}%，"
        f"且 {n_flip}/6 个因子方向翻转 → 典型过拟合/拟合噪声。",
        "3. **发现并修复一处前视泄漏**：原引擎方向从被评估快照自身推断。"
        "泄漏版 OOS 为 -6.59%，严格版 -11.18%，虚高约 4.6pt。"
        "现通过 `--orientation-db-root` 支持方向冻结，`strict_oos` 标记写入 summary。",
        "4. 这 6 个因子**不具备上线条件**；`INSPECTED` 状态应视为未通过验证。",
        "",
        "## 4. 建议下一步",
        "",
        "- 用 P0 的 OOS-fitness + 新算子库 + 末层线性组合**重新挖掘**，"
        "并以 `test_period` 明确留出，再走同一套严格 OOS 回测流程。",
        "- 把「方向从样本内冻结 + 成本感知 fitness」作为挖掘闸门，避免再次产出此类因子。",
        "- 修 `Turnover` 归一化 bug 后开启 fitness 的换手惩罚。",
    ]

    out_md = os.path.join(REPORT_DIR, "oos_validation_2025H1.md")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nWritten: {out_md}")
    print(f"Written: {os.path.join(REPORT_DIR, 'oos_validation_comparison.csv')}")
    print(f"Written: {os.path.join(REPORT_DIR, 'oos_validation_factor_ic.csv')}")


if __name__ == "__main__":
    main()
