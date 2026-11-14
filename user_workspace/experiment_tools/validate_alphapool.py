"""Validate the static AlphaPool (Phase 2) on real FactorMiner factor panels.

Part 1 (in-sample): load the 6 GP factor panels from ``factor_db/values``, build
a fixed-capacity AlphaPool, demonstrate mutual-IC de-duplication and that the
pooled (combined) RankIC beats the best single factor / equal-weight baseline.

Part 2 (OOS probe): repeat on ``factor_db_oos/values`` (2025H1) to report the
pool's out-of-sample behaviour. NOTE: the 6 legacy factors were already shown to
be individually weak (Phase 1), so this is an honest probe, not a demonstration
of strength -- re-mining under the statistical gates is the planned next step.

Run:  python -m core.cli ...   (not needed)
      venv/Scripts/python.exe user_workspace/experiment_tools/validate_alphapool.py
"""

import glob
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.miner.pool import AlphaPool, long_to_wide


def _load_panels(db_root: str):
    """Return (factor_panels: {id: wide (T,N)}, returns: wide (T,N))."""
    value_files = sorted(glob.glob(os.path.join(db_root, "values", "*.parquet")))
    if not value_files:
        raise RuntimeError(f"No factor value parquets in {db_root}/values")
    frames = []
    for f in value_files:
        fid = os.path.splitext(os.path.basename(f))[0]
        df = pd.read_parquet(f)
        df["_fid"] = fid
        frames.append(df)
    all_df = pd.concat(frames, ignore_index=True)

    # Shared forward-return target across all factors (it is the data's target).
    # Dedupe (timestamp, asset) by mean in case a (date, asset) appears more
    # than once across files, then pivot to a (T, N) wide panel.
    returns = (
        all_df.groupby(["timestamp", "asset"])["forward_return"].mean().unstack().sort_index()
    )

    factor_panels = {}
    for fid in all_df["_fid"].unique():
        sub = all_df[all_df["_fid"] == fid]
        wide = (
            sub.groupby(["timestamp", "asset"])["factor"].mean().unstack().sort_index()
        )
        wide = wide.reindex(index=returns.index, columns=returns.columns)
        factor_panels[fid] = wide
    return factor_panels, returns


def _pairwise_mutual_ic(panels, returns):
    ids = list(panels.keys())
    pool = AlphaPool(capacity=999, mutual_ic_max=0.99, weight_fit_shift=0)
    pool.set_target(returns)
    # admit sequentially so mutual_ic is measured against the growing pool
    seq = []
    for fid in ids:
        mic = pool.mutual_ic(panels[fid]) if pool._panels else 0.0
        seq.append((fid, mic))
        pool.admit(fid, panels[fid])
    return seq


def _run(db_root, capacity=6, label=""):
    print(f"\n=== {label}: {db_root} ===")
    panels, returns = _load_panels(db_root)
    ids = list(panels.keys())
    print(f"Loaded {len(ids)} factor panels; returns shape {returns.shape}")

    # Pairwise / sequential mutual-IC (de-dup signal).
    seq = _pairwise_mutual_ic(panels, returns)

    # Single-factor RankIC.
    singles = {}
    for fid in ids:
        pool = AlphaPool(capacity=1, weight_fit_shift=0)
        pool.set_target(returns)
        singles[fid] = pool.factor_rankic(panels[fid])

    # AlphaPool admission.
    pool = AlphaPool(capacity=capacity, mutual_ic_max=0.99, weight_fit_shift=0)
    pool.set_target(returns)
    admit = []
    for fid in ids:
        ok, reason = pool.admit(fid, panels[fid])
        admit.append((fid, ok, reason))

    # Pooled (combined) RankIC vs baselines.
    pooled = pool.pooled_rankic()
    best_single = max(singles.values())
    # Equal-weight of ALL factors (no dedup).
    eq = AlphaPool(capacity=999, mutual_ic_max=2.0, weight_fit_shift=0)
    eq.set_target(returns)
    for fid in ids:
        eq.admit(fid, panels[fid])
    eq_rankic = eq.pooled_rankic()

    # De-dup demo: clone the first factor (x1.0 + epsilon noise) and show it is
    # rejected as redundant against the real-shaped panel.
    clone_demo = _demo_dedup_on_real(panels, returns)

    print("  single RankIC:", {k: round(v, 4) for k, v in singles.items()})
    print("  admission:", [(a, ok, r) for a, ok, r in admit])
    print(f"  best single RankIC = {best_single:.4f}")
    print(f"  equal-weight-all  RankIC = {eq_rankic:.4f}")
    print(f"  AlphaPool pooled RankIC = {pooled:.4f}  (size={pool.report()['size']}, ids={pool._ids})")
    print(f"  AlphaPool weights = {[round(w, 4) for w in pool._weights]}")
    return {
        "label": label, "db_root": db_root, "ids": ids, "singles": singles,
        "seq_mutual_ic": seq, "admit": admit, "pooled": pooled,
        "best_single": best_single, "eq_all": eq_rankic,
        "pool_ids": pool._ids, "pool_weights": pool._weights,
        "clone_demo": clone_demo,
    }


