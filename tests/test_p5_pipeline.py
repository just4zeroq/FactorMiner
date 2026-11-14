"""P5: the end-to-end pipeline contract (isolated from real mining I/O)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.experiment import pipeline


def minimal_config():
    return {
        "paradigm": "GP",
        "population_size": 8,
        "max_iterations": 1,
        "data_feeds": {
            "exchange": "binance",
            "instrument_type": "futures",
            "timeframe": "1d",
            "pairs": ["BTC/USDT:USDT"],
            "mine_period": [["2023-04-01", "2024-12-31"]],
            "test_period": [["2025-01-01", "2025-06-30"]],
        },
        "fitness": {"gate_cost_rate": 0.0018},
        "backtest": {"commission_rate": 0.0004, "slippage_rate": 0.0005},
        "experiment": {"seed": 42},
    }


class TestPipelineHelpers(unittest.TestCase):
    def test_windows_from_first_test_period(self):
        self.assertEqual(
            pipeline._windows(minimal_config()),
            {"start": "2025-01-01", "end": "2025-06-30"},
        )

    def test_jsonable_drops_runtime_objects(self):
        out = pipeline._jsonable({"a": 1, "_runtime": {"x": object()}, "b": [1, 2]})
        self.assertEqual(out, {"a": 1, "b": [1, 2]})

    def test_report_contains_discipline_and_limitations(self):
        text = pipeline._report_text("run_x", {"created_at": "t"}, {
            "mining": {"n_candidates": 3, "factor_ids": ["a"], "n_promoted": 0,
                       "val_read_count": 30, "verdicts": []},
            "validated": {"n_folds": 5},
            "data_checked": {"ok": True, "warnings": []},
        })
        self.assertIn("run_x", text)
        self.assertIn("Holdout guard", text)
        self.assertIn("Empirical target-permutation null", text)
        self.assertIn("NOT that a profitable factor", text)
        self.assertIn("rolling pre-registered", text)
        self.assertNotIn("2026-12 onward, 90 daily bars", text)

    def test_combine_scans_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "metadata"
            meta.mkdir()
            (meta / "f1.json").write_text(json.dumps({
                "factor_id": "f1", "miner_type": "linear_combination",
                "logic_reference": {"weights": [0.5, 0.5]},
                "member_factor_ids": ["a", "b"],
            }), encoding="utf-8")
            out = pipeline._stage_combine(tmp)
            self.assertEqual(out["n_combination_factors"], 1)


class TestPipelineStages(unittest.TestCase):
    def test_all_eight_stages_run_and_archive(self):
        cfg = minimal_config()
        quality = MagicMock()
        quality.as_dict.return_value = {"ok": True, "critical": [], "warnings": [], "stats": {}}
        client = MagicMock()
        mining_result = {
            "n_candidates": 3,
            "factor_ids": ["fac_a", "fac_b"],
            "verdicts": [
                {"factor_id": "fac_a", "status": "DISCOVERED_UNVERIFIED",
                 "empirical_p": 0.01, "gate_checks": {"empirical_null_pass": True}},
            ],
            "n_promoted": 0,
            "val_read_count": 42,
            "hypothesis_budget": {"cumulative_tests": 42},
            "budget_snapshot": {},
        }
        wf_result = {"n_folds": 5, "refit_per_fold": True,
                     "weights_differ_across_folds": True, "mean_rank_ic": 0.01,
                     "all_folds_positive": False, "folds": []}
        oos_result = {
            "oos_root": "/tmp/oos", "written": ["fac_a", "fac_b"],
            "n_written": 2, "orientation_db_root": "/tmp/in",
        }
        bt_result = {"summary": {"scope": "2025-01-01..2025-06-30",
                                 "total_return_pct": -1.0, "sharpe": -0.2,
                                 "funding_source": "crypto_streams (20 assets)"}}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("core.data_feed.real_client.RealDataClient", return_value=client), \
                 patch("core.data_feed.panel_validator.validate_panel", return_value=quality), \
                 patch("core.evaluation.holdout_guard.guard_config"), \
                 patch("core.evaluation.experiment_trace.HoldoutLedger") as ledger, \
                 patch.object(pipeline, "_stage_mining", return_value=mining_result), \
                 patch.object(pipeline, "_stage_oos_recompute", return_value=oos_result), \
                 patch("core.experiment.walk_forward.run_walk_forward", return_value=wf_result), \
                 patch("core.backtest.backtest_engine.run_backtest", return_value=bt_result):
                ledger.return_value.snapshot.return_value = []
                result = pipeline.run_experiment(
                    cfg, config_path="cfg.json", root=Path(tmp), progress=lambda m: None
                )
            run = Path(result["run_dir"])
            status = json.loads((run / "status.json").read_text(encoding="utf-8"))
            for stage in ("data_checked", "mining", "gated", "validated",
                          "combined", "oos_prepared", "backtested", "completed"):
                self.assertIn(stage, status["completed_stages"], stage)
            for name in ("manifest.json", "manifest_final.json", "metrics.json",
                         "report.md", "walk_forward.json", "state.json"):
                self.assertTrue((run / name).exists(), name)
            metrics = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["mining"]["val_read_count"], 42)
            self.assertTrue(metrics["cost_identity"]["consistent"])
            self.assertIn("Empirical target-permutation null",
                          (run / "report.md").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "completed")


class TestResume(unittest.TestCase):
    def test_resume_skips_completed_stages(self):
        from core.experiment import manifest as mf, registry

        cfg = minimal_config()
        wf_result = {"n_folds": 3, "folds": []}
        bt_result = {"summary": {"scope": "x"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m = mf.build_manifest(cfg)
            path = registry.create_run(m, cfg, root=root)
            registry.update_status(m["run_id"], "data_checked", root=root)
            registry.update_status(m["run_id"], "mining", root=root)
            (path / "state.json").write_text(json.dumps({
                "data_checked": {"ok": True, "warnings": []},
                "mining": {"n_candidates": 1, "factor_ids": [], "verdicts": [],
                           "n_promoted": 0, "val_read_count": 1,
                           "hypothesis_budget": {}},
            }), encoding="utf-8")

            with patch("core.data_feed.real_client.RealDataClient") as client_cls, \
                 patch("core.data_feed.panel_validator.validate_panel") as validate, \
                 patch("core.evaluation.holdout_guard.guard_config"), \
                 patch("core.evaluation.experiment_trace.HoldoutLedger") as ledger, \
                 patch.object(pipeline, "_stage_mining") as mine, \
                 patch.object(pipeline, "_stage_oos_recompute") as oos, \
                 patch("core.experiment.walk_forward.run_walk_forward", return_value=wf_result), \
                 patch("core.backtest.backtest_engine.run_backtest", return_value=bt_result):
                ledger.return_value.snapshot.return_value = []
                result = pipeline.run_experiment(
                    cfg, resume=True, root=root, progress=lambda m: None
                )
                mine.assert_not_called()
                validate.assert_not_called()
                client_cls.assert_not_called()
                oos.assert_not_called()

            self.assertEqual(result["run_id"], m["run_id"])
            status = json.loads((path / "status.json").read_text(encoding="utf-8"))
            self.assertIn("completed", status["completed_stages"])


class TestExperimentCLI(unittest.TestCase):
    def test_subcommand_registered(self):
        from core.cli import setup_parser
        args = setup_parser().parse_args([
            "experiment", "--config", "cfg.json", "--seed", "7", "--resume"
        ])
        self.assertEqual(args.command, "experiment")
        self.assertEqual(args.seed, 7)
        self.assertTrue(args.resume)


if __name__ == "__main__":
    unittest.main()
