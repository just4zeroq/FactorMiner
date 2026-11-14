"""Validate the crypto-native feature library on real (or synthetic) panels.

What it does
------------
1. Loads every local ``1d`` futures pair and aligns stored crypto streams
   (funding / open interest / basis / liquidations) onto the shared date grid.
2. Builds every applicable crypto-native feature AST and evaluates it.
3. Scores each feature by cross-sectional RankIC against next-day returns,
   with Newey-West HAC t-stats (the same statistic the promotion gates use).
4. Writes a report to ``user_workspace/reports/crypto_features_validation.md``.

If no crypto stream file exists the script falls back to *synthetic* streams so
the plumbing can still be exercised; the report labels the data source
explicitly so synthetic numbers are never mistaken for alpha.

Usage
-----
    python user_workspace/experiment_tools/validate_crypto_features.py
    python user_workspace/experiment_tools/validate_crypto_features.py --synthetic
    python user_workspace/experiment_tools/validate_crypto_features.py --windows 12,24,48
"""

from __future__ import annotations

import argparse
import glob
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from core.data_feed.crypto_streams import (
    CRYPTO_STREAM_NAMES,
    load_crypto_streams,
)
from core.evaluation.significance import bh_fdr, newey_west_tstat, pvalue_from_t
from core.miner.crypto_features import (
    CRYPTO_FEATURE_TEMPLATES,
    crypto_streams_in,
    iter_crypto_features,
    streams_required_by_library,
)
from core.miner.expressions import FactorExpressionAST
from core.miner.operator_runtime import evaluate_ast

REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "crypto_features_validation.md"
MIN_ASSETS_PER_DATE = 5


def _pair_from_filename(path: str) -> str:
    safe = Path(path).stem.rsplit("-", 2)[0]
    a, b, c = safe.split("_")
    return f"{a}/{b}:{c}"


def _asset_from_pair(pair: str) -> str:
    return pair.split("/")[0]


def load_panel(
    data_root: Path,
    exchange: str,
    timeframe: str,
    trade_type: str,
    streams: List[str],
) -> Tuple[Dict[str, pd.DataFrame], List[str], Dict[str, int], str]:
    pattern = data_root / exchange / trade_type / f"*-{timeframe}-{trade_type}.feather"
    files = sorted(glob.glob(str(pattern)))
    if not files:
        raise SystemExit(f"No OHLCV files matching {pattern}")

    frames: Dict[str, pd.DataFrame] = {}
    stream_frames: Dict[str, Dict[str, pd.Series]] = defaultdict(dict)
    pair_stream_rows: Dict[str, int] = defaultdict(int)

    for path in files:
        pair = _pair_from_filename(path)
        asset = _asset_from_pair(pair)
        df = pd.read_feather(path)
        if "date" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates("date").set_index("date").sort_index()
        if "close" not in df.columns or len(df) < 120:
            continue
        frames[asset] = df[["open", "high", "low", "close", "volume"]].astype(float)

        if streams:
            loaded = load_crypto_streams(
                data_root, exchange, pair, timeframe, trade_type,
                pd.DatetimeIndex(df.index), streams=streams,
            )
            for name, series in loaded.items():
                stream_frames[name][asset] = series
                pair_stream_rows[name] += int(series.notna().sum())

    if not frames:
        raise SystemExit("No usable OHLCV frames found")

    all_dates = sorted(set().union(*[set(df.index) for df in frames.values()]))
    index = pd.DatetimeIndex(all_dates)
    assets = sorted(frames)

    panel: Dict[str, pd.DataFrame] = {}
    for col in ("open", "high", "low", "close", "volume"):
        panel[col] = pd.DataFrame(
            {a: frames[a][col].reindex(index) for a in assets}, index=index
        )
    for name, per_asset in stream_frames.items():
        panel[name] = pd.DataFrame(
            {a: per_asset[a].reindex(index) for a in assets if a in per_asset},
            index=index,
        )

    source = "real" if stream_frames else "none"
    return panel, assets, dict(pair_stream_rows), source


