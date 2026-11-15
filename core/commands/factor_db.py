"""``factorminer factor-db`` — inspect and manage the production factor library.

``promote`` puts factors in, ``backtest`` takes them out, and nothing in between
could answer "what is actually in the library right now?". That gap is why an
empty ``factor_db`` went unnoticed through three runs. This command owns the
library itself:

    factor-db list                          what is in here?
    factor-db show <id>                     full metadata + snapshot stats
    factor-db rm <id> --yes                 soft-delete into _deleted_<date>/
    factor-db export --out DIR              factor values out to csv/parquet
    factor-db corr --ids a,b,c              pairwise correlation + redundancy
    factor-db combine --ids a,b,c           build a combination factor

Deletion is always a *move* into ``_deleted_<YYYYMMDD>/`` inside the library, so
a mistaken removal is recoverable; nothing here unlinks data.
"""

from __future__ import annotations

import argparse
import datetime
import itertools
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------- #
# Library access
# --------------------------------------------------------------------------- #
def _roots(db_root: str) -> Dict[str, Path]:
    root = Path(db_root)
    return {
        "root": root,
        "metadata": root / "metadata",
        "values": root / "values",
        "sources": root / "sources",
        "weights": root / "weights",
        "models": root / "models",
    }


def load_metas(db_root: str) -> List[Dict[str, Any]]:
    meta_dir = _roots(db_root)["metadata"]
    if not meta_dir.is_dir():
        raise FileNotFoundError(
            f"No factor library at {meta_dir}. Create one with `promote`, or "
            "point --db-root at an existing library."
        )
    out: List[Dict[str, Any]] = []
    for path in sorted(meta_dir.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("factor_id", path.stem)
            out.append(data)
    return out


def _expression(meta: Dict[str, Any], db_root: str) -> str:
    from core.commands.factors import _expression_of

    return _expression_of(meta, db_root)


def _metrics(meta: Dict[str, Any]) -> Dict[str, Any]:
    m = meta.get("metrics") or {}
    return m if isinstance(m, dict) else {}


def _snapshot_path(db_root: str, factor_id: str) -> Path:
    return _roots(db_root)["values"] / f"{factor_id}.parquet"


def load_panel(db_root: str, factor_id: str) -> Optional["Any"]:
    """Load one factor snapshot as a (timestamp x asset) wide panel."""
    import pandas as pd

    path = _snapshot_path(db_root, factor_id)
    if not path.is_file():
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return None
    if frame.empty or "timestamp" not in frame.columns:
        return None
    if "asset" in frame.columns:
        return frame.pivot_table(
            index="timestamp", columns="asset", values="factor", aggfunc="last"
        ).sort_index()
    series = frame.set_index("timestamp")["factor"].sort_index()
    return series.to_frame(name=factor_id)


# --------------------------------------------------------------------------- #
# list / show
# --------------------------------------------------------------------------- #
def list_library(db_root: str) -> Dict[str, Any]:
    metas = load_metas(db_root)
    rows: List[Dict[str, Any]] = []
    for meta in metas:
        metrics = _metrics(meta)
        lineage = meta.get("data_lineage") or {}
        rows.append({
            "factor_id": meta.get("factor_id"),
            "status": meta.get("lifecycle_status") or "",
            "gate_pass": bool(metrics.get("gate_pass", False)),
            "miner_type": meta.get("miner_type") or "",
            "expression": _expression(meta, db_root),
            "hac_t": metrics.get("hac_t"),
            "p_adj": metrics.get("p_adj"),
            "dsr": metrics.get("dsr"),
            "rank_ic": metrics.get("RankIC"),
            "cost_adj_return": metrics.get("cost_adj_return"),
            "created_at": meta.get("created_at"),
            "snapshot_start": lineage.get("snapshot_start"),
            "snapshot_end": lineage.get("snapshot_end"),
            "snapshot_rows": lineage.get("snapshot_rows"),
            "run_id": (lineage.get("promotion") or {}).get("run_id")
            or (lineage.get("run_manifest") or {}).get("run_id"),
            "has_values": _snapshot_path(db_root, str(meta.get("factor_id"))).is_file(),
        })
    statuses: Dict[str, int] = {}
    for row in rows:
        statuses[row["status"] or "-"] = statuses.get(row["status"] or "-", 0) + 1
    return {
        "db_root": str(Path(db_root).resolve()),
        "total": len(rows),
        "n_with_values": sum(1 for r in rows if r["has_values"]),
        "n_gate_pass": sum(1 for r in rows if r["gate_pass"]),
        "by_status": dict(sorted(statuses.items())),
        "factors": rows,
    }


def _print_list(payload: Dict[str, Any], expr_width: int = 48) -> None:
    print(f"library      : {payload['db_root']}")
    print(
        f"factors      : {payload['total']} total, {payload['n_gate_pass']} gate_pass, "
        f"{payload['n_with_values']} with value snapshots"
    )
    print(f"lifecycle    : {payload['by_status']}")
    if not payload["factors"]:
        print("\n(empty) Publish a gated factor with `promote --all-inspected`.")
        return
    print()
    print(f"{'factor_id':<14} {'status':<22} {'gate':>5} {'dsr':>7} {'ic':>7}  {'start':<10} {'end':<10}  expression")
    for row in payload["factors"]:
        expr = row["expression"] or ""
        if expr_width > 0 and len(expr) > expr_width:
            expr = expr[: expr_width - 3] + "..."
        print(
            f"{row['factor_id']:<14} {row['status']:<22} "
            f"{('PASS' if row['gate_pass'] else 'FAIL'):>5} "
            f"{_num(row['dsr']):>7} {_num(row['rank_ic']):>7}  "
            f"{str(row['snapshot_start'] or '-')[:10]:<10} "
            f"{str(row['snapshot_end'] or '-')[:10]:<10}  {expr}"
        )


def _num(value: Any, width: int = 6) -> str:
    try:
        if value is None or isinstance(value, bool):
            return "-".rjust(width)
        return f"{float(value):.4g}".rjust(width)
    except (TypeError, ValueError):
        return "-".rjust(width)


def show_factor(db_root: str, factor_id: str) -> Dict[str, Any]:
    meta_dir = _roots(db_root)["metadata"]
    path = meta_dir / f"{factor_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Factor {factor_id} not found in {meta_dir}")
    with open(path, "r", encoding="utf-8-sig") as handle:
        meta = json.load(handle)
    panel = load_panel(db_root, factor_id)
    stats: Dict[str, Any] = {}
    if panel is not None:
        try:
            import numpy as np

            arr = panel.to_numpy(dtype=float)
            finite = arr[np.isfinite(arr)]
            stats = {
                "rows": int(panel.shape[0]),
                "assets": int(panel.shape[1]),
                "coverage": float(finite.size / arr.size) if arr.size else 0.0,
                "mean": float(finite.mean()) if finite.size else None,
                "std": float(finite.std()) if finite.size else None,
                "start": str(panel.index.min()),
                "end": str(panel.index.max()),
            }
        except Exception:  # pragma: no cover - diagnostics must not fail show
            stats = {}
    return {
        "factor_id": factor_id,
        "metadata": meta,
        "expression": _expression(meta, db_root),
        "snapshot": stats,
    }


