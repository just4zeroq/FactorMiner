"""Task registry persistence and cancellation.

The Launchpad used to lose every task on restart. These tests pin the contract:
a run that was still ``running`` when the process died must come back as
``interrupted`` (never as a phantom live task), and log trimming must keep the
store small enough to load eagerly at startup.
"""

import json
import os
import tempfile
import unittest

from api.task_store import (
    MAX_PERSISTED_LOGS,
    MiningCancelled,
    TaskStore,
    check_cancelled,
    request_cancel,
)


def _task(task_id="T-1", status="running", **extra):
    payload = {
        "id": task_id,
        "status": status,
        "start_time": "2026-11-14T10:00:00",
        "progress": 40,
        "logs": [f"line {i}" for i in range(500)],
    }
    payload.update(extra)
    return payload


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tasks.json")
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flush_and_reload_roundtrip(self):
        tasks = {"T-1": _task(status="completed")}
        self.store.flush(tasks, force=True)

        restored: dict = {}
        TaskStore(self.path).load(restored)
        self.assertIn("T-1", restored)
        self.assertEqual(restored["T-1"]["status"], "completed")
        self.assertEqual(restored["T-1"]["progress"], 40)

    def test_running_task_is_downgraded_to_interrupted(self):
        self.store.flush({"T-2": _task("T-2", status="running")}, force=True)

        restored: dict = {}
        TaskStore(self.path).load(restored)
        self.assertEqual(restored["T-2"]["status"], "interrupted")
        self.assertIn("restarted", restored["T-2"]["error_msg"])

    def test_terminal_statuses_are_preserved(self):
        self.store.flush(
            {
                "ok": _task("ok", status="completed"),
                "empty": _task("empty", status="completed_empty"),
                "dead": _task("dead", status="failed"),
                "stopped": _task("stopped", status="cancelled"),
            },
            force=True,
        )
        restored: dict = {}
        TaskStore(self.path).load(restored)
        self.assertEqual(restored["ok"]["status"], "completed")
        self.assertEqual(restored["empty"]["status"], "completed_empty")
        self.assertEqual(restored["dead"]["status"], "failed")
        self.assertEqual(restored["stopped"]["status"], "cancelled")

    def test_logs_are_trimmed_on_write(self):
        self.store.flush({"T-3": _task("T-3", status="completed")}, force=True)
        with open(self.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        logs = payload["tasks"][0]["logs"]
        self.assertEqual(len(logs), MAX_PERSISTED_LOGS)
        self.assertEqual(logs[-1], "line 499")

    def test_missing_file_loads_silently(self):
        restored: dict = {}
        TaskStore(os.path.join(self.tmp.name, "absent.json")).load(restored)
        self.assertEqual(restored, {})

    def test_corrupt_file_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        restored: dict = {}
        TaskStore(self.path).load(restored)
        self.assertEqual(restored, {})

    def test_flush_is_throttled_unless_forced(self):
        tasks = {"T-4": _task("T-4", status="running", progress=10)}
        self.store.flush(tasks, force=True)
        tasks["T-4"]["progress"] = 90
        self.store.flush(tasks)  # inside the throttle window: must be skipped

        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["tasks"][0]["progress"], 10)

        self.store.flush(tasks, force=True)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["tasks"][0]["progress"], 90)

    def test_store_keeps_newest_tasks_only(self):
        tasks = {
            f"T-{i}": _task(f"T-{i}", status="completed", start_time=f"2026-01-{i + 1:02d}T00:00:00")
            for i in range(70)
        }
        self.store.flush(tasks, force=True)
        with open(self.path, encoding="utf-8") as handle:
            records = json.load(handle)["tasks"]
        self.assertEqual(len(records), 60)
        self.assertEqual(records[0]["id"], "T-69")


class CancellationTests(unittest.TestCase):
    def test_request_cancel_marks_running_task(self):
        task = _task(status="running")
        self.assertTrue(request_cancel(task))
        self.assertTrue(task["cancel_requested"])

    def test_request_cancel_ignores_finished_task(self):
        task = _task(status="completed")
        self.assertFalse(request_cancel(task))
        self.assertNotIn("cancel_requested", task)

    def test_check_cancelled_raises(self):
        task = _task(status="running", cancel_requested=True)
        with self.assertRaises(MiningCancelled):
            check_cancelled(task)

    def test_check_cancelled_is_a_noop_when_not_requested(self):
        check_cancelled(_task(status="running"))


if __name__ == "__main__":
    unittest.main()
