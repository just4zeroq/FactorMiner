"""P0-b: single-ledger holdout guard + entry-point coverage.

The failure mode being pinned here is *not* the ledger arithmetic (that is
already covered by ``test_experiment_trace.py``), but the fact that the old
enforcement lived in one optional branch of the director. Every entry point
that reads realised returns on a holdout window must go through
``holdout_guard``; the last test class asserts the wiring itself so a future
CLI cannot quietly bypass it again.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.evaluation import holdout_guard
from core.evaluation.experiment_trace import HoldoutLedger, HoldoutReuseError

BURNED = ("holdout_2025H2_2026", "2025-07-01", "2026-11-13")


class _Stop(Exception):
    """Sentinel so an entry-point test stops right after the guard call."""


class HoldoutGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Keep ALL audit logs out of the real user_workspace.
        patcher = mock.patch(
            "core.evaluation.holdout_guard.default_workspace_path",
            side_effect=lambda name: os.path.join(self.tmp.name, name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger_path = os.path.join(self.tmp.name, "ledger.json")
        HoldoutLedger(self.ledger_path).reveal(*BURNED, note="bootstrap")

    def _guard(self, start=None, end=None, **kw):
        return holdout_guard.guard(
            start, end, ledger_path=self.ledger_path, source="test", **kw
        )

    def _log(self, name):
        path = Path(self.tmp.name) / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_burned_window_raises_in_default_strict_mode(self):
        self.assertEqual(holdout_guard.resolve_mode(), "strict")
        with self.assertRaises(HoldoutReuseError) as ctx:
            self._guard("2025-07-01", "2026-11-13")
        self.assertIn(BURNED[0], str(ctx.exception))
        self.assertIn("--allow-holdout-reuse", str(ctx.exception))

    def test_overlapping_window_with_a_different_key_also_raises(self):
        with self.assertRaises(HoldoutReuseError):
            self._guard("2026-06-01", "2026-12-31", note="different name")
        with self.assertRaises(HoldoutReuseError):
            self._guard("2024-01-01", "2025-09-01")

    def test_in_sample_window_passes_and_registers_nothing(self):
        out = self._guard("2023-01-01", "2024-12-31")
        self.assertTrue(out["allowed"])
        self.assertFalse(out["revealed"])
        self.assertEqual(out["reason"], "clean")
        self.assertEqual(len(HoldoutLedger(self.ledger_path).snapshot()), 1)

    def test_final_on_fresh_window_registers_one_reveal(self):
        out = self._guard("2026-12-01", "2027-03-01", purpose="final")
        self.assertTrue(out["revealed"])
        snapshot = HoldoutLedger(self.ledger_path).snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(snapshot[1]["start"], "2026-12-01")
        self.assertEqual(self._log("reports/holdout_access.jsonl")[-1]["event"], "revealed")

    def test_cross_process_instance_still_blocks(self):
        # A fresh ledger object (== a new process) must see the same burn.
        fresh = HoldoutLedger(self.ledger_path)
        self.assertTrue(fresh.is_revealed(BURNED[0]))
        with self.assertRaises(HoldoutReuseError):
            self._guard("2026-01-01", "2026-02-01")

    def test_allow_reuse_writes_an_immutable_override_line(self):
        out = self._guard("2025-07-01", "2026-11-13", allow_reuse=True)
        self.assertTrue(out["override"])
        self.assertTrue(out["allowed"])
        overrides = self._log("reports/holdout_overrides.jsonl")
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["source"], "test")
        self.assertEqual(overrides[0]["burned"][0]["period_key"], BURNED[0])

    def test_audit_mode_allows_but_logs_the_violation(self):
        out = self._guard("2025-08-01", "2025-09-01", mode="audit")
        self.assertTrue(out["allowed"])
        self.assertEqual(out["reason"], "audit_mode_allowed")
        self.assertEqual(self._log("reports/holdout_access.jsonl")[-1]["event"], "audit_allowed")

    def test_mode_from_config_and_environment(self):
        self.assertEqual(
            holdout_guard.resolve_mode({"evaluation": {"gates": {"holdout_mode": "audit"}}}),
            "audit",
        )
        with mock.patch.dict(os.environ, {"FACTORMINER_HOLDOUT_MODE": "off"}):
            self.assertEqual(holdout_guard.resolve_mode(), "off")
        self.assertEqual(holdout_guard.resolve_mode(explicit="strict"), "strict")

    def test_off_mode_skips_the_check(self):
        out = self._guard("2025-07-01", "2026-11-13", mode="off")
        self.assertTrue(out["allowed"])
        self.assertEqual(out["reason"], "mode=off")

    def test_final_requires_an_explicit_window(self):
        with self.assertRaises(ValueError):
            self._guard(None, None, purpose="final")

    def test_unbounded_explore_is_logged_not_blocked(self):
        out = self._guard(None, None)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["reason"], "unbounded")
        self.assertEqual(self._log("reports/holdout_access.jsonl")[-1]["event"], "unbounded")

    def test_one_sided_window_cannot_slip_past_overlap(self):
        with self.assertRaises(HoldoutReuseError):
            self._guard(None, "2026-01-01")
        with self.assertRaises(HoldoutReuseError):
            self._guard("2026-01-01", None)

    def test_guard_periods_checks_every_fold(self):
        with self.assertRaises(HoldoutReuseError):
            holdout_guard.guard_periods(
                [["2023-01-01", "2023-12-31"], ["2026-01-01", "2026-03-01"]],
                ledger_path=self.ledger_path, source="test",
            )

    def test_guard_config_reads_test_period_and_strict_holdout(self):
        cfg = {"data_feeds": {"test_period": [["2023-01-01", "2024-12-31"]]}}
        out = holdout_guard.guard_config(
            cfg, ledger_path=self.ledger_path, source="script:test"
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["allowed"])
        strict_cfg = {
            "evaluation": {"gates": {"strict_holdout": {
                "start": "2026-12-01", "end": "2027-03-01",
            }}}
        }
        final = holdout_guard.guard_config(
            strict_cfg, ledger_path=self.ledger_path, source="script:test"
        )
        self.assertTrue(final[0]["revealed"])

    def test_window_spec_is_readable(self):
        # The patch redirects to a temp dir, so the spec is empty there; this
        # pins that a missing spec never crashes the guard.
        self.assertEqual(holdout_guard.load_window_spec(), {})


class EntryPointCoverageTests(unittest.TestCase):
    """Assert the wiring, not the logic: a new CLI must not skip the guard."""

    def test_backtest_command_calls_the_guard_before_running(self):
        from core.commands import backtest as bt_cmd

        args = argparse.Namespace(
            factors="", weights=None, config=None,
            start="2025-07-01", end="2026-11-13",
            orientation_db_root="factor_db", holdout_mode=None,
            allow_holdout_reuse=False, db_root="factor_db",
        )
        with mock.patch.object(bt_cmd, "_discover_factor_ids", return_value=["f1"]), \
             mock.patch("core.evaluation.holdout_guard.guard", side_effect=_Stop) as spy:
            with self.assertRaises(_Stop):
                bt_cmd.run_backtest_cmd(args)
        self.assertTrue(spy.called)
        self.assertEqual(spy.call_args.kwargs["purpose"], "final")
        self.assertEqual(spy.call_args.kwargs["source"], "cli:backtest")

    def test_inspect_command_calls_the_guard_before_running(self):
        from core.commands import inspect as inspect_cmd

        args = argparse.Namespace(
            factor=None, ast="{}", code=None, config=None, pairs=None,
            start="2025-07-01", end="2026-11-13", timeframe="1d",
            user_dir=os.path.join(tempfile.gettempdir(), "definitely_missing_dir"),
            holdout_mode=None, allow_holdout_reuse=False,
        )
        with mock.patch("core.evaluation.holdout_guard.guard_periods", side_effect=_Stop) as spy:
            with self.assertRaises(_Stop):
                inspect_cmd.run_inspector(args)
        self.assertTrue(spy.called)
        self.assertEqual(spy.call_args.args[0], [["2025-07-01", "2026-11-13"]])

    def test_director_enforces_even_without_strict_holdout_config(self):
        from core.miner.director import FactorMinerDirector

        director = FactorMinerDirector.__new__(FactorMinerDirector)
        director.config = {
            "data_feeds": {"test_period": [["2025-08-01", "2025-09-01"]]},
            "evaluation": {"gates": {}},
        }
        director.holdout = mock.Mock(path="/tmp/isolated_ledger.json")
        with mock.patch("core.evaluation.holdout_guard.guard_periods", side_effect=_Stop) as spy:
            with self.assertRaises(_Stop):
                director._enforce_holdout()
        self.assertTrue(spy.called)
        self.assertEqual(spy.call_args.kwargs["purpose"], "explore")

    def test_director_run_checks_before_mining(self):
        """Fail-fast ordering: guard must run before ``miner.mine``."""
        from core.miner.director import FactorMinerDirector

        director = FactorMinerDirector.__new__(FactorMinerDirector)
        director.config = {"paradigm": "GP"}
        director.miner = mock.Mock()
        with mock.patch.object(FactorMinerDirector, "_enforce_holdout", side_effect=_Stop), \
             mock.patch.object(director, "_save_excellent_factors"), \
             mock.patch.object(director, "_print_results_table"):
            with self.assertRaises(_Stop):
                director.run(max_iterations=1)
        director.miner.mine.assert_not_called()

    def test_source_text_of_every_entry_point_references_the_guard(self):
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "core/commands/backtest.py",
            "core/commands/inspect.py",
            "core/miner/director.py",
        ):
            text = (root / rel).read_text(encoding="utf-8")
            self.assertIn("holdout_guard", text, msg=f"{rel} bypasses the guard")


class CanonicalLedgerTests(unittest.TestCase):
    # The canonical (committed) ledger must carry the one-time strict holdout
    # that was intentionally established for this repo. It was originally the
    # P0-b 16-month burn (holdout_2025H2_2026); on 2026-11-15 it was reset to a
    # 60-bar lock (holdout_60bars_2026) so the rolling-forward anchor sits at the
    # latest real bar instead of 16 months in the past. This pin tracks whatever
    # the *current* canonical reveal is.
    CANONICAL_KEY = "holdout_60bars_2026"

    def setUp(self):
        # This class inspects the *real* canonical ledger (the bootstrap
        # reveal), so it must opt out of the conftest isolation fixture that
        # redirects the ledger to a temp dir. We only read, never write, so the
        # real working tree is not polluted.
        os.environ.pop("FACTORMINER_HOLDOUT_LEDGER_PATH", None)

    def test_default_ledger_is_absolute_and_contains_the_canonical_reveal(self):
        path = holdout_guard.default_ledger_path()
        self.assertTrue(os.path.isabs(path))
        self.assertTrue(path.endswith(".holdout_ledger.json"))
        self.assertTrue(os.path.exists(path), "canonical holdout ledger is missing")
        ledger = HoldoutLedger(path)
        keys = [r["period_key"] for r in ledger.snapshot()]
        self.assertIn(self.CANONICAL_KEY, keys)

    def test_holdout_ledger_default_matches_the_guard(self):
        self.assertEqual(HoldoutLedger().path, holdout_guard.default_ledger_path())


class LedgerEnvOverrideTests(unittest.TestCase):
    """Lock the env-var redirect that lets the suite isolate the ledger (T3).

    ``FACTORMINER_HOLDOUT_LEDGER_PATH`` must override *only* the holdout ledger
    and leave other workspace files (e.g. ``experiment_window.json``) untouched.
    """

    def test_env_override_redirects_only_the_holdout_ledger(self):
        override = os.path.join(tempfile.mkdtemp(), "isolated_ledger.json")
        old = os.environ.get("FACTORMINER_HOLDOUT_LEDGER_PATH")
        os.environ["FACTORMINER_HOLDOUT_LEDGER_PATH"] = override
        try:
            self.assertEqual(holdout_guard.default_ledger_path(), override)
            self.assertEqual(
                holdout_guard.default_workspace_path("experiment_window.json"),
                os.path.join(
                    str(Path(__file__).resolve().parents[1]),  # repo root
                    "user_workspace", "experiment_window.json"
                ),
            )
            # HoldoutLedger() with no path must follow the override.
            self.assertEqual(HoldoutLedger().path, override)
        finally:
            if old is None:
                os.environ.pop("FACTORMINER_HOLDOUT_LEDGER_PATH", None)
            else:
                os.environ["FACTORMINER_HOLDOUT_LEDGER_PATH"] = old


if __name__ == "__main__":
    unittest.main()


class ProductionGovernanceTests(unittest.TestCase):
    """P1-c: a deployment must not be able to loosen its own holdout guard.

    ``audit`` / ``off`` and ``allow_reuse`` exist so research can move; in a
    production pipeline they would let the single-use holdout be re-read, which
    invalidates every published number downstream. ``FACTORMINER_PRODUCTION``
    turns all three into hard errors, enforced inside ``guard`` so the CLI, the
    experiment pipeline and the Web backtest inherit it from one place.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch(
            "core.evaluation.holdout_guard.default_workspace_path",
            side_effect=lambda name: os.path.join(self.tmp.name, name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger_path = os.path.join(self.tmp.name, "ledger.json")
        HoldoutLedger(self.ledger_path).reveal(*BURNED, note="bootstrap")

    def _guard(self, **kw):
        return holdout_guard.guard(
            "2025-07-01", "2026-11-13", ledger_path=self.ledger_path,
            source="test", **kw
        )

    def test_production_refuses_audit_mode(self):
        with mock.patch.dict(os.environ, {holdout_guard.PRODUCTION_ENV_VAR: "1"}):
            with self.assertRaises(HoldoutReuseError) as ctx:
                self._guard(mode="audit")
        self.assertIn("strict", str(ctx.exception))

    def test_production_refuses_off_mode(self):
        with mock.patch.dict(os.environ, {holdout_guard.PRODUCTION_ENV_VAR: "true"}):
            with self.assertRaises(HoldoutReuseError):
                self._guard(mode="off")

    def test_production_refuses_reuse_override(self):
        with mock.patch.dict(os.environ, {holdout_guard.PRODUCTION_ENV_VAR: "yes"}):
            with self.assertRaises(HoldoutReuseError) as ctx:
                self._guard(allow_reuse=True)
        self.assertIn("allow_holdout_reuse", str(ctx.exception))

    def test_production_still_allows_a_clean_strict_run(self):
        with mock.patch.dict(os.environ, {holdout_guard.PRODUCTION_ENV_VAR: "1"}):
            out = holdout_guard.guard(
                "2023-01-01", "2023-12-31",
                ledger_path=self.ledger_path, source="test",
            )
        self.assertTrue(out["allowed"])
        self.assertEqual(out["mode"], "strict")

    def test_without_the_flag_nothing_changes(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(holdout_guard.PRODUCTION_ENV_VAR, None)
            out = self._guard(mode="audit")
        self.assertTrue(out["allowed"])
        self.assertEqual(out["mode"], "audit")