# --------------------------------------------------------------------------- #
# rm (soft delete)
# --------------------------------------------------------------------------- #
def delete_factor(db_root: str, factor_id: str, *, apply: bool) -> Dict[str, Any]:
    """Soft-delete: move metadata/values/artifacts into ``_deleted_<date>/``."""
    roots = _roots(db_root)
    stamp = datetime.date.today().strftime("%Y%m%d")
    bucket = roots["root"] / f"_deleted_{stamp}"
    moves = [
        roots["metadata"] / f"{factor_id}.json",
        roots["values"] / f"{factor_id}.parquet",
        roots["sources"] / f"{factor_id}.py",
    ]
    for sub in ("weights", "models"):
        folder = roots[sub]
        if folder.is_dir():
            moves.extend(p for p in folder.glob(f"{factor_id}*") if p.is_file())
    present = [p for p in moves if p.is_file()]
    if not present:
        raise FileNotFoundError(f"Factor {factor_id} not found under {roots['root']}")
    if not apply:
        return {
            "dry_run": True,
            "factor_id": factor_id,
            "would_move": [str(p) for p in present],
            "bucket": str(bucket),
        }
    moved: List[str] = []
    for path in present:
        target = bucket / path.parent.name / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        moved.append(str(target))
    return {
        "dry_run": False,
        "factor_id": factor_id,
        "moved": moved,
        "bucket": str(bucket),
    }


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_factors(
    db_root: str,
    factor_ids: Sequence[str],
    *,
    out_dir: str,
    fmt: str = "parquet",
    wide: bool = True,
) -> Dict[str, Any]:
    import pandas as pd

    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    missing: List[str] = []
    for fid in factor_ids:
        panel = load_panel(db_root, fid)
        if panel is None:
            missing.append(fid)
            continue
        frame = panel if wide else panel.stack().rename("factor").reset_index()
        target = os.path.join(out_dir, f"{fid}.{fmt}")
        if fmt == "csv":
            frame.to_csv(target)
        else:
            frame.to_parquet(target)
        written.append(target)
    return {"out_dir": os.path.abspath(out_dir), "written": written, "missing": missing}