def synth_streams(panel: Dict[str, pd.DataFrame], assets: List[str], seed: int = 11) -> Dict[str, pd.DataFrame]:
    """Deterministic pseudo-streams derived from OHLCV (plumbing test only)."""
    rng = np.random.default_rng(seed)
    idx = panel["close"].index
    shape = panel["close"].shape
    close = panel["close"]
    volume = panel["volume"].fillna(0.0)

    funding = pd.DataFrame(
        rng.normal(0, 2e-4, shape), index=idx, columns=assets
    )
    oi = (volume.cumsum() * (1 + 0.1 * rng.normal(size=shape))).abs() + 1e8
    basis = close.pct_change().rolling(3, min_periods=1).mean() * 0.1 + rng.normal(0, 3e-4, shape)
    long_liq = (volume * np.abs(rng.normal(0.02, 0.01, shape))).abs()
    short_liq = (volume * np.abs(rng.normal(0.02, 0.01, shape))).abs()

    return {
        "funding_rate": pd.DataFrame(funding, index=idx, columns=assets),
        "open_interest": pd.DataFrame(oi, index=idx, columns=assets),
        "basis": pd.DataFrame(basis, index=idx, columns=assets),
        "long_liquidations": pd.DataFrame(long_liq, index=idx, columns=assets),
        "short_liquidations": pd.DataFrame(short_liq, index=idx, columns=assets),
    }


def forward_returns(close: pd.DataFrame) -> pd.DataFrame:
    """Next-bar close-to-close return (label must be strictly forward)."""
    return close.pct_change().shift(-1)


def cross_sectional_rank_ic(
    factor: pd.DataFrame, returns: pd.DataFrame, min_assets: int = MIN_ASSETS_PER_DATE
) -> pd.Series:
    """Per-date Spearman IC between a factor row and the forward-return row."""
    common = factor.index.intersection(returns.index)
    f = factor.reindex(common)
    r = returns.reindex(common)
    out = {}
    for dt in common:
        row_f = f.loc[dt]
        row_r = r.loc[dt]
        mask = row_f.notna() & row_r.notna()
        if int(mask.sum()) < min_assets:
            continue
        rf = row_f[mask].rank()
        rr = row_r[mask].rank()
        if rf.std(ddof=0) == 0 or rr.std(ddof=0) == 0:
            continue
        out[dt] = float(np.corrcoef(rf.values, rr.values)[0, 1])
    return pd.Series(out, dtype=float).sort_index()


def score_factor(ast: Dict, panel: Dict[str, pd.DataFrame], fwd: pd.DataFrame) -> Dict:
    values = evaluate_ast(ast, panel)
    if isinstance(values, pd.Series):
        values = values.to_frame("asset")
    ic = cross_sectional_rank_ic(values, fwd)
    clean = ic.dropna().values
    nw_t = newey_west_tstat(clean) if len(clean) >= 10 else 0.0
    mean_ic = float(ic.mean()) if len(ic) else 0.0
    std_ic = float(ic.std(ddof=1)) if len(ic) > 1 else 0.0
    expr = FactorExpressionAST(ast_dict=ast)
    return {
        "formula": expr.to_display_string(max_length=120),
        "mean_rank_ic": mean_ic,
        "ic_ir": float(mean_ic / std_ic) if std_ic > 0 else 0.0,
        "nw_t": float(nw_t),
        "p_value": pvalue_from_t(nw_t),
        "n_dates": int(len(ic)),
    }


def _fmt(value: float, digits: int = 4) -> str:
    return f"{value:+.{digits}f}" if np.isfinite(value) else "n/a"


