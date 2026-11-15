"""``factorminer holdout plan|reveal`` — the CLI counterpart of the ledger.

A rolling pre-registered window is useless if the only way to reveal it is a
Python snippet or the Web API. ``plan`` is read-only and explains exactly which
windows exist; ``reveal`` performs the *one* final access through
:func:`core.evaluation.holdout_guard.guard`, which writes the append-only ledger
and refuses a second read of the same bars.

Both subcommands share one data root with the doctor and the miner: an explicit
``--data-root`` wins, then ``data_feeds.data_root`` from ``--config``, then
``data``. Revealing a window that was planned against a different library would
burn the wrong bars.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from core.evaluation.holdout_guard import (
    HoldoutReuseError,
    default_ledger_path,
    enforce_governance,
    resolve_mode,
)
from core.evaluation.experiment_trace import HoldoutLedger


def _read_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read config {path}: {exc}") from exc


def _data_root(args: argparse.Namespace, config: Dict[str, Any]) -> str:
    explicit = getattr(args, "data_root", None)
    if explicit:
        return str(explicit)
    from core.data_feed.naming import resolve_data_root

    return str(resolve_data_root((config.get("data_feeds") or {}).get("data_root")))


def _plan(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    from core.experiment import windows

    spec = windows.load_spec(getattr(args, "spec", None))
    ledger_path = default_ledger_path()
    revealed = HoldoutLedger(ledger_path).snapshot()
    plan = windows.plan_windows(
        spec, data_root=_data_root(args, config), revealed=revealed
    )
    plan["ledger_path"] = ledger_path
    return plan


def _print_plan(plan: Dict[str, Any]) -> None:
    anchor = plan.get("anchor") or {}
    current = plan.get("current") or {}
    print(f"anchor       : {anchor.get('start')} ({anchor.get('source')})")
    print(
        f"window       : {plan.get('window_bars')} bars, "
        f"embargo {plan.get('embargo_bars')} bars, {plan.get('timeframe')}"
    )
    print(
        f"library      : {plan.get('n_common_bars')} common bars across "
        f"{plan.get('n_files')} files, last = {plan.get('last_common')}"
    )
    print(
        f"progress     : {current.get('accumulated')}/{current.get('min_bars')} bars, "
        f"ready = {current.get('ready')}, ETA = {current.get('eta')}"
    )
    rows = plan.get("windows") or []
    if rows:
        print(f"{'#':>3}  {'start':<12} {'end':<12} {'bars':>5}  {'state':<10} period_key")
        for window in rows:
            state = "revealed" if window.get("revealed") else (
                "ready" if window.get("ready") else "filling"
            )
            print(
                f"{window.get('index', 0):>3}  {window.get('start', ''):<12} "
                f"{window.get('end', ''):<12} {window.get('n_bars', 0):>5}  "
                f"{state:<10} {window.get('period_key', '')}"
            )
    else:
        print("windows      : none (the anchor is still ahead of the closed bars)")
    print(f"ledger       : {plan.get('ledger_path')}")


def _pick_window(plan: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    rows = plan.get("windows") or []
    explicit = getattr(args, "window", None)
    if explicit is not None:
        matches = [w for w in rows if int(w.get("index", 0)) == int(explicit)]
        if not matches:
            raise ValueError(
                f"No pre-registered window #{explicit}; run `core.cli holdout plan` "
                f"({len(rows)} planned)."
            )
        return matches[0]
    chosen = plan.get("next_ready_unrevealed")
    if not chosen:
        current = plan.get("current") or {}
        raise ValueError(
            "No ready, unrevealed window. "
            f"Progress {current.get('accumulated')}/{current.get('min_bars')} bars; "
            f"ETA {current.get('eta')}. Use `core.cli holdout plan` for details."
        )
    return chosen


def _dry_reveal(
    window: Dict[str, Any], config: Dict[str, Any], args: argparse.Namespace,
) -> Dict[str, Any]:
    """Read-only preview: no ledger, no access log, no reveal."""
    from core.evaluation import holdout_guard

    mode = holdout_guard.resolve_mode(
        config, getattr(args, "holdout_mode", None)
    )
    enforce_governance(
        mode, allow_reuse=bool(getattr(args, "allow_holdout_reuse", False))
    )
    ledger = HoldoutLedger(default_ledger_path())
    overlaps = [
        {"period_key": r.period_key, "start": r.start, "end": r.end}
        for r in ledger.overlapping(str(window["start"]), str(window["end"]))
    ]
    return {
        "dry_run": True,
        "would_reveal": not overlaps,
        "window": window,
        "mode": mode,
        "overlaps": overlaps,
    }


def _do_reveal(
    window: Dict[str, Any], config: Dict[str, Any], args: argparse.Namespace,
) -> Dict[str, Any]:
    from core.evaluation import holdout_guard

    result = holdout_guard.guard(
        window["start"],
        window["end"],
        purpose="final",
        config=config,
        mode=getattr(args, "holdout_mode", None),
        allow_reuse=bool(getattr(args, "allow_holdout_reuse", False)),
        # ``rolling_`` keeps the pre-registered anchor fixed: resolve_anchor()
        # deliberately ignores rolling_* reveals when computing the next window.
        period_key=str(window["period_key"]),
        source=f"cli:holdout reveal --window {window.get('index')}",
        note=getattr(args, "note", None) or "one-time rolling holdout reveal via CLI",
        context={
            "window_index": int(window.get("index", 0)),
            "period_key": str(window.get("period_key")),
            "timeframe": str((config.get("data_feeds") or {}).get("timeframe", "1d")),
        },
    )
    return {
        "dry_run": False,
        "would_reveal": bool(result.get("revealed")),
        "window": window,
        "guard": result,
    }


def run_holdout_cmd(args: argparse.Namespace) -> int:
    try:
        config = _read_config(getattr(args, "config", None))
        plan = _plan(args, config)
        if args.holdout_command == "plan":
            if getattr(args, "json", False):
                print(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
            else:
                _print_plan(plan)
            return 0

        window = _pick_window(plan, args)
        if not window.get("ready"):
            raise ValueError(
                f"Window #{window.get('index')} has only {window.get('n_bars')}/"
                f"{plan.get('window_bars')} bars; revealing a partial bar would "
                "burn the holdout on an unfinished candle."
            )
        if window.get("revealed") and not getattr(args, "allow_holdout_reuse", False):
            raise ValueError(
                f"Window #{window.get('index')} ({window['start']}..{window['end']}) "
                "is already revealed. Re-reading it would invalidate the result; "
                "pass --allow-holdout-reuse only to recompute a published number."
            )
        result = (
            _dry_reveal(window, config, args)
            if getattr(args, "dry_run", False)
            else _do_reveal(window, config, args)
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        else:
            action = "would reveal" if result["dry_run"] else "revealed"
            print(
                f"{action} window #{window['index']}: "
                f"{window['start']}..{window['end']} ({window['period_key']})"
            )
            if result["dry_run"] and not result["would_reveal"]:
                print("BLOCKED: overlaps an already revealed window", file=sys.stderr)
                return 1
        return 0
    except (HoldoutReuseError, ValueError) as exc:
        print(f"holdout: {exc}", file=sys.stderr)
        return 1


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "holdout",
        help="Plan or one-time reveal pre-registered rolling holdout windows",
    )
    commands = parser.add_subparsers(dest="holdout_command", required=True)

    plan = commands.add_parser("plan", help="Print the read-only window plan")
    plan.add_argument("--config", default=None, help="Config providing data_feeds.data_root")
    plan.add_argument("--data-root", default=None, help="Override the data root")
    plan.add_argument("--spec", default=None, help="Override experiment_window.json")
    plan.add_argument("--json", action="store_true", help="Machine-readable plan")
    plan.set_defaults(func=run_holdout_cmd)

    reveal = commands.add_parser(
        "reveal", help="Register a single final access to a ready window"
    )
    reveal.add_argument("--config", default=None, help="Config providing data_root / holdout_mode")
    reveal.add_argument("--data-root", default=None, help="Override the data root")
    reveal.add_argument("--spec", default=None, help="Override experiment_window.json")
    reveal.add_argument(
        "--window", type=int, default=None,
        help="1-based window index (default: first ready, unrevealed window)",
    )
    reveal.add_argument("--dry-run", action="store_true", help="Show the decision without writing")
    reveal.add_argument("--holdout-mode", choices=["strict", "audit", "off"], default=None)
    reveal.add_argument("--allow-holdout-reuse", action="store_true")
    reveal.add_argument("--note", default=None, help="Audit note for the ledger entry")
    reveal.add_argument("--json", action="store_true", help="Machine-readable result")
    reveal.set_defaults(func=run_holdout_cmd)


__all__ = ["add_parser", "run_holdout_cmd"]
