"""Summarise a mining run's gate decisions (status, verdict fields, fitness).

Reads ``<db_root>/metadata/*.json`` and prints the promotion table plus the
list of factor ids that reached INSPECTED. Also writes a CSV so the report can
be reproduced without re-running the miner.

Usage:
    python user_workspace/experiment_tools/summarize_p0_holdout.py \
        --db-root factor_db_p0 \
        --output user_workspace/reports/p0_holdout/factors.csv
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import pandas as pd

COLUMNS = [
    "factor_id", "miner_type", "lifecycle_status", "fitness_score",
    "RankIC", "RankIC_oos", "hac_t", "p_adj", "dsr", "n_trials",
    "cost_adj_return", "Turnover", "oos_enabled", "oos_consistent",
]


def load_rows(db_root: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(db_root, "metadata", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        metrics = meta.get("metrics", {}) or {}
        row = {
            "factor_id": meta.get("factor_id"),
            "miner_type": meta.get("miner_type"),
            "lifecycle_status": meta.get("lifecycle_status"),
        }
        for key in COLUMNS[3:]:
            row[key] = metrics.get(key)
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-root", default="factor_db_p0")
    ap.add_argument("--output", default="user_workspace/reports/p0_holdout/factors.csv")
    args = ap.parse_args()

    rows = load_rows(args.db_root)
    if not rows:
        raise SystemExit(f"no metadata found under {args.db_root}/metadata")

    df = pd.DataFrame(rows).sort_values("fitness_score", ascending=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    print(df.to_string(index=False))
    print("\nstatus counts:", df["lifecycle_status"].value_counts().to_dict())

    inspected = df[df["lifecycle_status"] == "INSPECTED"]["factor_id"].tolist()
    print("INSPECTED:", ",".join(inspected) if inspected else "(none)")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"\nwritten -> {args.output}")


if __name__ == "__main__":
    main()
