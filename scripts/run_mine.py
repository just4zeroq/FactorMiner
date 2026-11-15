"""Refresh the local library to latest, then run a FactorMiner experiment.

This enforces the operating discipline agreed for the project:

* local market data is always brought current *before* mining, so a run never
  silently trains/validates on a stale panel;
* the mining config itself uses data up to the latest non-holdout bar (see
  ``configExperiment.json``: ``mine_period`` ends 2026-06-30, ``test_period``
  is the most recent window that stays clear of the 60-bar strict holdout).

The refresh step shells out to the verified ``core.cli download`` path
(incremental fill from each symbol's last stored bar to T-1). ``update_data.py``
exists for scheduled idempotent jobs, but its ``SmartBatchDownloader`` health
gate was observed to loop on a benign 91/100 score, so this wrapper uses the
cleaner direct downloader. Refresh is best-effort: a symbol that fails to
refresh is reported but does not block mining (you still mine on whatever is
locally current), with a loud summary at the end.

Usage::

    venv/Scripts/python.exe scripts/run_mine.py \
        --config user_workspace/configs/configExperiment.json

    venv/Scripts/python.exe scripts/run_mine.py --skip-refresh   # offline / dev
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.name == "nt":
    _PY = os.path.join(_PROJECT_ROOT, "venv", "Scripts", "python.exe")
else:
    _PY = os.path.join(_PROJECT_ROOT, "venv", "bin", "python")


def _run(cmd: list[str], env: dict) -> int:
    print(">>> " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=_PROJECT_ROOT, env=env).returncode


def _load_universe(config_path: str) -> dict:
    with open(config_path, encoding="utf-8-sig") as handle:
        config = json.load(handle)
    feeds = config.get("data_feeds", {}) or {}
    pairs = [p for p in feeds.get("pairs", []) if isinstance(p, str) and p.strip()]
    if not pairs:
        raise SystemExit(f"config has no data_feeds.pairs: {config_path}")
    periods = list(feeds.get("mine_period", []) or []) + list(feeds.get("test_period", []) or [])
    starts = [p[0] for p in periods if isinstance(p, (list, tuple)) and p]
    return {
        "exchange": feeds.get("exchange", "binance"),
        "trade_type": feeds.get("instrument_type", "futures"),
        "timeframe": feeds.get("timeframe", "1d"),
        "pairs": pairs,
        "earliest_period": min(starts) if starts else "2017-01-01",
    }


def _refresh_symbol(pair: str, universe: dict, until: datetime.date, env: dict) -> tuple[int, str]:
    """Refresh one symbol to ``until`` via the verified downloader. Returns (rc, detail)."""
    cmd = [
        _PY, "-m", "core.cli", "download",
        "--exchange", universe["exchange"],
        "--symbols", pair,
        "--timeframes", universe["timeframe"],
        "--type", universe["trade_type"],
        "--start", universe["earliest_period"],
        "--end", until.isoformat(),
        "--mode", "incremental",
    ]
    rc = _run(cmd, env)
    return rc, "ok" if rc == 0 else "download failed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--config",
        default=os.path.join("user_workspace", "configs", "configExperiment.json"),
        help="mining config (default: user_workspace/configs/configExperiment.json)",
    )
    ap.add_argument("--user-dir", default="user_workspace", help="workspace directory")
    ap.add_argument(
        "--skip-refresh",
        action="store_true",
        help="do NOT refresh data first (offline / dev only; you take responsibility for staleness)",
    )
    args = ap.parse_args(argv)

    env = dict(os.environ)  # inherit proxy / PATH from the caller
    until = datetime.date.today() - datetime.timedelta(days=1)  # T-1: today's bar is still forming

    if not args.skip_refresh:
        universe = _load_universe(args.config)
        print(
            f"[run_mine] step 1/2: refresh {len(universe['pairs'])} symbols to {until.isoformat()} (T-1)"
        )
        ok = fail = 0
        for pair in universe["pairs"]:
            rc, detail = _refresh_symbol(pair, universe, until, env)
            if rc == 0:
                ok += 1
            else:
                fail += 1
                print(f"  ! {pair}: {detail}", file=sys.stderr)
        print(f"[run_mine] refresh done: {ok} ok, {fail} failed")
        if fail:
            print(
                "[run_mine] WARNING: some symbols did not refresh; mining proceeds on locally "
                "current data. Fix the feed or use update_data.py for a full scheduled job.",
                file=sys.stderr,
            )

    print("[run_mine] step 2/2: run experiment")
    return _run(
        [_PY, "-m", "core.cli", "experiment", "--config", args.config, "--user-dir", args.user_dir],
        env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