# --------------------------------------------------------------------------- #
# corr
# --------------------------------------------------------------------------- #
def correlation_matrix(
    db_root: str,
    factor_ids: Sequence[str],
    *,
    method: str = "spearman",
    threshold: float = 0.7,
) -> Dict[str, Any]:
    """Pairwise correlation between stored factor panels (F3).

    Correlation is computed on cross-sectionally ranked values (the signal the
    portfolio actually trades), not raw levels, so two factors that order assets
    identically are flagged even if their scales differ by orders of magnitude.
    """
    import numpy as np
    import pandas as pd

    panels: Dict[str, Any] = {}
    missing: List[str] = []
    for fid in factor_ids:
        panel = load_panel(db_root, fid)
        if panel is None:
            missing.append(fid)
            continue
        panels[fid] = panel.rank(axis=1) if wide_rank_ok(panel) else panel
    ids = [f for f in factor_ids if f in panels]
    matrix = pd.DataFrame(np.nan, index=ids, columns=ids, dtype=float)
    pairs: List[Dict[str, Any]] = []
    for a in ids:
        matrix.loc[a, a] = 1.0
    for a, b in itertools.combinations(ids, 2):
        joined = pd.concat(
            [panels[a].stack().rename("a"), panels[b].stack().rename("b")],
            axis=1,
        ).dropna()
        if len(joined) < 30:
            value = float("nan")
        else:
            value = float(joined["a"].corr(joined["b"], method=method))
        matrix.loc[a, b] = matrix.loc[b, a] = value
        if np.isfinite(value):
            pairs.append({"a": a, "b": b, "corr": value})
    redundant = [p for p in pairs if abs(p["corr"]) >= threshold]
    redundant.sort(key=lambda p: abs(p["corr"]), reverse=True)
    return {
        "method": method,
        "threshold": threshold,
        "ids": ids,
        "missing": missing,
        "matrix": {i: {j: matrix.loc[i, j] for j in ids} for i in ids},
        "redundant_pairs": redundant,
    }


def wide_rank_ok(panel: Any) -> bool:
    return getattr(panel, "shape", (0, 0))[1] > 1


# --------------------------------------------------------------------------- #
# combine
# --------------------------------------------------------------------------- #
def _aligned_values(
    panels: Dict[str, Any], index: Any, columns: Any
) -> Dict[str, Any]:
    import pandas as pd

    out: Dict[str, Any] = {}
    for fid, panel in panels.items():
        out[fid] = panel.reindex(index=index, columns=columns)
    return out


