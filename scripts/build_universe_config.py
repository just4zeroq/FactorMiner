"""Turn a materialised universe file into a mining config.

Keeping this separate from the downloader matters for reproducibility: the
universe JSON records *what the exchange listed* at discovery time, while the
config records *what the miner was allowed to use*. When an exchange delists a
symbol the universe file still has it; the config is the frozen decision.

Two rules are applied, both about data availability only (never returns):

* a symbol must have at least ``--min-bars`` bars inside the mining window —
  a market listed three days ago cannot be a cross-sectional bet;
* a symbol must have at least one bar in the test window, otherwise "OOS
  consistency" is vacuous for it.

Usage::

    python scripts/build_universe_config.py \\
        --universe user_workspace/universes/binance_futures_usdt_1d.json \\
        --template user_workspace/configs/configExperiment.json \\
        --out user_workspace/configs/configAllUSDTFutures.json \\
        --mine-start 2021-01-01 --mine-end 2026-06-30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.data_feed.naming import data_path  # noqa: E402


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a mining config from a universe file.")
    parser.add_argument("--universe", required=True)
    parser.add_argument("--template", default="user_workspace/configs/configExperiment.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--mine-start", default="2021-01-01")
    parser.add_argument("--mine-end", default="2026-06-30")
    parser.add_argument("--test-start", default="2026-07-01")
    parser.add_argument("--test-end", default="2026-09-13")
    parser.add_argument("--min-bars", type=int, default=250, help="bars required inside the mining window")
    parser.add_argument(
        "--coverage-policy",
        default="panel",
        choices=["panel", "all_pairs"],
        help="doctor coverage rule; panel = minimum cross-section width per bar",
    )
    parser.add_argument(
        "--min-assets-per-bar",
        type=int,
        default=50,
        help="with --coverage-policy panel: smallest acceptable cross-section",
    )
    parser.add_argument("--population", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--comment", default=None)
    return parser.parse_args(argv)


def _bar_count(path: Path, start: dt.date, end: dt.date) -> int:
    if not path.exists():
        return 0
    try:
        dates = pd.to_datetime(pd.read_feather(path, columns=["date"])["date"], errors="coerce").dropna()
    except Exception:  # noqa: BLE001 - an unreadable file simply cannot be mined
        return 0
    return int(((dates.dt.date >= start) & (dates.dt.date <= end)).sum())


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    universe = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    template = json.loads(Path(args.template).read_text(encoding="utf-8-sig"))

    exchange = universe["exchange"]
    trade_type = universe["trade_type"]
    timeframe = universe["timeframe"]
    symbols: List[str] = universe.get("symbols") or [m["symbol"] for m in universe.get("markets", [])]

    mine_start = dt.date.fromisoformat(args.mine_start)
    mine_end = dt.date.fromisoformat(args.mine_end)
    test_start = dt.date.fromisoformat(args.test_start)
    test_end = dt.date.fromisoformat(args.test_end)

    kept: List[str] = []
    dropped: List[Dict[str, Any]] = []
    for symbol in symbols:
        path = data_path(args.data_root, exchange, symbol, timeframe, trade_type)
        mine_bars = _bar_count(path, mine_start, mine_end)
        test_bars = _bar_count(path, test_start, test_end)
        if mine_bars < args.min_bars:
            dropped.append({"symbol": symbol, "reason": f"mine_bars={mine_bars}", "test_bars": test_bars})
            continue
        if test_bars <= 0:
            dropped.append({"symbol": symbol, "reason": "no bar in test window", "test_bars": test_bars})
            continue
        kept.append(symbol)

    config = dict(template)
    feeds = dict(config.get("data_feeds") or {})
    feeds.update(
        {
            "exchange": exchange,
            "instrument_type": trade_type,
            "timeframe": timeframe,
            "pairs": kept,
            "mine_period": [[mine_start.isoformat(), mine_end.isoformat()]],
            "test_period": [[test_start.isoformat(), test_end.isoformat()]],
            "mining_mode": "cross_asset",
            "data_root": args.data_root,
            "coverage_policy": args.coverage_policy,
            "min_assets_per_bar": args.min_assets_per_bar,
        }
    )
    config["data_feeds"] = feeds
    if args.population:
        config["population_size"] = args.population
    if args.iterations:
        config["max_iterations"] = args.iterations

    comment = args.comment or (
        f"全交易所 USDT {trade_type} 截面挖掘：universe={Path(args.universe).name} "
        f"({len(symbols)} listed -> {len(kept)} usable, min_bars={args.min_bars})，"
        f"mine {mine_start}..{mine_end}，test {test_start}..{test_end}。"
    )
    config["_comment"] = comment

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    # The audit is a report, not a runnable config. It used to be written next
    # to the config (``configX.universe.json``), which made every "all shipped
    # configs must declare X" test trip over a file that has no ``target`` /
    # ``data_quality`` section at all. Keep the configs directory containing
    # only configs: a ``configs/`` parent puts reports in its sibling
    # ``reports/``, anything else gets ``<dir>/reports``.
    if out.parent.name == "configs":
        reports_dir = out.parent.parent / "reports"
    else:
        reports_dir = out.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    audit = reports_dir / f"{out.stem}.universe.json"
    audit.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "universe": str(args.universe),
                "config": str(out),
                "rule": {"min_bars_in_mine_window": args.min_bars, "needs_bar_in_test": True},
                "listed": len(symbols),
                "usable": len(kept),
                "dropped": len(dropped),
                "dropped_detail": dropped,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"{exchange}/{trade_type}: {len(symbols)} listed -> {len(kept)} usable, {len(dropped)} dropped")
    if dropped:
        sample = ", ".join(f"{d['symbol']}({d['reason']})" for d in dropped[:5])
        print(f"  dropped e.g. {sample}")
    print(f"config -> {out}")
    print(f"audit  -> {audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
