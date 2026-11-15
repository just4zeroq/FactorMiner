"""Lock F3: the Web backtest endpoint must enforce the holdout guard.

Before this test, ``api/backtest_runs._execute`` called ``run_backtest`` directly
with the request's ``start``/``end`` and never went through ``holdout_guard`` —
so a backtest triggered from the Web UI could read a burned holdout window
without the single-ledger check that the CLI ``backtest`` command already
enforces. The guard must run first; a burned window blocks the run and must not
invoke the (heavy) backtest at all.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from core.evaluation.experiment_trace import HoldoutLedger

import api.backtest_runs as br

BURNED = ("holdout_test_api", "2025-07-01", "2026-11-13")


class _FakeResult:
    """Minimal stand-in for the ml4t result object inside ``_execute``."""


def _fake_backtest(*_a, **_k):
    return {"summary": {"scope": "test"}, "equity": None, "result": _FakeResult()}


class TestApiBacktestGuard(unittest.TestCase):
    def setUp(self):
        # The conftest autouse fixture isolates the ledger to a temp dir; reuse it.
        self.ledger_path = os.environ.get("FACTORMINER_HOLDOUT_LEDGER_PATH")
        self.assertIsNotNone(self.ledger_path, "conftest did not isolate the ledger")
        HoldoutLedger(self.ledger_path).reveal(*BURNED, note="bootstrap")

    def _execute_with_spy(self, request):
        run_id = "bt-guard-test"
        # ``start_run`` normally creates this dir; replicate it for the direct call.
        os.makedirs(br._run_dir(run_id), exist_ok=True)
        br._write_json(
            br._run_dir(run_id) + "/status.json",
            {"run_id": run_id, "status": "queued"},
        )
        calls = []
        real = _fake_backtest

        def _spy(*a, **k):
            calls.append((a, k))
            return real(*a, **k)

        with mock.patch(
            "core.backtest.backtest_engine.run_backtest", side_effect=_spy
        ):
            br._execute(run_id, request)
        return br._read_json(br._run_dir(run_id) + "/status.json"), calls

    def test_burned_window_is_blocked_and_skips_backtest(self):
        status, calls = self._execute_with_spy({
            "factor_ids": ["fac_x"],
            "start": "2025-08-01",
            "end": "2026-01-01",
        })
        self.assertEqual(status["status"], "failed")
        self.assertIn("HoldoutReuseError", status["error"])
        self.assertEqual(
            calls, [], "run_backtest must NOT execute on a burned holdout window"
        )

    def test_clean_window_runs_backtest(self):
        status, calls = self._execute_with_spy({
            "factor_ids": ["fac_x"],
            "start": "2023-01-01",
            "end": "2024-01-01",
        })
        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(calls), 1, "clean window should reach run_backtest")

    def test_oos_params_forwarded_to_engine(self):
        """Web OOS controls must reach run_backtest (W-08 end-to-end wiring).

        The Web form sends orientation_db_root / holdout_mode /
        allow_holdout_reuse. If any of these is dropped en route, the backtest
        silently falls back to 'explore' and the UI's 'strict OOS' promise is
        a lie.
        """
        status, calls = self._execute_with_spy({
            "factor_ids": ["fac_x"],
            "start": "2023-01-01",
            "end": "2024-01-01",
            "db_root": "factor_db_oos",
            "orientation_db_root": "factor_db",
            "holdout_mode": "strict",
            "allow_holdout_reuse": True,
        })
        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(calls), 1)
        _args, kwargs = calls[0]
        self.assertEqual(kwargs.get("db_root"), "factor_db_oos")
        self.assertEqual(kwargs.get("orientation_db_root"), "factor_db")

    def test_no_orientation_root_means_explore(self):
        """Without orientation_db_root the call is an explore run (no strict OOS)."""
        status, calls = self._execute_with_spy({
            "factor_ids": ["fac_x"],
            "start": "2023-01-01",
            "end": "2024-01-01",
        })
        self.assertEqual(len(calls), 1)
        _args, kwargs = calls[0]
        self.assertIsNone(kwargs.get("orientation_db_root"))


if __name__ == "__main__":
    unittest.main()
