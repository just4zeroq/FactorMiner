"""Generate a full-library factor screening report (CSV + recommendation list)
and batch-mark the shortlisted factors as INSPECTED.

Re-runs the screening from the real stored metadata, so it never depends on
memory of previous sessions. Each shortlisted factor is verified for data
availability (snapshot exists, non-empty, metrics finite) before being marked.
"""

import csv
import math
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.storage.factor_storage import LocalFactorStorage

DB = "factor_db"
REPORT_DIR = os.path.join("user_workspace", "reports")
STORAGE = LocalFactorStorage(DB)

# Screening thresholds (sample-in, in-sample mine-period metrics).
TURNOVER_MAX = 2.0
RANKIC_MIN = 0.04
TOP_N = 6


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    metas = STORAGE.list_metadata()
    print(f"total metadata: {len(metas)}")

    rows = []
    for m in metas:
        metrics = m.metrics or {}
        lineage = m.data_lineage or {}
        snap_file = lineage.get("snapshot_file")
        snap_path = os.path.join(DB, "values", snap_file) if snap_file else None
        snap_exists = bool(snap_path and os.path.exists(snap_path))
        snap_rows = lineage.get("snapshot_rows", 0) or 0
        pairs = lineage.get("pairs") or []

        rankic = metrics.get("RankIC")
        ic = metrics.get("IC")
        turnover = metrics.get("Turnover")
        fitness = metrics.get("fitness_score")

        metrics_ok = all(_finite(v) for v in [rankic, ic, turnover] if v is not None)
        data_valid = bool(metrics_ok and snap_exists and snap_rows > 0)

        rows.append(
            {
                "factor_id": m.factor_id,
                "miner_type": m.miner_type,
                "lifecycle_status": m.lifecycle_status,
                "RankIC": rankic,
                "IC": ic,
                "Turnover": turnover,
                "fitness_score": fitness,
                "abs_RankIC": abs(rankic) if _finite(rankic) else None,
                "pairs_count": len(pairs),
                "timeframe": lineage.get("timeframe"),
                "mining_mode": lineage.get("mining_mode"),
                "snapshot_rows": snap_rows,
                "snapshot_exists": snap_exists,
                "snapshot_file": snap_file,
                "created_at": m.created_at,
                "data_valid": data_valid,
            }
        )

    rows.sort(key=lambda r: (r["abs_RankIC"] or 0), reverse=True)

    candidates = [
        r
        for r in rows
        if _finite(r["Turnover"])
        and r["Turnover"] < TURNOVER_MAX
        and (r["abs_RankIC"] or 0) > RANKIC_MIN
        and r["data_valid"]
    ]
    print(f"candidates (turnover<{TURNOVER_MAX} & |RankIC|>{RANKIC_MIN} & valid): {len(candidates)}")

    recommended = candidates[:TOP_N]

    marked = []
    for r in recommended:
        STORAGE.update_lifecycle_status(r["factor_id"], "INSPECTED")
        marked.append(r["factor_id"])
    print(f"marked INSPECTED: {marked}")

    fields = [
        "factor_id",
        "miner_type",
        "lifecycle_status",
        "RankIC",
        "IC",
        "Turnover",
        "fitness_score",
        "abs_RankIC",
        "pairs_count",
        "timeframe",
        "mining_mode",
        "snapshot_rows",
        "snapshot_exists",
        "snapshot_file",
        "created_at",
        "data_valid",
    ]
    marked_set = set(marked)
    csv_path = os.path.join(REPORT_DIR, "factor_library_full_report.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            out = {k: r.get(k) for k in fields}
            out["lifecycle_status"] = "INSPECTED" if r["factor_id"] in marked_set else r["lifecycle_status"]
            writer.writerow(out)
    print(f"wrote {csv_path}")

    md_path = os.path.join(REPORT_DIR, "factor_recommendation.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# 因子全库推荐清单\n\n")
        f.write(f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"- 全库因子数: {len(rows)}\n")
        f.write(f"- 候选数(换手<{TURNOVER_MAX} 且 |RankIC|>{RANKIC_MIN} 且数据有效): {len(candidates)}\n")
        f.write(f"- 本次标记为 INSPECTED: {len(recommended)}\n\n")
        f.write("## 筛选条件\n")
        f.write(f"- Turnover < {TURNOVER_MAX}\n")
        f.write(f"- |RankIC| > {RANKIC_MIN}\n")
        f.write("- 数据快照存在且非空、指标完整 (data_valid)\n\n")
        f.write("## 推荐因子（按 |RankIC| 降序）\n\n")
        f.write("| 排名 | factor_id | RankIC | IC | Turnover | fitness | 方向建议 |\n")
        f.write("| --- | --- | --- | --- | --- | --- | --- |\n")
        for i, r in enumerate(recommended, 1):
            direction = "反向使用 (负 IC)" if (r["RankIC"] is not None and r["RankIC"] < 0) else "正向使用"
            f.write(
                f"| {i} | {r['factor_id']} | {r['RankIC']:.4f} | {r['IC']:.4f} | "
                f"{r['Turnover']:.4f} | {r['fitness_score']:.3f} | {direction} |\n"
            )
        f.write("\n## 说明\n")
        f.write("- 以上为样本内 (in-sample, mine 期) 指标，未含手续费/滑点/资金费率，不构成实盘结论。\n")
        f.write("- 标记为 INSPECTED 仅代表通过数据可用性核对；建议进一步做样本外组合净值回测（第③步）。\n")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
