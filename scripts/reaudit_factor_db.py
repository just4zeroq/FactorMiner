"""Re-audit every factor in the production library with the current gate.

The library grew up before the statistical gates existed: 6 factors are still
``INSPECTED`` although the gate now rejects them, and the other 78 have no
``hac_t / p_value / dsr`` at all, so nobody knows whether they are any good.

This script recomputes the full verdict from the stored factor snapshots with
exactly the same code the live evaluator uses, and reports what the gate would
decide today. Read-only by default; ``--apply`` writes the lifecycle status
back after copying the original metadata to ``metadata_pre_reaudit/``.

Usage::

    python scripts/reaudit_factor_db.py                     # report only
    python scripts/reaudit_factor_db.py --apply             # write back
    python scripts/reaudit_factor_db.py --db-root factor_db --oos-root factor_db_oos
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import statistics
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.evaluation.evaluator import ParallelEvaluator
from core.evaluation.significance import DEFAULT_GATE_CFG, apply_promotion_gates


def _load_panels(path: str) -> Optional[Tuple[pd.DataFrame, pd.DataFrame, str]]:
    """Return (factor, returns, mode) wide frames from a stored snapshot."""
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    cols = set(df.columns)
    if "factor" not in cols:
        return None
    if {"timestamp", "asset"}.issubset(cols):
        f = df.pivot_table(
            index="timestamp", columns="asset", values="factor", aggfunc="mean"
        )
        mode = "panel"
        if "forward_return" in cols:
            r = df.pivot_table(
                index="timestamp",
                columns="asset",
                values="forward_return",
                aggfunc="mean",
            ).reindex(index=f.index, columns=f.columns)
        else:
            r = pd.DataFrame(index=f.index, columns=f.columns, dtype=float)
        return f, r, mode
    if {"timestamp", "forward_return"}.issubset(cols):
        f = pd.Series(df["factor"].to_numpy(), index=pd.to_datetime(df["timestamp"]))
        r = pd.Series(
            df["forward_return"].to_numpy(), index=pd.to_datetime(df["timestamp"])
        )
        return f, r, "series"
    return None


def _rank_ic(factor, returns) -> float:
    if isinstance(factor, pd.DataFrame):
        ic = factor.corrwith(returns, axis=1, method="spearman").dropna()
        return float(ic.mean()) if len(ic) else 0.0
    pair = pd.DataFrame({"f": factor, "r": returns}).dropna()
    if pair.shape[0] < 30:
        return 0.0
    val = pair["f"].corr(pair["r"], method="spearman")
    return float(val) if pd.notna(val) else 0.0


def audit(
    db_root: str,
    oos_root: Optional[str],
    n_trials: int,
    gate_cfg: Dict,
) -> List[Dict]:
    evaluator = ParallelEvaluator(data_client=None, config={})
    rows: List[Dict] = []
    for path in sorted(glob.glob(os.path.join(db_root, "metadata", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        fid = meta["factor_id"]
        loaded = _load_panels(os.path.join(db_root, "values", f"{fid}.parquet"))
        if loaded is None:
            rows.append({"factor_id": fid, "old": meta.get("lifecycle_status"),
                         "new": "SKIP", "note": "no snapshot"})
            continue
        f_is, r_is, mode = loaded
        if isinstance(r_is, pd.DataFrame) and r_is.isna().all().all():
            rows.append({"factor_id": fid, "old": meta.get("lifecycle_status"),
                         "new": "SKIP", "note": "no forward_return"})
            continue

        verdict = evaluator._build_verdict(f_is, r_is, meta.get("metrics", {}) or {})
        rankic_is = _rank_ic(f_is, r_is)

        rankic_oos = 0.0
        oos_consistent = None
        oos_note = "no-oos-snapshot"
        if oos_root:
            loaded_oos = _load_panels(
                os.path.join(oos_root, "values", f"{fid}.parquet")
            )
            if loaded_oos is not None:
                f_oos, r_oos, _ = loaded_oos
                if not (
                    isinstance(r_oos, pd.DataFrame) and r_oos.isna().all().all()
                ):
                    rankic_oos = _rank_ic(f_oos, r_oos)
                    oos_consistent = bool(
                        np.sign(rankic_is) == np.sign(rankic_oos)
                        and abs(rankic_is) > 1e-9
                    )
                    oos_note = "ok"

        local_cfg = dict(gate_cfg)
        if oos_consistent is None:
            # No out-of-sample snapshot exists for this factor: the OOS gates
            # are not applicable (they are not silently "passed" either).
            local_cfg["require_oos_consistent"] = False
            local_cfg["require_oos_strength"] = False

        gate_input = {
            **verdict,
            "p_adj": 0.0,
            "oos_consistent": bool(oos_consistent) if oos_consistent is not None else False,
            "RankIC_oos": abs(rankic_oos),
            "oos_enabled": oos_consistent is not None,
        }
        # A4: do NOT call apply_promotion_gates per factor. A one-element
        # family makes BH-FDR return the raw p-value, which is exactly why the
        # previous re-audit reported p_adj == p_value for 84/84 factors.
        # Stash the inputs and gate the WHOLE library in one batch below.
        gate_input["_gate_cfg"] = local_cfg
        rows.append({
            "factor_id": fid,
            "old": meta.get("lifecycle_status"),
            "new": "PENDING",
            "mode": mode,
            "rankic_is": rankic_is,
            "rankic_oos": rankic_oos,
            "hac_t": verdict["hac_t"],
            "p_adj": 0.0,
            "dsr": 0.0,
            "net": verdict["cost_adj_return"],
            "ls_t": verdict.get("ls_t", 0.0),
            "turnover": verdict.get("ls_turnover", 0.0),
            "oos": oos_note,
            "fail": "-",
            "_verdict": verdict,
            "_gate_input": gate_input,
            "_gate_cfg": local_cfg,
            "_meta_path": path,
        })

    gated = [r for r in rows if r.get("_gate_input") is not None]
    if gated:
        inputs = [r["_gate_input"] for r in gated]
        family = [float(g.get("p_value", 1.0)) for g in inputs]
        results = apply_promotion_gates(
            inputs, cfg=gate_cfg, n_trials=n_trials, family_p_values=family
        )
        for row, res in zip(gated, results):
            failed = [k for k, v in res["gate"].items() if v is False and k != "pass"]
            row["new"] = res["status"]
            row["p_adj"] = res["p_adj"]
            row["dsr"] = res["dsr"]
            row["fail"] = ",".join(failed) or "-"
    return rows


def _summarise(rows: List[Dict]) -> None:
    ok = [r for r in rows if r.get("new") != "SKIP"]
    skipped = [r for r in rows if r.get("new") == "SKIP"]
    changed = [r for r in ok if r["old"] != r["new"]]
    print(f"library size          : {len(rows)}")
    print(f"re-audited            : {len(ok)}")
    print(f"skipped               : {len(skipped)}")
    print(f"status would change   : {len(changed)}")
    if ok:
        print(
            "pass (INSPECTED)      : "
            f"{sum(1 for r in ok if r['new'] == 'INSPECTED')}"
        )
        print(f"median |RankIC| (IS)  : {statistics.median(abs(r['rankic_is']) for r in ok):.4f}")
        print(f"median net/period     : {statistics.median(r['net'] for r in ok):.6f}")
    for r in changed:
        print(f"  {r['factor_id']}: {r['old']} -> {r['new']}  [{r['fail']}]")


def _write_report(rows: List[Dict], n_trials: int, path: str) -> Dict[int, int]:
    ok = [r for r in rows if r.get("new") != "SKIP"]
    ok.sort(key=lambda r: -abs(r["rankic_is"]))
    lines = [
        "# 因子库全库重审",
        "",
        f"- 因子总数：**{len(rows)}**，可重审：**{len(ok)}**，跳过：**{len(rows) - len(ok)}**",
        f"- DSR 的 `n_trials` 取 **{n_trials}**（库内因子数，作为搜索预算下界）",
        "- 判决使用与线上完全一致的 `_build_verdict` + `apply_promotion_gates`；",
        "  无 OOS 快照的因子，两道 OOS 闸门按“不适用”处理，而不是默认通过。",
        "",
        "| 因子 | 原状态 | 新状态 | 模式 | IS RankIC | OOS RankIC | HAC t | p_adj | DSR | 净收益/期 | 拦截原因 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ok:
        lines.append(
            f"| {r['factor_id']} | {r['old']} | {r['new']} | {r['mode']} | "
            f"{r['rankic_is']:+.4f} | {r['rankic_oos']:+.4f} | {r['hac_t']:.2f} | "
            f"{r['p_adj']:.3g} | {r['dsr']:.3f} | {r['net']:+.6f} | {r['fail']} |"
        )
    # T-05: derive every figure in the conclusions from `ok`, not from the
    # 2026-11-14 run that happened when this script was written.
    passed = [r for r in ok if r["new"] == "INSPECTED"]
    from collections import Counter
    reasons = Counter(
        part.strip()
        for r in ok
        if r["new"] != "INSPECTED"
        for part in str(r["fail"]).split(",")
        if part.strip() and part.strip() != "-"
    )
    top_reason, top_count = reasons.most_common(1)[0] if reasons else ("无", 0)
    n_blocked = max(1, len(ok) - len(passed))
    lines += [
        "",
        "## 结论",
        f"- 通过闸门：**{len(passed)}** / {len(ok)}。",
        f"- 拦截原因以 `{top_reason}` 为主（{top_count}/{n_blocked} 个被拦因子）："
        f"库内因子都是在没有搜索预算记录的情况下挖出来的，"
        f"按 {n_trials} 次试验折减后，策略 Sharpe 普遍达不到多重检验的门槛。",
        "- 这是预期结果：闸门的作用不是保留存量，而是阻止不合格因子继续占用 `INSPECTED`。",
        "",
        "## 分母敏感性",
        "",
        f"`n_trials` 是多重检验的分母。当前 {len(ok)} 个因子高度相关，"
        "真实独立试验数应小于库规模，但没有人记录过当时的搜索预算，"
        "所以下面给出不同分母下的通过率，用于判断结论是否被分母单独决定。",
        "",
        "| n_trials | 通过数 |",
        "|---|---|",
    ]
    sensitivity: Dict[int, int] = {}
    # A4: batch the whole library so BH-FDR sees the real family here too.
    sens_inputs = [r["_gate_input"] for r in ok if r.get("_gate_input") is not None]
    sens_family = [float(g.get("p_value", 1.0)) for g in sens_inputs]
    for k in sorted({n_trials, 20, 6, 1}, reverse=True):
        results = apply_promotion_gates(
            sens_inputs, cfg=DEFAULT_GATE_CFG, n_trials=k,
            family_p_values=sens_family,
        )
        cnt = sum(1 for res in results if res["status"] == "INSPECTED")
        sensitivity[k] = cnt
        lines.append(f"| {k} | {cnt} / {len(sens_inputs)} |")
    lines.append("")
    loose = sensitivity.get(1, 0)
    neg_net = sum(1 for r in ok if float(r["net"]) <= 0)
    weak_t = sum(1 for r in ok if abs(float(r.get("ls_t") or 0.0)) < 1.96)
    lines.append(
        f"即使把分母降到 1（完全不做多重检验），通过数也只有 **{loose}** / {len(ok)} —— "
        f"拦住它们的并不只是 DSR：{neg_net} 个因子成本后净收益 ≤ 0，"
        f"{weak_t} 个因子的多空 t 统计量 |t| < 1.96。"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"report -> {path}")
    return sensitivity


def _apply(rows: List[Dict], db_root: str) -> None:
    backup = os.path.join(db_root, "metadata_pre_reaudit")
    os.makedirs(backup, exist_ok=True)
    updated = 0
    for r in rows:
        if r.get("new") == "SKIP":
            continue
        path = r["_meta_path"]
        bak = os.path.join(backup, os.path.basename(path))
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        metrics = meta.get("metrics", {}) or {}
        metrics.update(r["_verdict"])
        metrics["p_adj"] = r["p_adj"]
        metrics["dsr"] = r["dsr"]
        metrics["reaudited_at"] = datetime.now(timezone.utc).date().isoformat()
        meta["metrics"] = metrics
        meta["lifecycle_status"] = r["new"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        updated += 1
    print(f"updated {updated} metadata files; backup -> {backup}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-root", default="factor_db")
    ap.add_argument("--oos-root", default="factor_db_oos")
    ap.add_argument("--n-trials", type=int, default=0,
                    help="search budget for DSR (default: library size)")
    ap.add_argument("--apply", action="store_true", help="write status back")
    ap.add_argument("--report", default="user_workspace/reports/factor_db_reaudit.md")
    args = ap.parse_args()

    size = len(glob.glob(os.path.join(args.db_root, "metadata", "*.json")))
    n_trials = args.n_trials or max(size, 1)
    oos_root = args.oos_root if os.path.isdir(args.oos_root) else None
    rows = audit(args.db_root, oos_root, n_trials, dict(DEFAULT_GATE_CFG))
    if not rows:
        print("[ERROR] 没有找到可重审的因子元数据，拒绝生成空报告", file=sys.stderr)
        return 1
    _summarise(rows)
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    _write_report(rows, n_trials, args.report)
    if args.apply:
        _apply(rows, args.db_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
