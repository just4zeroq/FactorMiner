"""Backtest run registry for the Web UI.

A backtest is heavier than a factor query (it loads every price file and runs an
event-driven simulation), so runs are queued onto a worker thread and their
state is persisted under ``user_workspace/reports/backtest_runs/<run_id>/``.
The filesystem is the source of truth: the API can be restarted and the run
list survives.

Artifacts per run::

    status.json    # queued / running / completed / failed + request echo
    summary.json   # ml4t metrics, effective weights, expanded combinations
    equity.csv     # timestamp,equity,return,cumulative_return,drawdown,...
    trades.csv     # per-trade ledger when ml4t exposes one
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import random
import threading
import traceback
from typing import Any, Optional

from core.evaluation import holdout_guard
from core.evaluation.experiment_trace import HoldoutReuseError

logger = logging.getLogger(__name__)

BACKTEST_ROOT = os.path.join("user_workspace", "reports", "backtest_runs")
MAX_EQUITY_POINTS = 1500

_lock = threading.Lock()
_active: dict[str, threading.Thread] = {}

RUN_FIELDS = (
    "factor_ids",
    "weights",
    "start",
    "end",
    "db_root",
    "orientation_db_root",
    "initial_cash",
    "commission_rate",
    "slippage_rate",
    "funding_rate",
    "n_long",
    "n_short",
    "rebalance_every",
    "exchange",
    "timeframe",
    "trade_type",
    "data_root",
    "allow_holdout_reuse",
    "holdout_mode",
)


def _run_dir(run_id: str) -> str:
    return os.path.join(BACKTEST_ROOT, run_id)


def _write_json(path: str, payload: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        logger.warning("Unreadable backtest artifact: %s", path, exc_info=True)
        return None


def _new_run_id() -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"bt-{stamp}-{random.randint(100, 999)}"


def _sanitize(payload: dict) -> dict:
    return {key: payload.get(key) for key in RUN_FIELDS if key in payload}


def start_run(payload: dict) -> dict:
    """Queue a backtest and return the initial status record."""
    factor_ids = [str(fid).strip() for fid in (payload.get("factor_ids") or []) if str(fid).strip()]
    if not factor_ids:
        raise ValueError("Select at least one factor with a stored snapshot")

    run_id = _new_run_id()
    directory = _run_dir(run_id)
    os.makedirs(directory, exist_ok=True)

    request = _sanitize({**payload, "factor_ids": factor_ids})
    status = {
        "run_id": run_id,
        "status": "queued",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "request": request,
    }
    _write_json(os.path.join(directory, "status.json"), status)

    thread = threading.Thread(
        target=_execute, args=(run_id, request), name=f"backtest-{run_id}", daemon=True
    )
    with _lock:
        _active[run_id] = thread
    thread.start()
    return status


def _execute(run_id: str, request: dict) -> None:
    directory = _run_dir(run_id)
    status_path = os.path.join(directory, "status.json")
    status = _read_json(status_path) or {"run_id": run_id}
    status.update(
        {
            "status": "running",
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    )
    _write_json(status_path, status)

    try:
        # Imported lazily so the API starts even when ml4t is not installed.
        from core.backtest.backtest_engine import run_backtest

        # P0-b: every entry point that reads holdout returns must pass the single
        # ledger guard BEFORE computing anything. A Web-triggered backtest on an
        # already-revealed holdout window must be blocked here, exactly like the
        # CLI backtest command (see core/commands/backtest.py). A burned window
        # raises HoldoutReuseError, which is caught below and marks the run failed.
        holdout_guard.guard(
            start=request.get("start"),
            end=request.get("end"),
            purpose="final" if request.get("orientation_db_root") else "explore",
            mode=request.get("holdout_mode"),
            allow_reuse=bool(request.get("allow_holdout_reuse", False)),
            source="api:backtest_runs",
            note=f"factors={len(request.get('factor_ids') or [])} db_root={request.get('db_root')}",
        )

        out = run_backtest(
            request["factor_ids"],
            db_root=request.get("db_root") or "factor_db",
            orientation_db_root=request.get("orientation_db_root"),
            data_root=request.get("data_root") or "data",
            exchange=request.get("exchange") or "binance",
            timeframe=request.get("timeframe") or "1d",
            trade_type=request.get("trade_type") or "futures",
            start=request.get("start"),
            end=request.get("end"),
            initial_cash=float(request.get("initial_cash") or 1_000_000.0),
            commission_rate=float(request.get("commission_rate") or 0.0004),
            slippage_rate=float(request.get("slippage_rate") or 0.0005),
            funding_rate=float(request.get("funding_rate") or 0.0001),
            n_long=int(request.get("n_long") or 5),
            n_short=int(request.get("n_short") or 5),
            rebalance_every=int(request.get("rebalance_every") or 5),
            weights=request.get("weights") or None,
        )

        _write_json(os.path.join(directory, "summary.json"), out["summary"])

        equity = out.get("equity")
        if equity is not None and not equity.empty:
            equity.to_csv(os.path.join(directory, "equity.csv"), index=False)

        try:
            trades = out["result"].to_trades_dataframe().to_pandas()
            if not trades.empty:
                trades.to_csv(os.path.join(directory, "trades.csv"), index=False)
        except Exception:
            logger.debug("Trade ledger unavailable for %s", run_id, exc_info=True)

        status.update(
            {
                "status": "completed",
                "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "summary": out["summary"],
            }
        )
    except Exception as exc:
        logger.error("Backtest %s failed: %s", run_id, exc, exc_info=True)
        status.update(
            {
                "status": "failed",
                "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            }
        )
    finally:
        with _lock:
            _active.pop(run_id, None)
        try:
            _write_json(status_path, status)
        except Exception:  # pragma: no cover - disk full etc.
            logger.error("Could not persist status for %s", run_id, exc_info=True)


def list_runs(limit: int = 30) -> list[dict]:
    """Newest-first run summaries, read straight from disk."""
    if not os.path.isdir(BACKTEST_ROOT):
        return []
    records = []
    for name in os.listdir(BACKTEST_ROOT):
        if not name.startswith("bt-"):
            continue
        status = _read_json(os.path.join(BACKTEST_ROOT, name, "status.json"))
        if not status:
            continue
        summary = status.get("summary") or _read_json(
            os.path.join(BACKTEST_ROOT, name, "summary.json")
        )
        records.append(
            {
                "run_id": status.get("run_id", name),
                "status": status.get("status", "unknown"),
                "created_at": status.get("created_at"),
                "finished_at": status.get("finished_at"),
                "error": status.get("error"),
                "n_factors": len(status.get("request", {}).get("factor_ids") or []),
                "scope": (summary or {}).get("scope"),
                "total_return_pct": (summary or {}).get("total_return_pct"),
                "sharpe": (summary or {}).get("sharpe"),
                "max_drawdown_pct": (summary or {}).get("max_drawdown_pct"),
            }
        )
    records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return records[: max(1, min(limit, 200))]


def get_run(run_id: str) -> dict:
    directory = _run_dir(run_id)
    status = _read_json(os.path.join(directory, "status.json"))
    if not status:
        raise FileNotFoundError(run_id)
    if not status.get("summary"):
        status["summary"] = _read_json(os.path.join(directory, "summary.json"))

    equity_path = os.path.join(directory, "equity.csv")
    if os.path.exists(equity_path):
        status["equity"] = _read_equity(equity_path)
    return status


def _read_equity(path: str) -> dict:
    """Down-sample the equity curve so the browser payload stays small."""
    import pandas as pd

    frame = pd.read_csv(path)
    if "timestamp" not in frame.columns:
        return {"timestamps": [], "equity": [], "drawdown": [], "downsampled": False}
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")

    total = len(frame)
    step = max(1, total // MAX_EQUITY_POINTS)
    sampled = frame.iloc[::step]
    return {
        "timestamps": [ts.strftime("%Y-%m-%d") for ts in sampled["timestamp"]],
        "equity": [round(float(v), 2) for v in sampled.get("equity", [])],
        "cumulative_return": [
            round(float(v), 6) for v in sampled.get("cumulative_return", [])
        ]
        or None,
        "drawdown": [round(float(v), 6) for v in sampled.get("drawdown", [])],
        "points": int(len(sampled)),
        "total_points": int(total),
        "downsampled": bool(step > 1),
    }


def is_running(run_id: str) -> bool:
    with _lock:
        thread = _active.get(run_id)
    return bool(thread and thread.is_alive())
