"""Ops commands after ``pipeline``: holdout reveal, factor triage, promotion.

These are the missing closes of the CLI loop: a planned window must be
revealable from the shell, a "0 promoted" run must explain itself, and a passing
factor must have a safe path into the production library. Everything runs on
temporary ledgers/libraries; the network and the real factor_db are untouched.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from core.commands import doctor, factors, holdout, pipeline, promote
from core.evaluation import holdout_guard
from core.evaluation.experiment_trace import HoldoutLedger


def _spec(root: Path, *, start: str = "auto", rolling: bool = True) -> str:
    path = root / "experiment_window.json"
    path.write_text(
        json.dumps({
            "label": "test rolling", "rolling": rolling, "start": start,
            "timeframe": "1d", "window_bars": 5, "embargo_bars": 1,
        }),
        encoding="utf-8",
    )
    return str(path)


def _tape(root: Path, n: int = 12, start: str = "2026-01-01") -> None:
    # Write straight to the naming layout so windows.plan_windows can find it.
    from core.data_feed.naming import data_path

    dates = pd.date_range(start, periods=n, freq="D")
    path = data_path(str(root), "binance", "BTC/USDT:USDT", "1d", "futures")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": dates,
        "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 10.0,
    }).to_feather(path)


def _holdout_args(root: Path, spec: str, command: str, **extra):
    base = dict(
        holdout_command=command, config=None, data_root=str(root), spec=spec,
        json=True, window=None, dry_run=False, holdout_mode=None,
        allow_holdout_reuse=False, note=None,
    )
    base.update(extra)
    return argparse.Namespace(**base)


def _call(cmd, args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cmd(args)
    return code, out.getvalue()


class HoldoutCommandTests(unittest.TestCase):
    def test_plan_is_read_only_and_shows_why_no_window_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _spec(root)
            _tape(root, n=5)
            ledger_path = str(root / "ledger.json")
            HoldoutLedger(ledger_path).reveal(
                "holdout_60bars_2026", "2026-09-16", "2026-11-15"
            )
            with mock.patch.dict(os.environ, {
                "FACTORMINER_HOLDOUT_LEDGER_PATH": ledger_path,
            }):
                code, text = _call(holdout.run_holdout_cmd, _holdout_args(root, spec, "plan"))
            plan = json.loads(text)
            self.assertEqual(code, 0)
            self.assertEqual(plan["anchor"]["start"], "2026-11-16")
            self.assertEqual(plan["windows"], [])
            self.assertEqual(len(HoldoutLedger(ledger_path).snapshot()), 1)

    def test_reveal_writes_a_rolling_key_once_and_keeps_the_anchor_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _spec(root, start="2026-01-01")
            _tape(root, n=12)
            ledger_path = str(root / "ledger.json")
            with mock.patch.dict(os.environ, {
                "FACTORMINER_HOLDOUT_LEDGER_PATH": ledger_path,
            }), mock.patch.object(holdout_guard, "_append_jsonl", lambda *a, **kw: None):
                code, text = _call(
                    holdout.run_holdout_cmd,
                    _holdout_args(root, spec, "reveal", window=1),
                )
                result = json.loads(text)
                entries = HoldoutLedger(ledger_path).snapshot()
                plan_after = holdout._plan(_holdout_args(root, spec, "plan"), {})

            self.assertEqual(code, 0)
            self.assertTrue(result["would_reveal"])
            self.assertEqual(result["window"]["start"], "2026-01-01")
            self.assertEqual(result["window"]["end"], "2026-01-05")
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0]["period_key"].startswith("rolling_"))
            # A rolling reveal must NOT move the pre-registered anchor.
            self.assertEqual(plan_after["anchor"]["start"], "2026-01-01")
            self.assertTrue(plan_after["windows"][0]["revealed"])

            with mock.patch.dict(os.environ, {
                "FACTORMINER_HOLDOUT_LEDGER_PATH": ledger_path,
            }):
                code2, _ = _call(
                    holdout.run_holdout_cmd,
                    _holdout_args(root, spec, "reveal", window=1),
                )
            self.assertEqual(code2, 1)

    def test_dry_run_never_writes_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _spec(root, start="2026-01-01")
            _tape(root, n=12)
            ledger_path = str(root / "ledger.json")
            with mock.patch.dict(os.environ, {
                "FACTORMINER_HOLDOUT_LEDGER_PATH": ledger_path,
            }):
                code, text = _call(
                    holdout.run_holdout_cmd,
                    _holdout_args(root, spec, "reveal", window=1, dry_run=True),
                )
            result = json.loads(text)
            self.assertEqual(code, 0)
            self.assertTrue(result["dry_run"])
            self.assertTrue(result["would_reveal"])
            self.assertFalse(Path(ledger_path).exists())

    def test_reveal_refuses_a_partial_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _spec(root, start="2026-01-01")
            _tape(root, n=3)  # fewer than window_bars=5
            ledger_path = str(root / "ledger.json")
            with mock.patch.dict(os.environ, {
                "FACTORMINER_HOLDOUT_LEDGER_PATH": ledger_path,
            }):
                code, _ = _call(
                    holdout.run_holdout_cmd,
                    _holdout_args(root, spec, "reveal", window=1),
                )
            self.assertEqual(code, 1)
            self.assertFalse(Path(ledger_path).exists())


class FactorsCommandTests(unittest.TestCase):
    @staticmethod
    def _run(root: Path) -> None:
        meta = root / "runs" / "run_test" / "factor_db" / "metadata"
        meta.mkdir(parents=True, exist_ok=True)
        for name, passed in (("fac_pass", True), ("fac_fail", False)):
            (meta / f"{name}.json").write_text(json.dumps({
                "factor_id": name, "lifecycle_status": "INSPECTED" if passed else "DISCOVERED_UNVERIFIED",
                "miner_type": "GP", "logic_hash": "abc", "created_at": "2026-11-15T00:00:00",
                "metrics": {
                    "gate_pass": passed,
                    "gate_checks": {"dsr_pass": passed, "empirical_null_pass": passed,
                                    "cost_adj_positive": passed},
                    "fitness_score": 3.0 if passed else 1.0,
                    "hac_t": 2.5, "p_adj": 0.01, "dsr": 0.9 if passed else 0.5,
                    "empirical_p": 0.02, "RankIC": 0.04, "RankIC_oos": 0.03,
                    "cost_adj_return": 1e-4,
                },
            }), encoding="utf-8")

    def test_collect_counts_rejections_and_ranks_passed_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root)
            payload = factors.collect("run_test", user_dir=str(root))
            self.assertEqual(payload["total"], 2)
            self.assertEqual(payload["n_passed"], 1)
            self.assertEqual(payload["factors"][0]["factor_id"], "fac_pass")
            self.assertEqual(
                payload["reject_reasons"],
                {"dsr_pass": 1, "empirical_null_pass": 1, "cost_adj_positive": 1},
            )

    def test_cli_emits_json_and_fails_on_a_missing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root)
            args = argparse.Namespace(
                run_id="run_test", user_dir=str(root), output_root=None,
                db_root=None, json=True, show_rejects=True, limit=20,
            )
            code, text = _call(factors.run_factors_cmd, args)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["n_passed"], 1)
            args.run_id = "run_missing"
            code, _ = _call(factors.run_factors_cmd, args)
            self.assertEqual(code, 1)


class PromoteCommandTests(unittest.TestCase):
    @staticmethod
    def _run(root: Path, *, passed: bool = True) -> None:
        db = root / "runs" / "run_test" / "factor_db"
        (db / "metadata").mkdir(parents=True, exist_ok=True)
        (db / "values").mkdir(parents=True, exist_ok=True)
        (db / "sources").mkdir(parents=True, exist_ok=True)
        (db / "metadata" / "fac_ok.json").write_text(json.dumps({
            "factor_id": "fac_ok", "miner_type": "LLM", "user_id": "t",
            "lifecycle_status": "INSPECTED" if passed else "DISCOVERED_UNVERIFIED",
            "logic_reference": {"type": "python_source", "source_file": "fac_ok.py"},
            "data_lineage": {"source": "unit"},
            "metrics": {
                "gate_pass": passed, "gate_checks": {"dsr_pass": passed},
                "dsr": 0.95, "hac_t": 3.1, "empirical_p": 0.01,
            },
        }), encoding="utf-8")
        pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01"]), "asset": ["BTC"],
            "factor": [0.1], "forward_return": [0.001],
        }).to_parquet(db / "values" / "fac_ok.parquet", index=False)
        (db / "sources" / "fac_ok.py").write_text("def f(x): return x\n", encoding="utf-8")

    @staticmethod
    def _args(root: Path, target: Path, **extra):
        base = dict(
            run_id="run_test", factor_id="fac_ok", db_root=str(target),
            user_dir=str(root), output_root=None, dry_run=False, force=False,
            json=True,
        )
        base.update(extra)
        return argparse.Namespace(**base)

    def test_promote_copies_metadata_values_and_source_with_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root)
            target = root / "production"
            args = self._args(root, target, json=False)

            code, text = _call(
                promote.run_promote_cmd, self._args(root, target, dry_run=True, json=False)
            )
            self.assertEqual(code, 0)
            self.assertIn("would promote", text)
            self.assertFalse(target.exists())

            code, text = _call(promote.run_promote_cmd, args)
            self.assertEqual(code, 0)
            self.assertTrue((target / "metadata" / "fac_ok.json").exists())
            self.assertTrue((target / "values" / "fac_ok.parquet").exists())
            self.assertTrue((target / "sources" / "fac_ok.py").exists())
            lineage = json.loads(
                (target / "metadata" / "fac_ok.json").read_text(encoding="utf-8")
            )["data_lineage"]
            self.assertEqual(lineage["promotion"]["run_id"], "run_test")
            self.assertTrue((root / "reports" / "promotions.jsonl").exists())

            code, _ = _call(promote.run_promote_cmd, args)
            self.assertEqual(code, 1)  # never overwrite a published id

    def test_promote_refuses_a_rejected_factor_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root, passed=False)
            target = root / "production"
            code, _ = _call(promote.run_promote_cmd, self._args(root, target))
            self.assertEqual(code, 1)
            self.assertFalse(target.exists())
            code, _ = _call(promote.run_promote_cmd, self._args(root, target, force=True))
            self.assertEqual(code, 0)
            self.assertTrue((target / "metadata" / "fac_ok.json").exists())


class PipelineRootAndStaleTests(unittest.TestCase):
    @staticmethod
    def _args(root: Path, **extra):
        cfg = root / "config.json"
        cfg.write_text(json.dumps({
            "data_feeds": {"data_root": str(root / "declared"), "pairs": []},
        }), encoding="utf-8")
        base = dict(
            config=str(cfg), data_root=None, skip_refresh=True, skip_doctor=False,
            dry_run=False, stale_is_fatal=False, fail_if_no_promotion=False,
            summary_dir=str(root / "summaries"), user_dir=str(root),
            miner=None, iterations=None, seed=None, resume=False, run_id=None,
            output_root=None, holdout_mode=None, allow_holdout_reuse=False,
            until=None, retries=1, skip_streams=True,
        )
        base.update(extra)
        return argparse.Namespace(**base)

    def test_default_data_root_comes_from_config_not_a_hardcoded_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self._args(root)
            self.assertEqual(pipeline._resolve_root(args), str(root / "declared"))
            args.data_root = str(root / "override")
            self.assertEqual(pipeline._resolve_root(args), str(root / "override"))

    def test_stale_refresh_aborts_before_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self._args(root, skip_refresh=False, stale_is_fatal=True)
            with mock.patch("core.commands.refresh.run_refresh_cmd", return_value=1), \
                 mock.patch("core.commands.run_experiment.run_experiment_cmd") as experiment:
                code = pipeline.run_pipeline_cmd(args)
            self.assertEqual(code, 1)
            experiment.assert_not_called()
            summaries = list((root / "summaries").glob("pipeline_*.json"))
            self.assertEqual(len(summaries), 1)
            self.assertIn("stale", json.loads(summaries[0].read_text(encoding="utf-8")))

    def test_freshness_warning_aborts_when_stale_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self._args(root, stale_is_fatal=True)
            rows = [{"check": "freshness", "status": doctor.WARN, "detail": "lag 5d"}]
            with mock.patch("core.commands.doctor.run_checks", return_value=rows), \
                 mock.patch("core.commands.run_experiment.run_experiment_cmd") as experiment:
                code = pipeline.run_pipeline_cmd(args)
            self.assertEqual(code, 1)
            experiment.assert_not_called()


class CliDispatchTests(unittest.TestCase):
    def test_main_reaches_all_new_commands_with_real_exit_codes(self):
        from core.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _spec(root)
            _tape(root, n=3)
            ledger_path = str(root / "ledger.json")

            FactorsCommandTests._run(root)
            with mock.patch.dict(os.environ, {
                "FACTORMINER_HOLDOUT_LEDGER_PATH": ledger_path,
            }), contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                plan_code = main([
                    "holdout", "plan", "--spec", spec, "--data-root", str(root), "--json",
                ])
                reveal_code = main([
                    "holdout", "reveal", "--spec", spec, "--data-root", str(root),
                    "--window", "1",
                ])
                factors_code = main([
                    "factors", "--run-id", "run_test", "--user-dir", str(root), "--json",
                ])
                promote_code = main([
                    "promote", "--run-id", "run_test", "--factor-id", "fac_fail",
                    "--user-dir", str(root), "--db-root", str(root / "production"),
                ])
            self.assertEqual(plan_code, 0)
            self.assertEqual(reveal_code, 1)   # 3 bars < window_bars=5
            self.assertEqual(factors_code, 0)
            self.assertEqual(promote_code, 1)  # gate_pass=false
            self.assertFalse(Path(ledger_path).exists())


if __name__ == "__main__":
    unittest.main()