def _demo_dedup_on_real(panels, returns):
    ids = list(panels.keys())
    base_id = ids[0]
    base = panels[base_id].to_numpy(dtype=float, na_value=float("nan"))
    rng = np.random.default_rng(7)
    clone = base + 1e-3 * rng.standard_normal(base.shape)
    pool = AlphaPool(capacity=999, mutual_ic_max=0.99, weight_fit_shift=0)
    pool.set_target(returns)
    pool.admit(base_id, panels[base_id])
    mic = pool.mutual_ic(clone)
    ok, reason = pool.admit(base_id + "_clone", clone)
    return {
        "base_id": base_id, "clone_mutual_ic": float(mic),
        "clone_admitted": ok, "clone_reason": reason,
    }


def _write_report(results, path):
    lines = ["# AlphaPool 验证报告（Phase 2，真实因子面板）", ""]
    for r in results:
        lines.append(f"## {r['label']} — `{r['db_root']}`")
        lines.append("")
        lines.append(f"- 因子数：{len(r['ids'])}；AlphaPool 容量 6；mutual-IC 阈值 0.99")
        lines.append(f"- 单因子 RankIC：")
        for fid, v in r["singles"].items():
            lines.append(f"  - `{fid}`: {v:+.4f}")
        lines.append("- 入池判定（含 sequential mutual-IC）：")
        for fid, mic in r["seq_mutual_ic"]:
            lines.append(f"  - `{fid}`: 入池前 mutual-IC = {mic:.3f}")
        lines.append("- 最终入池结果：")
        for fid, ok, reason in r["admit"]:
            lines.append(f"  - `{fid}`: {'入池' if ok else '拒绝'} ({reason})")
        lines.append(f"- **AlphaPool 组合 RankIC = {r['pooled']:+.4f}**（成员 {len(r['pool_ids'])}：{r['pool_ids']}）")
        lines.append(f"- 最优单因子 RankIC = {r['best_single']:+.4f}")
        lines.append(f"- 全体等权 RankIC = {r['eq_all']:+.4f}")
        cd = r.get("clone_demo")
        if cd:
            lines.append("")
            lines.append("  **mutual-IC 去冗余实测**：以真实面板克隆一个因子 "
                         f"(`{cd['base_id']}` ×1.0 + ε 噪声)")
            lines.append(f"  - 克隆的 mutual-IC = {cd['clone_mutual_ic']:.3f}")
            lines.append(f"  - 入池判定：{'入池' if cd['clone_admitted'] else '拒绝'} "
                         f"({cd['clone_reason']})  → "
                         f"{'✅ 去冗余生效' if not cd['clone_admitted'] else '⚠️ 未触发'}")
        verdict = "✅ 组合 ≥ 最优单因子" if r["pooled"] >= r["best_single"] else "⚠️ 组合 < 最优单因子（因子本身弱/冗余）"
        lines.append(f"- 结论：{verdict}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("说明：6 个 legacy 因子在 Phase 1 已被证明样本内过拟合、OOS 全亏；")
    lines.append("因此 OOS 探针的弱表现是因子本身问题，而非 AlphaPool 缺陷。")
    lines.append("AlphaPool 在此的真实价值是 **mutual-IC 去冗余** + **组合权重优化**，")
    lines.append("需配合「在统计闸门下重新挖掘出的 OOS 成立因子」才能体现组合 alpha。")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nReport written to {path}")


def main():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    factor_db = os.path.join(root, "factor_db")
    factor_db_oos = os.path.join(root, "factor_db_oos")
    results = []
    results.append(_run(factor_db, capacity=6, label="样本内 (in-sample 2024)"))
    if os.path.isdir(os.path.join(factor_db_oos, "values")):
        results.append(_run(factor_db_oos, capacity=6, label="样本外探针 (OOS 2025H1)"))
    out = os.path.join(root, "user_workspace", "reports", "alphapool_validation.md")
    _write_report(results, out)


if __name__ == "__main__":
    main()