def build_combination(
    db_root: str,
    factor_ids: Sequence[str],
    *,
    method: str = "equal",
    returns_root: Optional[str] = None,
    ridge_alpha: float = 5.0,
    max_weight: float = 0.5,
) -> Dict[str, Any]:
    """Fit member weights and return a persistable combination definition (F4).

    Methods:

    * ``equal``   — 1/n each (the honest baseline);
    * ``ic_weight`` — weight ∝ |RankIC| with the sign of the IC;
    * ``ridge``   — in-sample ridge of the forward return on standardised
      member values (the same estimator ``_finalize_linear`` uses);
    * ``hrp``     — hierarchical risk parity on the member value covariance,
      which needs no matrix inversion and survives near-collinear members.

    Weights are capped at ``max_weight`` and renormalised, so a single member can
    never dominate the book purely because it had the best in-sample fit.
    """
    import numpy as np
    import pandas as pd

    metas = {m.get("factor_id"): m for m in load_metas(db_root)}
    panels: Dict[str, Any] = {}
    missing: List[str] = []
    for fid in factor_ids:
        panel = load_panel(db_root, fid)
        if panel is None:
            missing.append(fid)
            continue
        panels[fid] = panel
    if not panels:
        raise ValueError("None of the requested factors has a value snapshot")

    index = None
    columns = None
    for panel in panels.values():
        index = panel.index if index is None else index.union(panel.index)
        columns = panel.columns if columns is None else columns.union(panel.columns)
    values = _aligned_values(panels, index, columns)
    stacked = pd.concat(
        {fid: frame.stack().dropna() for fid, frame in values.items()}, axis=1
    )
    ids = list(values)

    if method == "equal":
        weights = np.ones(len(ids), dtype=float)
    elif method == "ic_weight":
        weights = np.array(
            [
                float(_metrics(metas[fid]).get("RankIC") or 0.0)
                for fid in ids
            ],
            dtype=float,
        )
        if not np.any(np.abs(weights) > 0):
            weights = np.ones(len(ids), dtype=float)
    elif method == "ridge":
        rows = stacked.dropna()
        if rows.empty:
            raise ValueError("No overlapping observations across the members")
        x = rows[ids].to_numpy(dtype=float)
        x = (x - x.mean(axis=0)) / np.where(x.std(axis=0) > 0, x.std(axis=0), 1.0)
        returns_root = returns_root or db_root
        y = _forward_returns_for(returns_root, rows.index, ids)
        if y is None:
            raise ValueError(
                "ridge needs forward returns; pass --returns-root pointing at a "
                "library whose snapshots carry them"
            )
        alpha = max(float(ridge_alpha), 0.0)
        gram = x.T @ x + alpha * np.eye(x.shape[1])
        weights = np.linalg.solve(gram, x.T @ y)
    elif method == "hrp":
        rows = stacked.dropna()
        if rows.empty:
            raise ValueError("No overlapping observations across the members")
        weights = _hrp_weights(rows[ids].to_numpy(dtype=float))
    else:
        raise ValueError(f"Unknown method {method!r}")

    weights = np.asarray(weights, dtype=float)
    total = float(np.abs(weights).sum())
    if total <= 0:
        weights = np.ones(len(ids), dtype=float)
        total = float(len(ids))
    weights = weights / total
    if max_weight and max_weight > 0:
        weights = _cap_weights(weights, float(max_weight))

    return {
        "method": method,
        "member_factor_ids": ids,
        "weights": [float(w) for w in weights],
        "missing": missing,
        "ridge_alpha": ridge_alpha if method == "ridge" else None,
        "max_weight": max_weight,
        "n_observations": int(stacked.dropna().shape[0]),
    }


def _forward_returns_for(root: str, index: Any, ids: Sequence[str]) -> Optional["Any"]:
    import pandas as pd

    for fid in ids:
        path = _snapshot_path(root, fid)
        if not path.is_file():
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception:
            continue
        if "forward_return" not in frame.columns:
            continue
        if "asset" in frame.columns:
            wide = frame.pivot_table(
                index="timestamp", columns="asset", values="forward_return", aggfunc="last"
            )
        else:
            wide = frame.set_index("timestamp")["forward_return"].to_frame()
        stacked = wide.stack().dropna()
        aligned = stacked.reindex(index).dropna()
        if len(aligned) >= 30:
            return aligned.to_numpy(dtype=float)
    return None


def _cap_weights(weights: "Any", cap: float) -> "Any":
    """Project |weights| onto ``sum = 1, max <= cap``, keeping the signs.

    Water-filling: anything above the cap is pinned there and its excess is
    redistributed over the members that still have headroom. When the cap is
    infeasible (``cap * n < 1``) it is relaxed to ``1/n`` — a 0.4 cap on two
    members cannot hold, and silently returning weights that violate it would be
    worse than saying so.
    """
    import numpy as np

    w = np.asarray(weights, dtype=float)
    if w.size == 0:
        return w
    sign = np.sign(w)
    sign[sign == 0] = 1.0
    mag = np.abs(w)
    total = float(mag.sum())
    if total <= 0:
        return np.full(w.size, 1.0 / w.size)
    mag = mag / total

    cap = float(cap)
    if cap <= 0:
        return mag * sign
    cap = min(cap, 1.0)
    if cap * w.size < 1.0:
        cap = 1.0 / w.size

    for _ in range(200):
        over = mag > cap + 1e-15
        if not over.any():
            break
        excess = float((mag[over] - cap).sum())
        mag[over] = cap
        free = ~over
        if not free.any():
            break
        headroom = mag[free].sum()
        if headroom <= 0:
            mag[free] += excess / float(free.sum())
        else:
            mag[free] += excess * (mag[free] / headroom)
    total = float(mag.sum())
    if total > 0:
        mag = mag / total
    return mag * sign


