"""Layer 3 regression: reproducibility, one-time reveals and operational truth.

Covers §13 item 11 plus the second-wave confirmed bugs (E-02/E-04/E-06,
K-04/K-06/K-07/K-08, X-01/X-02/X-03/X-06/X-07, A-03/A-04/A-05/A-06/A-08,
S-01/S-02, T-01/T-04, D-08, G-05). Every test uses a temp directory so the
canonical ledgers under ``user_workspace`` are never touched.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from core.evaluation import holdout_guard
from core.evaluation.experiment_trace import (
    ExperimentTrace,
    HoldoutLedger,
    HoldoutReuseError,
    TrialRecord,
)
from core.experiment import manifest as mf
from core.experiment import registry as reg


class GuardBoundTests(unittest.TestCase):
    """E-02: a final reveal must be fully bounded and unambiguous."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "ledger.json")
        patcher = mock.patch(
            "core.evaluation.holdout_guard.default_workspace_path",
            side_effect=lambda name: os.path.join(self.tmp.name, name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _guard(self, start=None, end=None, **kw):
        return holdout_guard.guard(
            start, end, ledger_path=self.ledger, source="test", **kw
        )

    def test_one_sided_final_is_rejected(self):
        for start, end in ((None, "2026-01-01"), ("2026-01-01", None), (None, None)):
            with self.assertRaisesRegex(ValueError, "BOTH --start and --end"):
                self._guard(start, end, purpose="final")
        self.assertFalse(HoldoutLedger(self.ledger).snapshot())

    def test_one_sided_explore_still_checks_overlap(self):
        HoldoutLedger(self.ledger).reveal("burn", "2025-01-01", "2025-12-31")
        with self.assertRaises(HoldoutReuseError):
            self._guard(None, "2025-06-01")
        with self.assertRaises(HoldoutReuseError):
            self._guard("2025-06-01", None)

    def test_garbage_bound_is_rejected_not_truncated(self):
        with self.assertRaisesRegex(ValueError, "Invalid holdout window bound"):
            self._guard("garbage_start", "2026-01-01")

    def test_reversed_window_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "is after end"):
            self._guard("2026-06-01", "2026-01-01")

    def test_intraday_bounds_keep_time_and_timezone_is_utc(self):
        out = self._guard("2026-01-01T08:00:00+08:00", "2026-01-02T09:30:00+08:00")
        self.assertEqual(out["start"], "2026-01-01T00:00:00")
        self.assertEqual(out["end"], "2026-01-02T01:30:00")

    def test_intraday_reveal_overlaps_intraday_query(self):
        self._guard("2026-01-01T10:00:00", "2026-01-01T14:00:00", purpose="final")
        with self.assertRaises(HoldoutReuseError):
            self._guard("2026-01-01T13:00:00", "2026-01-01T15:00:00")

    def test_period_key_includes_research_context(self):
        cfg = {"data_feeds": {"timeframe": "1h"}, "target": {"horizon_bars": 5}}
        out = self._guard("2026-01-01", "2026-02-01", purpose="final", config=cfg)
        self.assertTrue(out["revealed"])
        snapshot = HoldoutLedger(self.ledger).snapshot()
        self.assertNotEqual(snapshot[0]["period_key"], "holdout_2026-01-01_2026-02-01")
        self.assertIn("holdout_2026-01-01_2026-02-01_", snapshot[0]["period_key"])


class InspectCommandTests(unittest.TestCase):
    """K-06: the inspector must not be a one-sided-window back door."""

    def _args(self, **kw):
        base = dict(
            factor=None, ast="{}", code=None, config=None, pairs=None,
            start=None, end=None, timeframe="1d",
            user_dir=os.path.join(tempfile.gettempdir(), "definitely_missing_dir"),
            holdout_mode=None, allow_holdout_reuse=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_one_sided_window_is_rejected_before_guard(self):
        from core.commands import inspect as cmd

        with mock.patch("core.evaluation.holdout_guard.guard_periods") as spy:
            code = cmd.run_inspector(self._args(start="2026-01-01"))
        self.assertEqual(code, 2)
        spy.assert_not_called()

    def test_missing_config_is_loud(self):
        from core.commands import inspect as cmd

        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.json")
            self.assertEqual(cmd.run_inspector(self._args(config=missing)), 1)

    def test_holdout_error_is_converted_to_exit_code(self):
        from core.commands import inspect as cmd

        with mock.patch(
            "core.evaluation.holdout_guard.guard_periods",
            side_effect=HoldoutReuseError("burned"),
        ):
            self.assertEqual(cmd.run_inspector(self._args(start="2026-01-01", end="2026-02-01")), 1)


class ManifestResumeTests(unittest.TestCase):
    """X-02 / G-05: fail-closed resume and honest seed handling."""

    def test_verify_manifest_fails_closed_on_missing_hashes(self):
        m = mf.build_manifest({"data_feeds": {"pairs": ["A"]}, "paradigm": "GP"})
        for key in ("config_hash", "git_sha", "operator_set_hash"):
            m.pop(key)
        m["data_snapshot"] = {}
        out = mf.verify_manifest(m, {"data_feeds": {"pairs": ["A"]}, "paradigm": "GP"})
        self.assertFalse(out["reproducible"])
        self.assertEqual(set(out["missing"]), {"config_hash", "git_sha", "operator_set_hash", "data_snapshot_hash"})

    def test_verify_manifest_passes_when_identical(self):
        cfg = {"data_feeds": {"pairs": ["A"]}, "paradigm": "GP"}
        m = mf.build_manifest(cfg)
        self.assertTrue(mf.verify_manifest(m, cfg)["reproducible"])

    def test_seed_applied_reflects_actual_seeding(self):
        cfg = {"data_feeds": {"pairs": ["A"]}, "paradigm": "GP", "experiment": {"seed": 7}}
        self.assertTrue(mf.build_manifest(cfg)["seed_applied"])
        self.assertFalse(mf.build_manifest({"data_feeds": {"pairs": ["A"]}})["seed_applied"])

    def test_derive_rngs_is_deterministic_and_paradigm_scoped(self):
        cfg = {"paradigm": "GP", "experiment": {"seed": 42}}
        a = mf.derive_rngs(cfg, salt="gp")
        b = mf.derive_rngs(cfg, salt="gp")
        self.assertEqual([a[0].random() for _ in range(3)], [b[0].random() for _ in range(3)])
        self.assertEqual(list(a[1].integers(0, 1000, 5)), list(b[1].integers(0, 1000, 5)))
        other = mf.derive_rngs({**cfg, "paradigm": "RL"}, salt="gp")
        self.assertNotEqual(a[0].random(), other[0].random())

    def test_unseeded_rngs_fall_back_to_global_modules(self):
        import random

        r, npr = mf.derive_rngs({})
        self.assertIs(r, random)
        self.assertIs(npr, np.random)

    def test_data_snapshot_is_cwd_independent(self):
        cfg = {"data_feeds": {"exchange": "binance", "instrument_type": "futures",
                             "timeframe": "1d", "pairs": ["A"]}}
        before = mf.data_snapshot(cfg)["hash"]
        old = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            after = mf.data_snapshot(cfg)["hash"]
        finally:
            os.chdir(old)
        self.assertEqual(before, after)

    def test_sample_operator_params_honours_private_rng(self):
        from core.miner.operator_runtime import sample_operator_params

        r1 = random.Random(11)
        r2 = random.Random(11)
        self.assertEqual(
            sample_operator_params("ts_mean", rng=r1),
            sample_operator_params("ts_mean", rng=r2),
        )


class LedgerConcurrencyTests(unittest.TestCase):
    """E-04: two independent objects/processes must see each other's writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_two_ledger_objects_cannot_reveal_overlapping_windows(self):
        path = os.path.join(self.tmp.name, "ledger.json")
        a, b = HoldoutLedger(path), HoldoutLedger(path)
        a.reveal("A", "2025-01-01", "2025-06-30")
        with self.assertRaises(HoldoutReuseError):
            b.reveal("B", "2025-03-01", "2025-09-30")

    def test_two_trace_objects_do_not_overwrite_each_other(self):
        path = os.path.join(self.tmp.name, "trace.jsonl")
        a, b = ExperimentTrace(path), ExperimentTrace(path)
        a.record(TrialRecord(signature="sig-a", fitness=1.0))
        b.record(TrialRecord(signature="sig-b", fitness=2.0))
        self.assertEqual(ExperimentTrace(path).num_trials, 2)

    def test_repeat_attempts_are_journalled(self):
        path = os.path.join(self.tmp.name, "trace.jsonl")
        trace = ExperimentTrace(path)
        trace.record(TrialRecord(signature="dup", fitness=1.0))
        trace.record(TrialRecord(signature="dup", fitness=2.0))
        self.assertEqual(trace.num_trials, 1)
        self.assertEqual(trace.num_attempts, 2)
        self.assertEqual(ExperimentTrace(path).num_attempts, 2)
        self.assertAlmostEqual(ExperimentTrace(path).all()[0].fitness, 2.0)

    def test_unknown_keys_do_not_discard_whole_record(self):
        path = os.path.join(self.tmp.name, "trace.jsonl")
        Path(path).write_text(
            json.dumps({"signature": "s1", "fitness": 1.0, "future_field": 1}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(ExperimentTrace(path).num_trials, 1)

    def test_corrupt_holdout_ledger_fails_closed(self):
        path = os.path.join(self.tmp.name, "bad.json")
        Path(path).write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "corrupt"):
            HoldoutLedger(path)


class RegistryLifecycleTests(unittest.TestCase):
    """X-06: run ids, terminal states and stage monotonicity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = mf.build_manifest({"paradigm": "GP"})
        self.rid = self.m["run_id"]

    def test_run_id_collision_is_refused(self):
        reg.create_run(self.m, {}, root=self.root)
        with self.assertRaises(FileExistsError):
            reg.create_run(self.m, {}, root=self.root)

    def test_failed_run_is_resumable(self):
        reg.create_run(self.m, {}, root=self.root)
        reg.update_status(self.rid, "mining", status="failed", error="boom", root=self.root)
        self.assertEqual(reg.find_resumable(self.m["config_hash"], root=self.root), self.rid)

    def test_artifact_does_not_reopen_completed_run(self):
        reg.create_run(self.m, {}, root=self.root)
        reg.update_status(self.rid, "completed", root=self.root)
        reg.write_artifact(self.rid, "metrics.json", {"x": 1}, root=self.root)
        self.assertEqual(reg.read_status(self.rid, root=self.root)["status"], "completed")

    def test_stage_regression_is_ignored(self):
        reg.create_run(self.m, {}, root=self.root)
        reg.update_status(self.rid, "validated", root=self.root)
        reg.update_status(self.rid, "mining", root=self.root)
        self.assertEqual(reg.read_status(self.rid, root=self.root)["stage"], "validated")

    def test_save_manifest_persists_atomically(self):
        reg.create_run(self.m, {}, root=self.root)
        self.m["seed_applied"] = True
        reg.save_manifest(self.rid, self.m, root=self.root)
        self.assertTrue(
            json.loads((self.root / self.rid / "manifest.json").read_text())["seed_applied"]
        )


class OperationalTruthTests(unittest.TestCase):
    """A-03/A-08, S-02/T-01, K-04/K-07, X-07, E-06, D-08."""

    def test_interrupted_is_terminal_for_cancel(self):
        from api.task_store import TERMINAL_STATUSES, request_cancel

        self.assertIn("interrupted", TERMINAL_STATUSES)
        self.assertFalse(request_cancel({"status": "interrupted"}))

    def test_task_store_load_normalises_incomplete_records(self):
        from api.task_store import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tasks.json")
            Path(path).write_text(json.dumps({"tasks": [{"id": "t1"}]}), encoding="utf-8")
            tasks = {}
            TaskStore(path).load(tasks)
            self.assertEqual(tasks["t1"]["status"], "interrupted")
            self.assertEqual(tasks["t1"]["start_time"], "")
            self.assertIsInstance(tasks["t1"]["logs"], list)

    def test_backtest_run_id_traversal_is_rejected(self):
        from api import backtest_runs

        with self.assertRaises(FileNotFoundError):
            backtest_runs._run_dir("../../etc/passwd")

    def test_orphan_backtest_is_marked_interrupted(self):
        from api import backtest_runs

        with tempfile.TemporaryDirectory() as tmp:
            rid = "bt-20200101-000000-123"
            directory = Path(tmp) / rid
            directory.mkdir()
            (directory / "status.json").write_text(json.dumps({
                "run_id": rid, "status": "running",
                "created_at": "2020-01-01T00:00:00",
            }), encoding="utf-8")
            with mock.patch.object(backtest_runs, "BACKTEST_ROOT", tmp), \
                 mock.patch.object(backtest_runs, "_active", {}):
                self.assertEqual(backtest_runs.recover_orphans(), 1)
                self.assertEqual(
                    json.loads((directory / "status.json").read_text())["status"],
                    "interrupted",
                )

    def test_startup_validation_rejects_bool_iterations_and_falsy_types(self):
        from core.startup_validation import StartupValidationError, validate_mining_startup

        base = {"paradigm": "GP", "data_feeds": {"pairs": ["A"], "required_streams": ["close"]}}
        for bad in (
            {**base, "max_iterations": True},
            {**base, "fitness": 0},
            {**base, "search_space": []},
            {**base, "miner": "GP"},
            {**base, "paradigm": "RL", "rl_config": {"x": 1}},
            {**base, "experiment": {"seed": "abc"}},
            {**base, "evaluation": {"gates": {"holdout_mode": "loose"}}},
        ):
            with self.assertRaises(StartupValidationError):
                validate_mining_startup(bad)

    def test_custom_miner_may_keep_its_own_rl_config_key(self):
        from core.startup_validation import validate_mining_startup

        # The bundled MyCustomRL plugin reads ``rl_config``; a blanket ban
        # would break legitimate user extensions.
        cfg = {"paradigm": "MyCustomRL", "max_iterations": 1,
               "data_feeds": {"pairs": ["A"], "required_streams": ["close"]},
               "rl_config": {"learning_rate": 0.1}}
        with mock.patch("core.startup_validation.MinerRegistry") as reg_mock:
            reg_mock._registry = {"MyCustomRL": object()}
            validate_mining_startup(cfg)

    def test_advanced_gp_config_uses_paradigm_key(self):
        text = (Path(__file__).resolve().parents[1] / "user_workspace" / "configs" /
                "configAdvancedGP.json").read_text(encoding="utf-8-sig")
        cfg = json.loads(text)
        self.assertEqual(cfg["paradigm"], "AdvancedSampleGP")
        self.assertNotIn("miner", cfg)

    def test_weights_validation(self):
        from core.commands.backtest import _parse_weights

        self.assertEqual(_parse_weights("a=1,b=2", ["a", "b"]), {"a": 1.0, "b": 2.0})
        for raw in ("typo=1", "a=1,0.5", "a=nan", "a=0,b=0", "1,2,3"):
            with self.assertRaises(ValueError):
                _parse_weights(raw, ["a", "b"])

    def test_cli_flag_beats_config_and_config_beats_default(self):
        from core.commands import backtest as cmd

        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "costs.json")
            Path(cfg).write_text(json.dumps({"commission_rate": 0.9, "rebalance_every": 9}),
                                 encoding="utf-8")
            args = argparse.Namespace(
                config=cfg, factors="a", weights=None,
                no_expand_combinations=False,
                start="2026-01-01", end="2026-02-01",
                exchange="binance", timeframe="1d", trade_type="futures",
                db_root="factor_db", orientation_db_root=None, data_root="data",
                commission=0.1, slippage=None, funding=None, real_funding=False,
                rebalance=None, n_long=None, n_short=None, initial_cash=None,
                output=os.path.join(tmp, "out"),
                allow_holdout_reuse=False, holdout_mode=None,
            )
            with mock.patch.object(cmd, "run_backtest", return_value={
                "summary": {}, "result": mock.MagicMock(),
                "prices": pd.DataFrame(), "signals": pd.DataFrame(),
                "funding": pd.DataFrame(),
            }) as run, mock.patch.object(holdout_guard, "guard"), \
                 mock.patch("os.makedirs"), mock.patch("pandas.DataFrame.to_csv"):
                self.assertEqual(cmd.run_backtest_cmd(args), 0)
            self.assertAlmostEqual(run.call_args.kwargs["commission_rate"], 0.1)
            self.assertEqual(run.call_args.kwargs["rebalance_every"], 9)

    def test_file_dates_keep_intraday_bars(self):
        from core.experiment.windows import _file_dates

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.feather"
            pd.DataFrame({"date": pd.date_range("2026-01-01", periods=5, freq="1h")}) \
              .to_feather(path)
            dates = _file_dates(path)
            self.assertEqual(len(dates), 5)

    def test_sandbox_rejects_literal_arithmetic_overflow(self):
        from core.evaluation.code_sandbox import FactorCodePolicy, SecurityError

        code = "x = 999999 * 999999 * 999999\nfactor = df['close']"
        with self.assertRaises(SecurityError):
            FactorCodePolicy().visit(ast.parse(code))

    def test_get_data_info_reads_date_column(self):
        # ccxt is not installed in the system interpreter used by CI; stub it
        # for this pure file-inspection test.
        with mock.patch.dict(sys.modules, {"ccxt": mock.MagicMock()}):
            from core.data_feed.data_downloader import DataDownloader

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "x.feather"
                pd.DataFrame({"date": pd.date_range("2026-01-01", periods=3, freq="D"),
                              "close": [1.0, 2.0, 3.0]}).to_feather(path)
                out = DataDownloader().get_data_info(str(path))
                self.assertTrue(out["success"])
                self.assertEqual(out["date_range"]["start"], "2026-01-01 00:00:00")


if __name__ == "__main__":
    unittest.main()
