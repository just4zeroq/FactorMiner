"""Rolling pre-registered evaluation windows.

Why this replaces the static window
-----------------------------------
The old ``experiment_window.json`` was a single batch bet:
``start=2026-12-01, min_bars=90``. It had three defects.

1. The start was an arbitrary month boundary rather than the day after the
   last revealed holdout, so the 2026-11-14..2026-11-30 bars would have been
   downloaded but belonged to no registered window.
2. ``freshness.experiment_progress`` counted **calendar days** as bars
   (``(last - start).days + 1``), so any gap in a tape overstated progress.
3. One 90-bar lottery meant the platform could produce evidence only once per
   quarter. A mature platform accumulates evidence continuously: as soon as a
   small pre-registered slice is complete it becomes available exactly once.

This module plans windows in **bar space**, not calendar space. It reads the
actual timestamps present in every OHLCV file at the configured timeframe,
takes the intersection (a window is clean only if every asset has it), starts
at the day after the last ledger reveal, and chunks into
``window_bars`` + ``embargo_bars`` strides. Each chunk is a window that can be
revealed once via the existing ``holdout_guard`` / ``HoldoutLedger``.

Nothing here reveals anything. Planning is read-only.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from core.data_feed.naming import data_path
from core.evaluation.holdout_guard import (
    default_ledger_path,
    load_window_spec,
)

logger = logging.getLogger(__name__)

SPEC_NAME = "experiment_window.json"
ENV_SPEC_VAR = "FACTORMINER_WINDOW_SPEC"

TF_DAYS = {"1w": 7.0, "1d": 1.0, "4h": 1 / 6, "1h": 1 / 24,
           "15m": 1 / 96, "5m": 1 / 288, "1m": 1 / 1440}

DEFAULT_WINDOW_BARS = 30
DEFAULT_EMBARGO_BARS = 5
MAX_WINDOWS = 36


def spec_path() -> Path:
    env = os.environ.get(ENV_SPEC_VAR)
    if env:
        return Path(env)
    root = Path(__file__).resolve().parents[2]
    return root / "user_workspace" / SPEC_NAME


def load_spec(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the rolling spec; falls back to the guard's shared loader."""
    p = Path(path) if path else spec_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        logger.warning("Cannot read rolling window spec %s: %s", p, exc)
        return {}


def revealed_windows(ledger_path: Optional[str] = None) -> List[Dict[str, str]]:
    from core.evaluation.experiment_trace import HoldoutLedger

    try:
        return HoldoutLedger(ledger_path or default_ledger_path()).snapshot()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Cannot read holdout ledger: %s", exc)
        return []