def _hrp_weights(matrix: "Any") -> "Any":
    """Compact hierarchical risk parity (Lopez de Prado, 2016)."""
    import numpy as np
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import squareform

    x = np.asarray(matrix, dtype=float)
    n = x.shape[1]
    if n == 1:
        return np.ones(1)
    corr = np.nan_to_num(np.corrcoef(x, rowvar=False), nan=0.0)
    np.fill_diagonal(corr, 1.0)
    # keep the correlation matrix positive semi-definite enough for a distance
    corr = np.clip(corr, -0.999, 0.999)
    dist = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, 1.0))
    condensed = squareform(dist, checks=False)
    link = linkage(condensed, method="single")
    order = _quasi_diagonal(link, n)
    cov = np.cov(x, rowvar=False)
    cov = np.atleast_2d(cov)
    weights = np.ones(n, dtype=float)
    clusters = [order]
    while clusters:
        nxt: List[List[int]] = []
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            split = len(cluster) // 2
            left, right = cluster[:split], cluster[split:]
            var_l = _cluster_var(cov, left)
            var_r = _cluster_var(cov, right)
            denom = var_l + var_r
            alpha = 0.5 if denom <= 0 else 1.0 - var_l / denom
            weights[left] *= alpha
            weights[right] *= 1.0 - alpha
            nxt.extend([left, right])
        clusters = [c for c in nxt if len(c) > 1]
    total = weights.sum()
    return weights / total if total > 0 else np.ones(n) / n


def _quasi_diagonal(link: "Any", n: int) -> List[int]:
    import numpy as np

    link = np.asarray(link, dtype=float)
    order = [int(link[-1, 0]), int(link[-1, 1])]
    while max(order) >= n:
        expanded: List[int] = []
        for node in order:
            if node < n:
                expanded.append(node)
                continue
            row = link[node - n]
            expanded.extend([int(row[0]), int(row[1])])
        order = expanded
    return order


def _cluster_var(cov: "Any", idx: Sequence[int]) -> float:
    import numpy as np

    if not idx:
        return 0.0
    sub = cov[np.ix_(idx, idx)]
    ones = np.ones(len(idx))
    denom = float(ones @ np.linalg.pinv(sub) @ ones)
    return 1.0 / denom if denom > 0 else 0.0


