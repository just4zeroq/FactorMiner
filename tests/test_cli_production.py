"""Production CLI surface: ``doctor``, ``refresh`` and ``pipeline``.

The preflight and the unattended loop exist so a 40-minute run cannot start on a
short/stale/misconfigured panel and so a scheduler gets one exit code plus a
machine-readable summary. These tests pin the decision logic, not the network:
``doctor`` is exercised directly with an explicit ``now``, and ``refresh`` runs
with ``--dry-run``.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.commands import doctor, pipeline, refresh
from core.data_feed.naming import data_path

_REAL_CONFIG = Path(__file__).resolve().parents[1] / "user_workspace" / "configs" / "configExperiment.json"


def _write_tape(root: Path, pair: str, dates, timeframe: str = "1d") -> None:
    p = data_path(root, "binance", pair, timeframe, "futures")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "date": pd.to_datetime(list(dates)),
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 10.0,
        }
    ).to_feather(p)


def _config_file(tmp: Path, *, pairs, mine_period, test_period, extra=None) -> str:
    config = json.loads(_REAL_CONFIG.read_text(encoding="utf-8-sig"))
    config["data_feeds"]["pairs"] = pairs
    config["data_feeds"]["mine_period"] = [list(mine_period)]
    config["data_feeds"]["test_period"] = [list(test_period)]
    # Pin the data root inside the temp dir: otherwise the panel loader falls
    # back to the project's real ``data/`` and every test would pass or fail
    # depending on which symbols happened to be downloaded on this machine.
    config["data_feeds"]["data_root"] = str(tmp / "data")
    config.setdefault("storage", {})["db_root"] = str(tmp / "factor_db")
    if extra:
        extra(config)
    path = tmp / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_root = self.tmp / "data"
        self.now = pd.Timestamp("2026-11-16 00:30")

    def _rows(self, config_path):
        return doctor.run_checks(
            config_path, data_root=str(self.data_root), now=self.now
        )

    def test_ready_panel_has_no_fatal_check(self):
        dates = pd.date_range("2026-01-01", "2026-11-15", freq="D")
        _write_tape(self.data_root, "BTC/USDT:USDT", dates)
        cfg = _config_file(
            self.tmp,
            pairs=["BTC/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-09-13"),
        )
        rows = self._rows(cfg)
        status = {r["check"]: r["status"] for r in rows}
        self.assertEqual(status["config"], doctor.OK)
        self.assertEqual(status["universe"], doctor.OK)
        self.assertEqual(status["coverage"], doctor.OK)
        self.assertEqual(status["freshness"], doctor.OK)
        self.assertNotIn(doctor.FATAL, [r["status"] for r in rows])

    def test_missing_pair_is_fatal(self):
        cfg = _config_file(
            self.tmp,
            pairs=["BTC/USDT:USDT", "ETH/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-09-13"),
        )
        _write_tape(
            self.data_root, "BTC/USDT:USDT",
            pd.date_range("2026-01-01", "2026-11-15", freq="D"),
        )
        rows = self._rows(cfg)
        universe = [r for r in rows if r["check"] == "universe"][0]
        self.assertEqual(universe["status"], doctor.FATAL)
        self.assertIn("ETH/USDT:USDT", universe["detail"])

    def test_short_coverage_is_fatal(self):
        _write_tape(
            self.data_root, "BTC/USDT:USDT",
            pd.date_range("2026-01-01", "2026-06-30", freq="D"),
        )
        cfg = _config_file(
            self.tmp,
            pairs=["BTC/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-09-13"),
        )
        rows = self._rows(cfg)
        coverage = [r for r in rows if r["check"] == "coverage"]
        self.assertTrue(any(r["status"] == doctor.FATAL for r in coverage))
        self.assertTrue(any("test_period" in r["detail"] for r in coverage))
        # freshness of a panel that stops in June must warn, not fail
        fresh = [r for r in rows if r["check"] == "freshness"][0]
        self.assertEqual(fresh["status"], doctor.WARN)

    def test_unclosed_last_bar_is_not_counted_as_coverage(self):
        # The tape ends on the current day; that bar has not closed yet, so it
        # must not satisfy a period ending today.
        _write_tape(
            self.data_root, "BTC/USDT:USDT",
            pd.date_range("2026-01-01", "2026-11-16", freq="D"),
        )
        cfg = _config_file(
            self.tmp,
            pairs=["BTC/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-11-16"),
        )
        rows = self._rows(cfg)
        coverage = [r for r in rows if r["detail"].startswith("test_period")]
        self.assertEqual(coverage[0]["status"], doctor.FATAL)

    def test_strict_exit_code_promotes_warnings(self):
        _write_tape(
            self.data_root, "BTC/USDT:USDT",
            pd.date_range("2026-01-01", "2026-11-15", freq="D"),
        )
        cfg = _config_file(
            self.tmp,
            pairs=["BTC/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-09-13"),
        )
        args = argparse.Namespace(
            config=cfg, data_root=str(self.data_root), user_dir=str(self.tmp),
            json=False, strict=True, miner=None,
        )
        self.assertEqual(doctor.run_doctor_cmd(args), 1)
        args.strict = False
        self.assertEqual(doctor.run_doctor_cmd(args), 0)

    def test_missing_config_exits_nonzero(self):
        args = argparse.Namespace(
            config=str(self.tmp / "nope.json"), data_root=str(self.data_root),
            user_dir=str(self.tmp), json=True, strict=False, miner=None,
        )
        self.assertEqual(doctor.run_doctor_cmd(args), 1)


class RefreshAdapterTests(unittest.TestCase):
    def test_argv_mapping(self):
        args = argparse.Namespace(
            config="cfg.json", until="2026-11-14", symbols="BTC/USDT:USDT",
            timeframe="4h", data_root="d", retries=2, report_dir="r",
            start=None, dry_run=True,
        )
        argv = refresh.refresh_argv(args)
        self.assertEqual(argv[0], "--config")
        for flag in ("--until", "--symbols", "--timeframe", "--data-root",
                     "--retries", "--report-dir", "--dry-run"):
            self.assertIn(flag, argv)

    def test_dry_run_reports_without_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2026-01-01", "2026-11-15", freq="D")
            _write_tape(root / "data", "BTC/USDT:USDT", dates)
            cfg = _config_file(
                root, pairs=["BTC/USDT:USDT"],
                mine_period=("2026-01-02", "2026-06-30"),
                test_period=("2026-07-01", "2026-09-13"),
            )
            args = argparse.Namespace(
                config=cfg, until=None, symbols=None, timeframe=None,
                data_root=str(root / "data"), retries=1, report_dir=None,
                start=None, dry_run=True,
            )
            self.assertEqual(refresh.run_refresh_cmd(args), 0)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _args(self, config, **overrides):
        base = dict(
            config=config, miner=None, iterations=1, seed=1, resume=False,
            run_id=None, user_dir=str(self.tmp), output_root=str(self.tmp / "runs"),
            data_root=str(self.tmp / "data"), until=None, retries=1,
            holdout_mode=None, allow_holdout_reuse=False,
            skip_refresh=True, skip_doctor=True, dry_run=False,
            fail_if_no_promotion=False, summary_dir=str(self.tmp / "reports"),
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_dry_run_writes_a_summary_and_skips_mining(self):
        cfg = _config_file(
            self.tmp, pairs=["BTC/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-09-13"),
        )
        _write_tape(
            self.tmp / "data", "BTC/USDT:USDT",
            pd.date_range("2026-01-01", "2026-11-15", freq="D"),
        )
        args = self._args(cfg, dry_run=True, skip_refresh=True, skip_doctor=False)
        code = pipeline.run_pipeline_cmd(args)
        self.assertEqual(code, 0)
        reports = list((self.tmp / "reports").glob("pipeline_*.json"))
        self.assertEqual(len(reports), 1)
        summary = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual([s["stage"] for s in summary["stages"]], ["refresh", "doctor", "experiment"])
        self.assertIsNone(summary.get("run_id"))

    def test_fatal_doctor_blocks_the_run(self):
        cfg = _config_file(
            self.tmp, pairs=["BTC/USDT:USDT"],
            mine_period=("2026-01-02", "2026-06-30"),
            test_period=("2026-07-01", "2026-09-13"),
        )
        # no tape at all -> universe FATAL
        args = self._args(cfg, dry_run=False, skip_refresh=True, skip_doctor=False)
        code = pipeline.run_pipeline_cmd(args)
        self.assertEqual(code, 1)
        summary = json.loads(
            sorted((self.tmp / "reports").glob("pipeline_*.json"))[-1].read_text(encoding="utf-8")
        )
        self.assertEqual(summary["exit_code"], 1)
        stages = {s["stage"]: s for s in summary["stages"]}
        self.assertEqual(stages["doctor"]["exit_code"], 1)
        self.assertNotIn("experiment", stages)


class CryptoStreamRefreshTests(unittest.TestCase):
    """Declared streams must not silently go stale (attach skips missing files)."""

    def _config(self, tmp: Path, streams) -> str:
        config = json.loads(_REAL_CONFIG.read_text(encoding="utf-8-sig"))
        config["data_feeds"]["pairs"] = ["BTC/USDT:USDT"]
        config["data_feeds"]["mine_period"] = [["2026-01-01", "2026-06-30"]]
        config["data_feeds"]["test_period"] = [["2026-07-01", "2026-09-13"]]
        config["data_feeds"]["crypto_streams"] = streams
        path = tmp / "streams_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return str(path)

    def test_no_declared_streams_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root, [])
            code = refresh.refresh_crypto_streams(
                cfg, data_root=str(root / "data"), until="2026-11-14", dry_run=True
            )
        self.assertEqual(code, 0)

    def test_dry_run_plans_from_the_earliest_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root, ["funding_rate"])
            code = refresh.refresh_crypto_streams(
                cfg, data_root=str(root / "data"), until="2026-11-14", dry_run=True
            )
        self.assertEqual(code, 0)

    def test_missing_floor_without_a_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = json.loads(_REAL_CONFIG.read_text(encoding="utf-8-sig"))
            config["data_feeds"]["pairs"] = ["BTC/USDT:USDT"]
            config["data_feeds"]["mine_period"] = []
            config["data_feeds"]["test_period"] = []
            config["data_feeds"]["crypto_streams"] = ["funding_rate"]
            path = root / "nostart.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            code = refresh.refresh_crypto_streams(
                str(path), data_root=str(root / "data"), until="2026-11-14", dry_run=True
            )
        self.assertEqual(code, 1)


class ExperimentPreflightTests(unittest.TestCase):
    """``experiment --preflight`` must refuse to start on a broken dataset."""

    def test_preflight_aborts_before_mining(self):
        from core.commands.run_experiment import run_experiment_cmd

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config_file(
                root, pairs=["BTC/USDT:USDT"],
                mine_period=("2026-01-02", "2026-06-30"),
                test_period=("2026-07-01", "2026-09-13"),
            )
            args = argparse.Namespace(
                config=cfg, miner="GP", iterations=1, seed=1, resume=False,
                run_id=None, user_dir=str(root), output_root=str(root / "runs"),
                holdout_mode=None, allow_holdout_reuse=False, preflight=True,
            )
            with self.assertRaises(SystemExit) as ctx:
                run_experiment_cmd(args)
            self.assertEqual(ctx.exception.code, 1)
            # nothing was written: the abort happened before the run directory
            self.assertFalse((root / "runs").exists())

    def test_data_root_flag_reaches_the_preflight(self):
        """``--data-root`` must be the root the panel is read from.

        The bug this pins: the preflight resolved ``data_feeds.data_root`` while
        ``RealDataClient`` hardcoded ``cwd/data``.  A run pointed at an empty
        root therefore passed the preflight (it read the flag) and then mined
        the project's real data -- the check was decorative.
        """
        from core.commands.run_experiment import run_experiment_cmd

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            populated = root / "data"
            _write_tape(
                populated, "BTC/USDT:USDT", pd.date_range("2026-01-01", "2026-11-15", freq="D")
            )
            cfg = _config_file(
                root, pairs=["BTC/USDT:USDT"],
                mine_period=("2026-01-02", "2026-06-30"),
                test_period=("2026-07-01", "2026-09-13"),
            )
            empty = root / "empty_data"
            args = argparse.Namespace(
                config=cfg, miner="GP", iterations=1, seed=1, resume=False,
                run_id=None, user_dir=str(root), output_root=str(root / "runs"),
                holdout_mode=None, allow_holdout_reuse=False, preflight=True,
                data_root=str(empty),
            )
            # The override must win over the config's (populated) data_root.
            with self.assertRaises(SystemExit) as ctx:
                run_experiment_cmd(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertFalse(empty.exists())


class DataRootContractTests(unittest.TestCase):
    """The panel loader must read the same directory the preflight validated."""

    def _client(self, tmp: Path, data_root=None):
        from core.data_feed.real_client import RealDataClient

        feeds = {
            "exchange": "binance",
            "instrument_type": "futures",
            "timeframe": "1d",
            "pairs": ["BTC/USDT:USDT"],
            "mine_period": [["2026-01-02", "2026-06-30"]],
            "test_period": [],
            "mining_mode": "sequential_single",
        }
        if data_root is not None:
            feeds["data_root"] = str(data_root)
        return RealDataClient({"data_feeds": feeds})

    def test_panel_is_read_from_the_configured_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tape(root / "custom", "BTC/USDT:USDT",
                        pd.date_range("2026-01-01", "2026-04-01", freq="D"))
            client = self._client(root, root / "custom")
            self.assertIsNotNone(client._mine_data)
            self.assertFalse(client._mine_data.empty)

    def test_relative_root_anchors_at_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tape(root / "rot", "BTC/USDT:USDT",
                        pd.date_range("2026-01-01", "2026-04-01", freq="D"))
            previous = os.getcwd()
            os.chdir(root)
            try:
                client = self._client(root, "rot")
                self.assertFalse(client._mine_data.empty)
            finally:
                os.chdir(previous)

    def test_missing_file_off_the_canonical_root_never_auto_downloads(self):
        """A stray root must fail fast, not silently download into ``cwd/data``."""
        from unittest import mock

        import core.data_feed.data_downloader as downloader_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            class _Never:
                def __init__(self, *a, **kw):
                    calls.append(a)

            with mock.patch.object(downloader_mod, "DataDownloader", _Never):
                client = self._client(root, root / "nowhere")
            self.assertTrue(calls == [])
            self.assertTrue(client._mine_data is None or client._mine_data.empty)


class HoldoutWindowMessageTests(unittest.TestCase):
    """``0 ready`` must say *why*: anchor, progress, and when the 30th bar lands."""

    def test_zero_windows_reports_the_anchor_and_the_eta(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            ledger.write_text(
                json.dumps(
                    [
                        {
                            "period_key": "holdout_60bars_2026",
                            "start": "2026-09-16",
                            "end": "2026-11-15",
                            "at": "2026-11-15T13:11:49",
                            "note": "",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            _write_tape(
                root / "data", "BTC/USDT:USDT",
                pd.date_range("2026-01-01", "2026-11-14", freq="D"),
            )
            spec = {
                "label": "t", "rolling": True, "start": "auto",
                "timeframe": "1d", "window_bars": 30, "embargo_bars": 5,
            }
            with mock.patch(
                "core.evaluation.holdout_guard.default_ledger_path",
                return_value=str(ledger),
            ), mock.patch(
                "core.evaluation.holdout_guard.load_window_spec", return_value=spec
            ):
                rows = doctor._check_holdout({}, data_root=str(root / "data"))

        row = {r["check"]: r for r in rows}["holdout_window"]
        self.assertEqual(row["status"], doctor.WARN)
        # The anchor is the day after the burned window, so nothing before it
        # can be chunked -- the message has to say that instead of "0 planned".
        self.assertIn("anchor 2026-11-16", row["detail"])
        self.assertIn("0/30", row["detail"])
        self.assertIn("2026-12-15", row["detail"])


class MineFlagTests(unittest.TestCase):
    """``mine`` must accept the same reproducibility flags as ``experiment``."""

    def test_seed_and_data_root_reach_the_director(self):
        from unittest import mock

        from core.commands import mine as mine_cmd

        captured = {}

        class _Director:
            def __init__(self, config, client):
                captured["config"] = config

            def run(self, max_iterations):
                return []

            def get_latest_factor_ids(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "max_iterations": 1,
                        "data_feeds": {"pairs": ["BTC/USDT:USDT"], "timeframe": "1d"},
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                miner="GP", config=str(cfg), iterations=1, user_dir=str(root),
                seed=7, data_root=str(root / "market"),
            )
            with mock.patch.object(mine_cmd, "FactorMinerDirector", _Director), \
                 mock.patch.object(mine_cmd, "load_user_modules", lambda *a, **kw: None), \
                 mock.patch.object(mine_cmd, "validate_mining_startup", lambda *a, **kw: None), \
                 mock.patch("core.data_feed.real_client.RealDataClient", lambda config: object()):
                mine_cmd.run_miner(args)

        self.assertEqual(captured["config"]["experiment"]["seed"], 7)
        self.assertEqual(captured["config"]["data_feeds"]["data_root"], str(root / "market"))


if __name__ == "__main__":
    unittest.main()
