"""Durable task registry for the mining launcher.

``TaskManager.tasks`` used to be a plain dict: every restart wiped the task list
and any in-flight run became invisible. This module keeps the same dict-shaped
API but mirrors it to ``.workbuddy/tasks.json`` so the Launchpad keeps its
history (and can report runs that were interrupted by a restart).

Writes are throttled: progress ticks and log lines fire many times per second,
so only state transitions force a flush. Logs are trimmed on the way out — the
file must stay small enough to load at startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

STORE_DIR = ".workbuddy"
STORE_PATH = os.path.join(STORE_DIR, "tasks.json")
MAX_PERSISTED_LOGS = 60
MAX_PERSISTED_TASKS = 60
MIN_FLUSH_INTERVAL = 5.0

# A-03: ``interrupted`` is terminal too. Without it, ``request_cancel`` happily
# reported ``cancelled=True`` for a ghost task whose worker died with the process.
TERMINAL_STATUSES = {"completed", "completed_empty", "failed", "cancelled", "interrupted"}


class TaskStore:
    def __init__(self, path: str = STORE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_flush = 0.0

    def load(self, tasks: dict) -> None:
        """Merge the persisted tasks into ``tasks``, downgrading stale ones."""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except Exception:
            logger.warning("Unreadable task store at %s; starting empty", self.path, exc_info=True)
            return
        records = payload.get("tasks") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            return
        for record in records:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            # A-08: hand-edited or truncated stores used to take down
            # /api/tasks, /api/stats and /api/dashboard with KeyError.
            record.setdefault("start_time", "")
            record.setdefault("status", "interrupted")
            record.setdefault("miner", "")
            record.setdefault("config", "")
            record.setdefault("logs", [])
            record.setdefault("factors", [])
            record.setdefault("metrics", {})
            record.setdefault("progress", 0)
            record.setdefault("current_task", 0)
            record.setdefault("total_tasks", 0)
            record.setdefault("cancel_requested", False)
            if not isinstance(record["logs"], list):
                record["logs"] = []
            if not isinstance(record["factors"], list):
                record["factors"] = []
            if not isinstance(record["metrics"], dict):
                record["metrics"] = {}
            if record["status"] not in TERMINAL_STATUSES:
                # The process that owned this run is gone; say so instead of
                # showing a phantom "running" task forever.
                record["status"] = "interrupted"
                record["error_msg"] = record.get("error_msg") or (
                    "The engine restarted before this task finished. Re-launch to continue."
                )
            tasks[record["id"]] = record

    def flush(self, tasks: dict, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < MIN_FLUSH_INTERVAL:
            return
        with self._lock:
            if not force and now - self._last_flush < MIN_FLUSH_INTERVAL:
                return
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                records = sorted(
                    tasks.values(),
                    key=lambda task: task.get("start_time") or "",
                    reverse=True,
                )[:MAX_PERSISTED_TASKS]
                payload = {"version": 1, "tasks": [_trim(task) for task in records]}
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=1, ensure_ascii=False, default=str)
                os.replace(tmp, self.path)
                self._last_flush = now
            except Exception:
                logger.warning("Could not persist task store", exc_info=True)


def _trim(task: dict) -> dict:
    trimmed = dict(task)
    logs = trimmed.get("logs") or []
    if isinstance(logs, list) and len(logs) > MAX_PERSISTED_LOGS:
        trimmed["logs"] = logs[-MAX_PERSISTED_LOGS:]
    # A-08: factors/metrics grew without bound in tasks.json.
    factors = trimmed.get("factors")
    if isinstance(factors, list) and len(factors) > MAX_PERSISTED_LOGS:
        trimmed["factors"] = factors[-MAX_PERSISTED_LOGS:]
    metrics = trimmed.get("metrics")
    if isinstance(metrics, dict) and len(metrics) > 200:
        trimmed["metrics"] = dict(list(metrics.items())[:200])
    # The traceback of a failed run can be huge and is already in logs.
    if trimmed.get("error_msg") and len(str(trimmed["error_msg"])) > 4000:
        trimmed["error_msg"] = str(trimmed["error_msg"])[:4000] + "\n… (truncated)"
    return trimmed


class MiningCancelled(RuntimeError):
    """Raised from the progress callback when the user stops a run."""


def request_cancel(task: Optional[dict]) -> bool:
    if not task:
        return False
    if task.get("status") in TERMINAL_STATUSES:
        return False
    task["cancel_requested"] = True
    return True


def check_cancelled(task: dict) -> None:
    """Raise when the task has been asked to stop."""
    if task.get("cancel_requested"):
        raise MiningCancelled("Task cancelled by user")