def resolve_anchor(spec: Dict[str, Any],
                   revealed: Optional[Sequence[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Anchor = explicit start, or the day after the last revealed holdout."""
    entries = list(revealed if revealed is not None else revealed_windows())
    # Pre-registration means the boundary is FIXED. Only the original one-time
    # holdout anchors the timeline; a rolling window's own reveal must never
    # push the anchor forward, or every future window would silently move.
    anchored = [e for e in entries
                if not str(e.get("period_key", "")).startswith("rolling_")]
    ends = [str(e.get("end", ""))[:10] for e in anchored if e.get("end")]
    ledger_end = max(ends) if ends else None
    auto = (
        (pd.Timestamp(ledger_end) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if ledger_end else None
    )

    declared = spec.get("start")
    if declared and str(declared).lower() not in ("auto", "ledger", ""):
        chosen = str(declared)[:10]
        source = "explicit"
        # Starting LATER than the anchor throws away genuinely fresh bars.
        wasted = max(0, (pd.Timestamp(chosen) - pd.Timestamp(auto)).days) if auto else 0
        overlaps_burned = bool(auto and chosen <= ledger_end)
    elif auto:
        chosen, source = auto, "ledger_end_plus_one"
        wasted, overlaps_burned = 0, False
    else:
        chosen = (datetime.date.today()).strftime("%Y-%m-%d")
        source = "today_no_ledger"
        wasted, overlaps_burned = 0, False
    return {
        "start": chosen,
        "source": source,
        "ledger_end": ledger_end,
        "auto_anchor": auto,
        "starts_after_anchor": bool(wasted > 0),
        "wasted_days": int(wasted),
        "overlaps_burned_holdout": overlaps_burned,
    }


def _file_dates(path: Path) -> List[pd.Timestamp]:
    try:
        frame = pd.read_feather(path, columns=["date"])
    except Exception:
        try:
            frame = pd.read_feather(path)
        except Exception as exc:  # pragma: no cover - corrupt file
            logger.debug("Cannot read %s: %s", path, exc)
            return []
    if frame.empty or "date" not in frame.columns:
        return []
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    # X-07: ``normalize()`` flattened 1h/15m/5m bars to the same midnight, so
    # a module that advertises bar-space windows saw exactly one bar per day
    # (window_bars=30 silently became ~30 days). Keep the real bar timestamp.
    return [pd.Timestamp(d) for d in dates.unique()]


def common_timeline(items: Iterable[Dict[str, Any]],
                    *,
                    data_root: str = "data",
                    timeframe: str = "1d",
                    now: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    """Intersection of timestamps across every non-stream OHLCV file.

    Using the intersection (rather than the shortest tape's last date) makes
    the bar count exact even when a symbol has gaps: a window is available only
    when *all* assets actually have those bars.

    Unclosed bars are removed (review P0-d): the newest bar in a just-refreshed
    file is still forming, and counting it let a window look ``ready`` one bar
    early — revealing it burned a holdout window on a partial candle whose
    close (and therefore its forward return) would still change.
    """
    paths: List[Path] = []
    for item in items:
        if item.get("timeframe") != timeframe or item.get("stream"):
            continue
        try:
            p = data_path(data_root, item["exchange"], item["symbol"],
                          timeframe, item["trade_type"])
        except (KeyError, ValueError):
            continue
        if p.exists():
            paths.append(p)
    if not paths:
        return {"dates": [], "n_files": 0, "n_common": 0, "last": None}

    series = [_file_dates(p) for p in paths]
    series = [s for s in series if s]
    if not series:
        return {"dates": [], "n_files": len(paths), "n_common": 0, "last": None}
    common = set(series[0])
    for s in series[1:]:
        common &= set(s)
    dates = sorted(common)

    from core.data_feed.bar_clock import closed_only

    closed = closed_only(dates, timeframe, now=now)
    dropped = len(dates) - len(closed)
    dates = closed
    return {
        "dates": dates,
        "n_files": len(paths),
        "n_common": len(dates),
        "n_dropped_unclosed": dropped,
        "last": dates[-1].strftime("%Y-%m-%d") if dates else None,
    }


def _overlaps(lo: str, hi: str, entry: Dict[str, str]) -> bool:
    a, b = str(entry.get("start", ""))[:10], str(entry.get("end", ""))[:10]
    return bool(a and b and lo <= b and hi >= a)


def plan_windows(
    spec: Optional[Dict[str, Any]] = None,
    *,
    data_root: str = "data",
    items: Optional[Iterable[Dict[str, Any]]] = None,
    revealed: Optional[Sequence[Dict[str, str]]] = None,
    today: Optional[datetime.date] = None,
    now: Optional[pd.Timestamp] = None,
) -> Dict[str, Any]:
    """Plan rolling windows from the real bar timeline. Read-only.

    ``now`` (default: today at 00:00 UTC) is what decides whether the newest bar
    in the library has closed; a window is never ``ready`` on a forming bar.
    """
    spec = spec or {}
    today = today or datetime.date.today()
    if now is None:
        # Conservative: treating "today" as its 00:00 means the current day's
        # bar is never counted as finished, regardless of the wall clock.
        now = pd.Timestamp(today)
    timeframe = str(spec.get("timeframe", "1d"))
    window_bars = max(1, int(spec.get("window_bars", DEFAULT_WINDOW_BARS)))
    embargo_bars = max(0, int(spec.get("embargo_bars", DEFAULT_EMBARGO_BARS)))
    entries = list(revealed if revealed is not None else revealed_windows())
    anchor = resolve_anchor(spec, entries)

    if items is None:
        from core.data_feed.freshness import scan_library
        items = scan_library(data_root, now=now)[0]
    timeline = common_timeline(
        items, data_root=data_root, timeframe=timeframe, now=now
    )
    dates = [d for d in timeline["dates"] if d >= pd.Timestamp(anchor["start"])]

    windows: List[Dict[str, Any]] = []
    stride = window_bars + embargo_bars
    pos, index = 0, 0
    while pos < len(dates) and index < MAX_WINDOWS:
        chunk = dates[pos:pos + window_bars]
        lo, hi = chunk[0].strftime("%Y-%m-%d"), chunk[-1].strftime("%Y-%m-%d")
        ready = len(chunk) == window_bars
        windows.append({
            "index": index + 1,
            "period_key": f"rolling_{lo}_{hi}",
            "start": lo,
            "end": hi,
            "n_bars": len(chunk),
            "ready": ready,
            "revealed": any(_overlaps(lo, hi, e) for e in entries),
        })
        if not ready:
            break
        pos += stride
        index += 1

    # When the last chunk is exactly full there are no leftover bars, but the
    # *next* window has just opened. Show it as accumulating at 0 bars instead
    # of reporting the previous (already ready) window as "current".
    if windows and windows[-1]["ready"]:
        last_date = pd.Timestamp(windows[-1]["end"])
        nxt = (last_date + datetime.timedelta(days=round(
            (embargo_bars + 1) * TF_DAYS.get(timeframe, 1.0)
        ))).strftime("%Y-%m-%d")
        windows.append({
            "index": windows[-1]["index"] + 1,
            "period_key": f"rolling_{nxt}_{nxt}",
            "start": nxt,
            "end": nxt,
            "n_bars": 0,
            "ready": False,
            "revealed": False,
        })

    last = timeline["last"]
    step = TF_DAYS.get(timeframe, 1.0)
    bar = pd.Timedelta(seconds=round(step * 86400))
    if windows:
        cur = windows[-1]
        accumulated = cur["n_bars"]
        cur_start = cur["start"]
    else:
        accumulated = 0
        cur_start = anchor["start"]
    remaining = max(0, window_bars - accumulated)

    # When does bar number ``window_bars`` land? The next bar can be no earlier
    # than *both* the pre-registered anchor and the bar after the last stored
    # one. Using ``last`` alone made the ETA one bar optimistic whenever the
    # anchor was still in the future -- exactly the "0 windows yet" state, which
    # is the only state an operator ever reads the ETA in: it said 2026-12-14
    # for a 30th bar that lands on 2026-12-15.
    first_future = pd.Timestamp(cur_start)
    if last:
        first_future = max(first_future, pd.Timestamp(last) + bar)

    def _stamp(ts: pd.Timestamp) -> str:
        # Sub-daily timeframes must not be rounded to a date: 30 hourly bars fit
        # inside two days, so a date-only ETA is always "today" and useless.
        return ts.strftime("%Y-%m-%d") if bar >= pd.Timedelta(days=1) else ts.strftime("%Y-%m-%d %H:%M")

    if remaining == 0 and last:
        eta = last
    else:
        eta = _stamp(first_future + bar * (remaining - 1))

    current = {
        "label": spec.get("label", "rolling pre-registered window"),
        "start": cur_start,
        "anchor": anchor["start"],
        "anchor_source": anchor.get("source"),
        "min_bars": window_bars,
        "timeframe": timeframe,
        "accumulated": accumulated,
        "remaining": remaining,
        "coverage": round(min(1.0, accumulated / window_bars), 4),
        "ready": accumulated >= window_bars,
        "eta": eta,
        "as_of": last,
        "today": today.isoformat(),
        "rolling": True,
    }

    ready_unrevealed = [w for w in windows if w["ready"] and not w["revealed"]]
    return {
        "anchor": anchor,
        "window_bars": window_bars,
        "embargo_bars": embargo_bars,
        "timeframe": timeframe,
        "n_common_bars": timeline["n_common"],
        "n_files": timeline["n_files"],
        "last_common": last,
        "windows": windows,
        "n_ready": sum(1 for w in windows if w["ready"]),
        "n_revealed": sum(1 for w in windows if w["revealed"]),
        "next_ready_unrevealed": ready_unrevealed[0] if ready_unrevealed else None,
        "current": current,
        "note": ("windows are chunked in bar space from the day after the last "
                 "revealed holdout; each may be revealed only once"),
    }


def status(path: Optional[str] = None, *, data_root: str = "data") -> Dict[str, Any]:
    """Convenience entry for API/CLI: spec + ledger + library -> plan."""
    spec = load_spec(path)
    if not spec.get("rolling"):
        return {"rolling": False, "spec": spec,
                "hint": "set rolling=true in experiment_window.json"}
    return plan_windows(spec, data_root=data_root)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the current rolling pre-registration plan (read-only)."""
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", default=None)
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--json", action="store_true", help="emit the full plan as JSON")
    args = ap.parse_args(argv)
    spec = load_spec(args.spec)
    plan = plan_windows(spec, data_root=args.data_root)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        return 0
    cur = plan["current"]
    print(f"anchor       : {plan['anchor']['start']} ({plan['anchor']['source']})")
    print(f"window       : {plan['window_bars']} bars, embargo {plan['embargo_bars']} bars, "
          f"{plan['timeframe']}")
    print(f"library      : {plan['n_common_bars']} common bars across {plan['n_files']} files, "
          f"last = {plan['last_common']}")
    print(f"progress     : {cur['accumulated']}/{cur['min_bars']} bars, "
          f"ready = {cur['ready']}, ETA = {cur['eta']}")
    print(f"windows      : {plan['n_ready']} ready, {plan['n_revealed']} revealed")
    nxt = plan["next_ready_unrevealed"]
    if nxt:
        print(f"next clean   : {nxt['start']} .. {nxt['end']} "
              f"({nxt['period_key']}) — reveal via holdout_guard(purpose='final')")
    else:
        print("next clean   : none yet; no window may be evaluated before it is ready")
    print(f"note         : {plan['note']}")
    return 0


__all__ = [
    "ENV_SPEC_VAR",
    "SPEC_NAME",
    "common_timeline",
    "load_spec",
    "main",
    "plan_windows",
    "resolve_anchor",
    "revealed_windows",
    "spec_path",
    "status",
]


if __name__ == "__main__":
    raise SystemExit(main())
