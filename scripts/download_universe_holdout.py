"""Download the history of a *clean* universe holdout (disjoint fresh symbols).

A universe holdout is only independent evidence if the reserved symbols were
never part of any mining universe. Those symbols do not exist in the local
library yet, so they must be fetched first. This script is the missing entry
point; it never runs implicitly (network access is explicit and opt-in).

    python scripts/download_universe_holdout.py --config <cfg> --dry-run
    python scripts/download_universe_holdout.py --config <cfg>
    python scripts/download_universe_holdout.py --symbols "SUI/USDT:USDT,..." \
        --start 2023-01-01

After the download, edit the config so that ``data_feeds.universe_holdout``
lists exactly these disjoint symbols, set ``universe_holdout_mode: strict``
and optionally ``universe_holdout_require_fresh: true``, then run::

    python -m core.cli experiment --config <cfg> --miner GP

A split whose reserved symbols are a subset of ``pairs`` is refused here unless
``--allow-pseudo-clean`` is given: re-reading already-mined assets is a
mechanism check, not a holdout.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.experiment import universe as uni  # noqa: E402


def _load_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"config not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config",
                    default="user_workspace/configs/configUniverseHoldout.json")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated fresh symbols; overrides the config list")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=None,
                    help="default: yesterday (T-1, no forming bar)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-pseudo-clean", action="store_true",
                    help="allow downloading a subset of the existing universe")
    ap.add_argument("--data-root", default="data")
    args = ap.parse_args(argv)

    cfg = _load_config(args.config)
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        feeds = dict(cfg.get("data_feeds") or {})
        feeds["universe_holdout"] = symbols
        cfg["data_feeds"] = feeds
    split = uni.resolve_split(cfg)

    if not split.enabled:
        raise SystemExit("no universe_holdout declared in the config")
    if not split.clean and not args.allow_pseudo_clean:
        raise SystemExit(
            "reserved symbols are a subset of the mining universe (clean=False). "
            "Declare disjoint fresh symbols, or pass --allow-pseudo-clean "
            "explicitly for a mechanism check."
        )

    feeds = cfg["data_feeds"]
    end = args.end or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    print(f"universe holdout: {len(split.train_pairs)} train / "
          f"{len(split.holdout_pairs)} reserved, clean={split.clean}")
    print(f"range {args.start} .. {end} · {feeds.get('timeframe', '1d')} · "
          f"{feeds.get('exchange', 'binance')} / {feeds.get('instrument_type', 'futures')}")
    if args.dry_run:
        for sym in split.holdout_pairs:
            print(f"  would fetch {sym}")
        print("dry run: nothing downloaded")
        return 0

    from core.data_feed.batch_downloader import SmartBatchDownloader

    downloader = SmartBatchDownloader(data_root=args.data_root)
    failed: List[str] = []
    for sym in split.holdout_pairs:
        try:
            result = downloader.download_ohlcv_batch(
                exchange_id=feeds.get("exchange", "binance"),
                symbol=sym,
                timeframe=feeds.get("timeframe", "1d"),
                start_date=args.start,
                end_date=end,
                trade_type=feeds.get("instrument_type", "futures"),
                progress_callback=None,
                download_mode="fill_gap",
            )
            ok = bool(result.get("success", True))
        except Exception as exc:  # network / exchange errors are per-symbol
            ok, result = False, {"error": str(exc)}
        print(f"  {'ok' if ok else 'FAIL'} {sym}: {result.get('error', '')}")
        if not ok:
            failed.append(sym)

    print(f"\n{len(split.holdout_pairs) - len(failed)}/"
          f"{len(split.holdout_pairs)} symbols downloaded")
    if failed:
        print("failed:", ", ".join(failed))
        return 1
    print("Next: set universe_holdout_mode=strict in the config and run "
          "`python -m core.cli experiment --config ... --miner GP`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
