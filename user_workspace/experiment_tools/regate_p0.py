"""Re-run the promotion gate on an existing db_root with the current verdict code.

Needed when the gate's own statistics are fixed after a run (e.g. sign
orientation, DSR trial denominator): the mined candidates are unchanged, so we
recompute their verdict from the stored snapshots instead of re-mining (which
would also re-reveal the one-time holdout).

Original metadata files are copied to ``<metadata>/../metadata_pre_regate`` once.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

import pandas as pd

# Make the project root importable when run as a standalone script.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.evaluation.evaluator import ParallelEvaluator
from core.evaluation.significance import apply_promotion_gates


def _panel(values: pd.DataFrame, column: str) -> pd.DataFrame:
    sub = values.dropna(subset=[column])
    if sub.empty:
        return pd.DataFrame()
    return sub.pivot_table(index="timestamp", columns="asset", values=column, aggfunc="mean")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-root", default="factor_db_p0")
    ap.add_argument("--config", default="user_workspace/configs/configOOSHoldout.json")
    ap.add_argument("--apply", action="store_true", help="Write updated metadata (default: dry run)")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    gate_cfg = dict(cfg.get("evaluation", {}).get("gates", {}))
    for key in ("strict_holdout", "trace_path", "holdout_path"):
        gate_cfg.pop(key, None)

    evaluator = ParallelEvaluator(data_client=None, config=cfg)
    meta_paths = sorted(glob.glob(os.path.join(args.db_root, "metadata", "*.json")))
    if not meta_paths:
        raise SystemExit(f"no metadata under {args.db_root}")

    backup_dir = os.path.join(args.db_root, "metadata_pre_regate")
    rows = []
    for path in meta_paths:
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        fid = meta["factor_id"]
        values_path = os.path.join(args.db_root, "values", f"{fid}.parquet")
        if not os.path.exists(values_path):
            print(f"[skip] {fid}: no values snapshot")
            continue
        values = pd.read_parquet(values_path)
        fpanel = _panel(values, "factor")
        rpanel = _panel(values, "forward_return")
        if fpanel.empty or rpanel.empty:
            print(f"[skip] {fid}: empty panel")
            continue
        rpanel = rpanel.reindex(index=fpanel.index, columns=fpanel.columns)
        metrics = meta.get("metrics", {}) or {}
        verdict = evaluator._build_verdict(fpanel, rpanel, metrics)
        n_trials = int(metrics.get("n_trials") or 0)
        if n_trials <= 1:
            n_trials = len(meta_paths)
        gates = apply_promotion_gates([{**metrics, **verdict}], cfg=gate_cfg, n_trials=n_trials)
        g = gates[0]
        rows.append({
            "factor_id": fid,
            "old_status": meta.get("lifecycle_status"),
            "new_status": g["status"],
            "hac_t": verdict["hac_t"],
            "orientation": verdict.get("orientation"),
            "dsr": g["dsr"],
            "p_adj": g["p_adj"],
            "ir": verdict["ir"],
            "cost_adj": verdict["cost_adj_return"],
            "n_trials": n_trials,
            "fail": ",".join(k for k, v in g["gate"].items() if v is False) or "-",
        })
        if args.apply:
            os.makedirs(backup_dir, exist_ok=True)
            bak = os.path.join(backup_dir, os.path.basename(path))
            if not os.path.exists(bak):
                shutil.copy2(path, bak)
            metrics.update(verdict)
            metrics.update({
                "p_adj": g["p_adj"],
                "dsr": g["dsr"],
                "n_trials": n_trials,
            })
            meta["metrics"] = metrics
            meta["lifecycle_status"] = g["status"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(df.to_string(index=False))
    print("\nstatus counts:", df["new_status"].value_counts().to_dict())
    if args.apply:
        print(f"updated metadata in {args.db_root}; backup -> {backup_dir}")


if __name__ == "__main__":
    main()