def persist_combination(
    db_root: str,
    combo: Dict[str, Any],
    *,
    apply: bool,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the combination as a first-class factor (metadata + values)."""
    import hashlib

    signature = json.dumps(
        {
            "members": combo["member_factor_ids"],
            "weights": [round(w, 10) for w in combo["weights"]],
            "method": combo["method"],
        },
        sort_keys=True,
    )
    digest = hashlib.md5(signature.encode("utf-8")).hexdigest()[:8]
    factor_id = f"combo_{digest}"
    payload = {
        "factor_id": factor_id,
        "miner_type": "LinearCombination",
        "lifecycle_status": "DISCOVERED_UNVERIFIED",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "member_factor_ids": combo["member_factor_ids"],
        "logic_reference": {
            "type": "linear_combination",
            "member_factor_ids": combo["member_factor_ids"],
            "weights": combo["weights"],
            "method": combo["method"],
            "note": note,
        },
        "metrics": {"gate_pass": False, "combination_method": combo["method"]},
    }
    if not apply:
        return {"dry_run": True, "factor_id": factor_id, "metadata": payload}

    roots = _roots(db_root)
    meta_path = roots["metadata"] / f"{factor_id}.json"
    if meta_path.exists():
        raise ValueError(f"{factor_id} already exists in {roots['metadata']}")

    # Materialise the weighted, cross-sectionally standardised panel so the
    # combination can be backtested without re-deriving the members.
    try:
        import numpy as np
        import pandas as pd

        panels = {}
        for fid in combo["member_factor_ids"]:
            panel = load_panel(db_root, fid)
            if panel is not None:
                panels[fid] = panel.rank(axis=1, pct=True)
        if panels:
            index = None
            columns = None
            for panel in panels.values():
                index = panel.index if index is None else index.union(panel.index)
                columns = panel.columns if columns is None else columns.union(panel.columns)
            total = None
            for fid, weight in zip(combo["member_factor_ids"], combo["weights"]):
                panel = panels.get(fid)
                if panel is None:
                    continue
                scaled = panel.reindex(index=index, columns=columns) * float(weight)
                total = scaled if total is None else total.add(scaled, fill_value=0.0)
            if total is not None:
                long = (
                    total.rename_axis(index="timestamp", columns="asset")
                    .stack().dropna()
                    .rename("factor")
                    .reset_index()
                )
                long["forward_return"] = np.nan
                long.to_parquet(roots["values"] / f"{factor_id}.parquet", index=False)
                payload["data_lineage"] = {
                    "snapshot_file": f"{factor_id}.parquet",
                    "snapshot_rows": int(len(long)),
                }
    except Exception as exc:  # pragma: no cover - values are best effort
        payload["metrics"]["value_export_error"] = str(exc)

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return {"dry_run": False, "factor_id": factor_id, "path": str(meta_path)}


# --------------------------------------------------------------------------- #
# decay (O2 factor retirement)
# --------------------------------------------------------------------------- #
def decay_report(
    db_root: str,
    factor_ids: Sequence[str],
    *,
    window: int = 90,
    threshold: float = 0.0,
) -> Dict[str, Any]:
    """Trailing RankIC of each stored factor over its most recent ``window`` bars.

    A factor that passed the gate in-sample can decay: the market adapts. The
    only honest way to notice is to recompute the signal on the newest data the
    library holds and compare it with the promotion-time evidence. ``threshold``
    is the trailing RankIC below which the factor is flagged ``DECAYED``.

    This reads the stored snapshot (factor vs. its forward return), so it needs
    no re-mining and no re-download — but it IS the in-sample window until the
    library is refreshed, so a flag here is a prompt to re-audit, not proof.
    """
    import numpy as np
    import pandas as pd

    rows: List[Dict[str, Any]] = []
    for fid in factor_ids:
        path = _snapshot_path(db_root, fid)
        if not path.is_file():
            rows.append({"factor_id": fid, "status": "no_snapshot"})
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            rows.append({"factor_id": fid, "status": "unreadable", "error": str(exc)})
            continue
        if "forward_return" not in frame.columns or frame["forward_return"].isna().all():
            rows.append({"factor_id": fid, "status": "no_forward_returns"})
            continue
        data = frame.dropna(subset=["factor", "forward_return"])
        if data.empty:
            rows.append({"factor_id": fid, "status": "no_overlap"})
            continue
        if "asset" in data.columns:
            keep = sorted(data["timestamp"].unique())[-max(2, int(window)):]
            data = data[data["timestamp"].isin(keep)]
            ranked = data.copy()
            ranked["f"] = ranked.groupby("timestamp")["factor"].rank()
            ranked["r"] = ranked.groupby("timestamp")["forward_return"].rank()
            ic = ranked.groupby("timestamp").apply(
                lambda g: g["f"].corr(g["r"]) if len(g) >= 3 else np.nan
            )
        else:
            ic = data["factor"].rolling(max(3, int(window))).corr(
                data["forward_return"]
            ).dropna()
        ic = ic.replace([np.inf, -np.inf], np.nan).dropna()
        if ic.empty:
            rows.append({"factor_id": fid, "status": "insufficient_data"})
            continue
        trailing = float(ic.tail(max(5, int(window))).mean())
        flagged = trailing <= float(threshold)
        rows.append({
            "factor_id": fid,
            "status": "DECAYED" if flagged else "ok",
            "trailing_rank_ic": trailing,
            "n_obs": int(ic.size),
            "window": int(window),
            "threshold": float(threshold),
        })
    return {
        "db_root": str(Path(db_root).resolve()),
        "window": int(window),
        "threshold": float(threshold),
        "n_flagged": sum(1 for r in rows if r["status"] == "DECAYED"),
        "factors": rows,
    }


def apply_decay(db_root: str, payload: Dict[str, Any]) -> List[str]:
    """Set ``lifecycle_status = DECAYED`` on the flagged factors."""
    meta_dir = _roots(db_root)["metadata"]
    updated: List[str] = []
    for row in payload.get("factors", []):
        if row.get("status") != "DECAYED":
            continue
        path = meta_dir / f"{row['factor_id']}.json"
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        meta["previous_lifecycle_status"] = meta.get("lifecycle_status")
        meta["lifecycle_status"] = "DECAYED"
        meta.setdefault("decay", {}).update({
            "flagged_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "trailing_rank_ic": row.get("trailing_rank_ic"),
            "window": payload.get("window"),
            "threshold": payload.get("threshold"),
        })
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
        updated.append(str(row["factor_id"]))
    return updated


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_factor_db_cmd(args: argparse.Namespace) -> int:
    action = args.action
    db_root = args.db_root
    try:
        if action == "list":
            payload = list_library(db_root)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                _print_list(payload, expr_width=args.expr_width)
            return 0

        if action == "show":
            payload = show_factor(db_root, args.factor_id)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                print(f"factor     : {payload['factor_id']}")
                print(f"expression : {payload['expression'] or '-'}")
                meta = payload["metadata"]
                print(f"status     : {meta.get('lifecycle_status')}")
                print(f"miner      : {meta.get('miner_type')}")
                print(f"created    : {meta.get('created_at')}")
                metrics = _metrics(meta)
                for key in (
                    "gate_pass", "hac_t", "p_adj", "dsr", "empirical_p",
                    "RankIC", "RankIC_oos", "cost_adj_return", "ls_turnover",
                    "n_trials", "fitness_score",
                ):
                    if key in metrics:
                        print(f"  {key:<14}: {metrics[key]}")
                checks = metrics.get("gate_checks") or {}
                if checks:
                    print("  gate_checks   : " + ", ".join(
                        f"{k}={'ok' if v else 'FAIL'}" for k, v in checks.items()
                    ))
                snap = payload["snapshot"]
                if snap:
                    print(
                        f"snapshot   : {snap['rows']} rows x {snap['assets']} assets, "
                        f"coverage {snap['coverage']:.1%}, {snap['start']} -> {snap['end']}"
                    )
            return 0

        if action == "rm":
            if not args.yes and not args.dry_run:
                print(
                    "factor-db rm: refusing to move anything without --yes "
                    "(use --dry-run to preview). Deletion is a soft move into "
                    "_deleted_<date>/ inside the library.",
                    file=sys.stderr,
                )
                return 2
            payload = delete_factor(db_root, args.factor_id, apply=not args.dry_run)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            elif payload["dry_run"]:
                print(f"would move {len(payload['would_move'])} file(s) to {payload['bucket']}")
                for path in payload["would_move"]:
                    print(f"  {path}")
            else:
                print(f"moved {payload['factor_id']} -> {payload['bucket']}")
            return 0

        if action == "export":
            ids = _resolve_ids(db_root, args)
            payload = export_factors(
                db_root, ids, out_dir=args.out, fmt=args.format, wide=not args.long
            )
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                print(f"exported {len(payload['written'])} factor(s) to {payload['out_dir']}")
                for path in payload["written"]:
                    print(f"  {path}")
                if payload["missing"]:
                    print(
                        f"  missing value snapshots: {', '.join(payload['missing'])}",
                        file=sys.stderr,
                    )
            return 0 if payload["written"] else 1

        if action == "corr":
            ids = _resolve_ids(db_root, args)
            payload = correlation_matrix(
                db_root, ids, method=args.method, threshold=args.threshold
            )
            if args.out:
                _write_corr_csv(payload, args.out)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                _print_corr(payload)
                if args.out:
                    print(f"\nmatrix written: {os.path.abspath(args.out)}")
            return 0

        if action == "decay":
            ids = _resolve_ids(db_root, args)
            payload = decay_report(
                db_root, ids, window=args.window, threshold=args.threshold
            )
            if args.apply:
                payload["updated"] = apply_decay(db_root, payload)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                print(f"window    : last {payload['window']} observations")
                print(f"threshold : trailing RankIC <= {payload['threshold']}")
                print(f"flagged   : {payload['n_flagged']}/{len(payload['factors'])}")
                for row in payload["factors"]:
                    ic = row.get("trailing_rank_ic")
                    if ic is None:
                        print(f"  {row['factor_id']:<14} {row['status']}")
                    else:
                        print(
                            f"  {row['factor_id']:<14} {row['status']:<8} "
                            f"trailing IC {ic:+.4f} (n={row['n_obs']})"
                        )
                if args.apply:
                    print(f"updated   : {len(payload.get('updated') or [])} metadata record(s)")
                else:
                    print("dry run: pass --apply to mark the flagged factors DECAYED")
            return 0

        if action == "combine":
            ids = _resolve_ids(db_root, args)
            combo = build_combination(
                db_root,
                ids,
                method=args.method,
                returns_root=args.returns_root,
                ridge_alpha=args.ridge_alpha,
                max_weight=args.max_weight,
            )
            result = persist_combination(
                db_root, combo, apply=not args.dry_run, note=args.note
            )
            payload = {**combo, **result}
            if args.out:
                with open(args.out, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                print(f"method   : {combo['method']}")
                for fid, weight in zip(combo["member_factor_ids"], combo["weights"]):
                    print(f"  {weight:+.4f}  {fid}")
                action_word = "would persist" if result["dry_run"] else "persisted"
                print(f"{action_word} {result['factor_id']}")
            return 0
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"factor-db {action}: {exc}", file=sys.stderr)
        return 1
    return 2


def _resolve_ids(db_root: str, args: argparse.Namespace) -> List[str]:
    raw = getattr(args, "ids", None)
    if raw:
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    metas = load_metas(db_root)
    return [str(m.get("factor_id")) for m in metas]


def _write_corr_csv(payload: Dict[str, Any], path: str) -> None:
    ids = payload["ids"]
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("," + ",".join(ids) + "\n")
        for row in ids:
            values = [payload["matrix"][row][col] for col in ids]
            handle.write(
                row + "," + ",".join(
                    "" if v != v else f"{v:.6f}" for v in values
                ) + "\n"
            )


def _print_corr(payload: Dict[str, Any]) -> None:
    print(f"method    : {payload['method']} (cross-sectional rank)")
    print(f"factors   : {len(payload['ids'])}")
    if payload["missing"]:
        print(f"missing   : {', '.join(payload['missing'])}")
    pairs = payload["redundant_pairs"]
    print(f"redundant : {len(pairs)} pair(s) with |rho| >= {payload['threshold']}")
    for pair in pairs[:20]:
        print(f"  {pair['corr']:+.3f}  {pair['a']} ~ {pair['b']}")


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "factor-db", help="Inspect and manage the production factor library"
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="What is in the library?")
    p_list.add_argument("--expr-width", type=int, default=48)
    p_list.set_defaults(func=run_factor_db_cmd)

    p_show = sub.add_parser("show", help="Full metadata + snapshot stats for one factor")
    p_show.add_argument("factor_id")
    p_show.set_defaults(func=run_factor_db_cmd)

    p_rm = sub.add_parser("rm", help="Soft-delete one factor into _deleted_<date>/")
    p_rm.add_argument("factor_id")
    p_rm.add_argument("--yes", action="store_true", help="Actually move the files")
    p_rm.add_argument("--dry-run", action="store_true", help="Only list what would move")
    p_rm.set_defaults(func=run_factor_db_cmd)

    p_export = sub.add_parser("export", help="Export factor values to csv/parquet")
    p_export.add_argument("--ids", default=None, help="Comma-separated ids (default: all)")
    p_export.add_argument("--out", required=True, help="Output directory")
    p_export.add_argument("--format", default="parquet", choices=["parquet", "csv"])
    p_export.add_argument("--long", action="store_true", help="Long (timestamp, asset) form")
    p_export.set_defaults(func=run_factor_db_cmd)

    p_corr = sub.add_parser("corr", help="Pairwise correlation + redundant pairs")
    p_corr.add_argument("--ids", default=None, help="Comma-separated ids (default: all)")
    p_corr.add_argument("--method", default="spearman", choices=["spearman", "pearson"])
    p_corr.add_argument("--threshold", type=float, default=0.7)
    p_corr.add_argument("--out", default=None, help="Write the matrix as CSV")
    p_corr.set_defaults(func=run_factor_db_cmd)

    p_combo = sub.add_parser("combine", help="Fit and persist a combination factor")
    p_combo.add_argument("--ids", default=None, help="Comma-separated ids (default: all)")
    p_combo.add_argument(
        "--method", default="equal", choices=["equal", "ic_weight", "ridge", "hrp"]
    )
    p_combo.add_argument("--returns-root", default=None, help="Library holding forward returns")
    p_combo.add_argument("--ridge-alpha", type=float, default=5.0)
    p_combo.add_argument("--max-weight", type=float, default=0.5)
    p_combo.add_argument("--note", default=None)
    p_combo.add_argument("--out", default=None, help="Write the combo definition as JSON")
    p_combo.add_argument("--dry-run", action="store_true")
    p_combo.set_defaults(func=run_factor_db_cmd)

    p_decay = sub.add_parser(
        "decay", help="Trailing RankIC check; flag factors that lost their edge"
    )
    p_decay.add_argument("--ids", default=None, help="Comma-separated ids (default: all)")
    p_decay.add_argument("--window", type=int, default=90, help="Trailing observations")
    p_decay.add_argument(
        "--threshold", type=float, default=0.0,
        help="Flag when trailing RankIC <= this value",
    )
    p_decay.add_argument("--apply", action="store_true", help="Write lifecycle_status=DECAYED")
    p_decay.set_defaults(func=run_factor_db_cmd)

    for child in (p_list, p_show, p_rm, p_export, p_corr, p_combo, p_decay):
        child.add_argument("--json", action="store_true", help="Machine-readable output")
        # ``--db-root`` lives on the leaf so both `factor-db --db-root X list`
        # and `factor-db list --db-root X` resolve to the same library.
        child.add_argument(
            "--db-root", default="factor_db", help="Production library root"
        )


__all__ = [
    "add_parser",
    "apply_decay",
    "build_combination",
    "correlation_matrix",
    "decay_report",
    "delete_factor",
    "export_factors",
    "list_library",
    "persist_combination",
    "show_factor",
]