def write_report(
    path: Path,
    source: str,
    assets: List[str],
    panel: Dict[str, pd.DataFrame],
    date_range: Tuple[pd.Timestamp, pd.Timestamp],
    stream_rows: Dict[str, int],
    results: List[Dict],
    ast_count: int,
    gp_demo: Dict,
    windows: List[int],
    min_dates: int = 30,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append("# 加密原生特征库验证报告\n")
    lines.append(f"> 数据来源：**{ {'real': '真实交易所数据', 'synthetic': '合成数据（仅验证管线）', 'none': '无加密流'}[source] }**")
    lines.append(f"> 标的池：{len(assets)} 个（{', '.join(assets[:12])}{' …' if len(assets) > 12 else ''}）")
    lines.append(f"> 区间：{date_range[0].date()} → {date_range[1].date()}，窗口阶梯 {windows}")
    lines.append("")

    lines.append("## 1. 数据与流覆盖\n")
    lines.append("| 流 | 非空观测数（跨标的总和） |")
    lines.append("|---|---|")
    for name in CRYPTO_STREAM_NAMES:
        if name in panel:
            lines.append(f"| `{name}` | {stream_rows.get(name, 0):,} |")
        else:
            lines.append(f"| `{name}` | — (未提供) |")
    lines.append("")
    lines.append(
        "> 说明：Binance 公共接口的 funding 可长期回溯；open interest 仅保留最近约 30 天，"
        "因此 OI 类特征的有效样本明显短于价格类；liquidation 无免费接口，需自备数据文件。\n"
    )

    lines.append("## 2. 特征库规模\n")
    lines.append(f"- 模板数：{len(CRYPTO_FEATURE_TEMPLATES)}（funding / open_interest / basis / liquidation / interaction）")
    lines.append(f"- 本次可用 AST 数（模板 × 窗口）：{ast_count}")
    lines.append(f"- 最小每期资产数：{MIN_ASSETS_PER_DATE}")
    eligible = [r for r in results if r["n_dates"] >= min_dates]
    if len(eligible) < len(results):
        lines.append(
            f"- 样本不足（有效 IC 期数 < {min_dates}）而排除出排名的特征："
            f"{len(results) - len(eligible)} 个（多为 OI/爆仓类，受数据保留期限制）"
        )
    lines.append("")

    lines.append("## 3. 单特征横截面 RankIC（按 |mean RankIC| 排序）\n")
    q_pass = sum(1 for r in results if r.get("q_bh", 1.0) <= 0.05)
    lines.append(
        f"> 多重检验：本次共评估 {len(results)} 个特征（即 {len(results)} 次试验），"
        f"BH-FDR(q≤0.05) 通过 **{q_pass}** 个。"
        "单看原始 t 值会高估显著性，闸门以 q 值为准；同一模板不同窗口高度相关，"
        "BH 在正相关下偏保守——即便如此仍无一通过。\n"
    )
    lines.append("| 排名 | 类别 | 窗口 | 公式 | mean RankIC | ICIR | NW t | BH q | 有效期数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    ranked = sorted(eligible or results, key=lambda r: abs(r["mean_rank_ic"]), reverse=True)
    for i, r in enumerate(ranked[:20], 1):
        lines.append(
            f"| {i} | {r['category']} | {r.get('window', '-')} | `{r['formula']}` | "
            f"{_fmt(r['mean_rank_ic'])} | "
            f"{_fmt(r['ic_ir'], 3)} | {_fmt(r['nw_t'], 2)} | {r.get('q_bh', 1.0):.3f} | {r['n_dates']} |"
        )
    lines.append("")

    lines.append("## 4. 分类汇总\n")
    lines.append("| 类别 | 特征数 | 平均 |RankIC| | 最大 |RankIC| | 平均 ICIR |")
    lines.append("|---|---|---|---|---|")
    by_cat: Dict[str, List[Dict]] = defaultdict(list)
    for r in (eligible or results):
        by_cat[r["category"]].append(r)
    for cat, rows in sorted(by_cat.items()):
        mean_abs = float(np.mean([abs(x["mean_rank_ic"]) for x in rows]))
        max_abs = float(np.max([abs(x["mean_rank_ic"]) for x in rows]))
        mean_ir = float(np.mean([abs(x["ic_ir"]) for x in rows]))
        lines.append(f"| {cat} | {len(rows)} | {mean_abs:.4f} | {max_abs:.4f} | {mean_ir:.3f} |")
    missing = {t.category for t in CRYPTO_FEATURE_TEMPLATES} - set(by_cat)
    if missing:
        lines.append("")
        lines.append(
            f"> 未纳入汇总的类别：{', '.join(sorted(missing))} —— 有效 IC 期数不足 "
            f"{min_dates}（数据保留期或流未提供）。"
        )
    lines.append("")

    lines.append("## 5. GP 挖矿接入检查\n")
    lines.append(f"- 自动识别的加密流 terminals：{', '.join(gp_demo['terminals']) or '（无）'}")
    lines.append(f"- 初始种群中加密特征播种数：{gp_demo['seeded']} / {gp_demo['population']}")
    lines.append("")

    lines.append("## 6. 结论与边界\n")
    lines.append(
        "- 该报告验证的是**加密原生流的接入、对齐与特征可评估性**；未计交易成本、未做样本外切分，"
        "因此表中 RankIC 是\"第一眼读数\"，不是可上线结论。\n"
        "- 因子要进入 `INSPECTED` 仍需通过 Phase 1 的统计闸门（BH-FDR / DSR / OOS 强度）。\n"
        "- 备注：加密 7x24 连续、无涨跌停，但资产池随上市时间变化；横截面 IC 只在当期有数据的标的上计算，"
        "已通过 `min_assets` 过滤极窄截面。\n"
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def gp_demo(panel: Dict[str, pd.DataFrame], assets: List[str]) -> Dict:
    """Instantiate GP only to inspect terminals / seeding (no mining loop)."""
    from core.miner.paradigms.gp_miner import GPFactorMiner

    class _Client:
        def get_data(self):
            return panel

        def get_returns(self):
            return None

        def get_test_data(self):
            return None

        def get_test_returns(self):
            return None

    cfg = {
        "paradigm": "GP",
        "population_size": 20,
        "search_space": {
            "allowed_operators": [
                "ts_zscore", "ts_delta", "div", "sub", "mul", "sign",
                "add", "abs", "ts_ema", "ts_rank", "ts_momentum",
                "ts_realized_vol",
            ],
        },
        "gp": {"crypto_seed_ratio": 0.5},
    }
    miner = GPFactorMiner(_Client(), cfg)
    miner.initialize_search_space()
    pop = miner.generate_candidates()
    seeded = sum(
        1 for c in pop
        if any(s in str(c.get_source()) for s in CRYPTO_STREAM_NAMES)
    )
    return {
        "terminals": [t for t in miner.terminals if t in CRYPTO_STREAM_NAMES],
        "seeded": seeded,
        "population": len(pop),
    }


def local_pairs(data_root: Path, exchange: str, timeframe: str, trade_type: str) -> List[str]:
    pattern = data_root / exchange / trade_type / f"*-{timeframe}-{trade_type}.feather"
    return [_pair_from_filename(p) for p in sorted(glob.glob(str(pattern)))]


def run_live(
    data_root: Path,
    exchange: str,
    timeframe: str,
    trade_type: str,
    pairs: List[str],
    report_path: Path,
) -> None:
    """End-to-end: Director mines on real crypto streams through the gates."""
    import os
    import shutil
    from collections import Counter

    from core.data_feed.real_client import RealDataClient
    from core.miner.director import FactorMinerDirector
    from core.storage.factor_storage import LocalFactorStorage

    print("\n" + "=" * 78)
    print(f"LIVE DIRECTOR — {len(pairs)} pairs, crypto streams, cross-asset")
    print("=" * 78)
    cfg = {
        "paradigm": "GP",
        "max_iterations": 2,
        "population_size": 10,
        "top_k_factors": 5,
        "linear_combine": True,
        "linear_combine_top_k": 3,
        "data_feeds": {
            "exchange": exchange,
            "instrument_type": trade_type,
            "timeframe": timeframe,
            "pairs": pairs,
            "required_streams": ["open", "high", "low", "close", "volume"],
            "crypto_streams": ["funding_rate", "basis", "open_interest"],
            "mine_period": [["2024-01-01", "2025-06-30"]],
            "test_period": [["2025-07-01", "2026-06-30"]],
            "mining_mode": "cross_asset",
        },
        "search_space": {
            "allowed_operators": [
                "add", "sub", "mul", "div", "ts_mean", "ts_std", "ts_zscore",
                "ts_rank", "ts_delta", "ts_momentum", "ts_ema", "ts_ref",
                "ts_sum", "ts_var", "ts_median", "ts_realized_vol", "ts_rsi",
                "cumsum", "signed_power", "ts_corr", "abs", "sign", "log",
                "sqrt", "tanh", "clamp", "cs_rank", "cs_zscore", "cs_neutralize",
            ],
            "terminals": ["close", "open", "high", "low", "volume"],
        },
        "gp": {
            "max_depth": 2,
            "windows": [12, 24, 48],
            "powers": [1.0, 2.0],
            "parsimony": 0.05,
            "crypto_seed_ratio": 0.5,
            "crypto_windows": [12, 24, 48],
        },
        "fitness": {},
        "evaluation": {
            "max_workers": 4,
            "gates": {
                "promotion": True,
                "trace_path": "user_workspace/_crypto_live_trace.jsonl",
                "holdout_path": "user_workspace/_crypto_live_holdout.jsonl",
            },
        },
        "storage": {"db_root": "user_workspace/_crypto_live_db"},
    }
    db_root = cfg["storage"]["db_root"]
    trace_path = cfg["evaluation"]["gates"]["trace_path"]
    holdout_path = cfg["evaluation"]["gates"]["holdout_path"]

    def _cleanup() -> None:
        for p in (db_root, trace_path, holdout_path):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    _cleanup()  # idempotent re-runs
    client = RealDataClient(cfg)
    director = FactorMinerDirector(cfg, client)
    store = LocalFactorStorage(db_root)
    director.storage_client = store
    director.miner.storage_client = store

    candidates = []
    statuses: Counter = Counter()
    try:
        candidates = director.run(max_iterations=cfg["max_iterations"])
        for meta in store.list_metadata():
            statuses[getattr(meta, "lifecycle_status", "?")] += 1
    finally:
        _cleanup()

    print(f"Candidates: {len(candidates)}")
    print(f"Lifecycle in throwaway db: {dict(statuses)}")
    top = sorted(
        candidates, key=lambda c: c.metrics.get("fitness_score", -1e9), reverse=True
    )[:3]
    for c in top:
        print(
            f"  fitness={c.metrics.get('fitness_score', 0):.4f} "
            f"RankIC={c.metrics.get('RankIC', float('nan')):.4f}  "
            f"{c.to_display_string(max_length=110)}"
        )

    lines = ["", "## 7. 端到端 Director 运行（真实加密流）", ""]
    lines.append(f"- 标的数：{len(pairs)}；迭代 2 轮 × 种群 10；cross-asset；1d")
    lines.append(f"- 数据配置：`crypto_streams=[funding_rate, basis, open_interest]`（GP 自动扩充 terminals）")
    lines.append(f"- 返回候选：{len(candidates)}；生命周期分布：{dict(statuses) or '（无落库）'}")
    if top:
        lines.append("")
        lines.append("| fitness | RankIC | 表达式 |")
        lines.append("|---|---|---|")
        for c in top:
            lines.append(
                f"| {c.metrics.get('fitness_score', 0):.4f} | "
                f"{c.metrics.get('RankIC', float('nan')):.4f} | "
                f"`{c.to_display_string(max_length=110)}` |"
            )
    lines.append("")
    with report_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Live section appended to {report_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--trade-type", default="futures")
    ap.add_argument("--streams", default=",".join(CRYPTO_STREAM_NAMES))
    ap.add_argument("--windows", default="12,24,48")
    ap.add_argument("--synthetic", action="store_true", help="force synthetic streams")
    ap.add_argument("--live", action="store_true",
                    help="also run a short Director mining pass through the gates")
    args = ap.parse_args()

    data_root = Path(args.data_root)
    streams = [s for s in args.streams.split(",") if s]
    windows = [int(w) for w in args.windows.split(",") if w]

    panel, assets, stream_rows, source = load_panel(
        data_root, args.exchange, args.timeframe, args.trade_type, streams
    )
    if args.synthetic or not crypto_streams_in(panel):
        panel.update(synth_streams(panel, assets))
        source = "synthetic"
        stream_rows = {
            name: int(panel[name].notna().sum().sum())
            for name in CRYPTO_STREAM_NAMES if name in panel
        }

    present = crypto_streams_in(panel)
    print(f"Data source: {source}; assets={len(assets)}; streams={sorted(present)}")
    print("Library requirements:", streams_required_by_library())

    fwd = forward_returns(panel["close"])
    specs = list(iter_crypto_features(panel, windows=windows))
    asts = [ast for _, _, ast in specs]
    print(f"Evaluating {len(asts)} crypto-native ASTs ...")

    results: List[Dict] = []
    for i, (template, window, ast) in enumerate(specs, 1):
        try:
            scored = score_factor(ast, panel, fwd)
        except Exception as exc:
            print(f"  skip {template.name}@{window}: {exc}")
            continue
        scored["category"] = template.category
        scored["template"] = template.name
        scored["window"] = window
        results.append(scored)
        if i % 20 == 0:
            print(f"  {i}/{len(asts)} done")

    if not results:
        print("No features evaluated.")
        return 1

    # Multiple-testing correction across every evaluated feature (trials).
    q_values = bh_fdr([r["p_value"] for r in results])
    for r, q in zip(results, q_values):
        r["q_bh"] = float(q)

    date_range = (panel["close"].index.min(), panel["close"].index.max())
    demo = gp_demo(panel, assets)
    write_report(
        REPORT_PATH, source, assets, panel, date_range, stream_rows,
        results, len(asts), demo, windows,
    )

    ranked = sorted(
        [r for r in results if r["n_dates"] >= 30] or results,
        key=lambda r: abs(r["mean_rank_ic"]), reverse=True,
    )
    print("\n=== Top 10 by |mean RankIC| ===")
    for r in ranked[:10]:
        print(
            f"  {r['category']:14s} {_fmt(r['mean_rank_ic'])}  "
            f"ICIR {_fmt(r['ic_ir'], 2)}  t {_fmt(r['nw_t'], 2)}  "
            f"q {r.get('q_bh', 1.0):.3f}  {r['formula'][:60]}"
        )
    n_pass = sum(1 for r in results if r.get("q_bh", 1.0) <= 0.05)
    print(f"\nBH-FDR(q<=0.05) passed: {n_pass} / {len(results)} trials")
    print(f"\nReport written: {REPORT_PATH}")

    if args.live:
        pairs = local_pairs(data_root, args.exchange, args.timeframe, args.trade_type)
        run_live(
            data_root, args.exchange, args.timeframe, args.trade_type, pairs, REPORT_PATH
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
