"""P7-2: cross-sectional (universe) holdout — split, ledger and pipeline wiring."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.experiment import pipeline
from core.experiment import universe as uni


def _cfg(n: int = 10, holdout=None, **extra):
    pairs = [f"A{i}/USDT:USDT" for i in range(n)]
    feeds = {
        "exchange": "binance",
        "instrument_type": "futures",
        "timeframe": "1d",
        "pairs": pairs,
        "mine_period": [["2023-04-01", "2024-12-31"]],
        "test_period": [["2025-01-01", "2025-06-30"]],
    }
    if holdout is not None:
        feeds["universe_holdout"] = holdout
    feeds.update(extra)
    return {
        "paradigm": "GP",
        "population_size": 8,
        "max_iterations": 1,
        "data_feeds": feeds,
        "fitness": {"gate_cost_rate": 0.0018},
        "backtest": {"commission_rate": 0.0004, "slippage_rate": 0.0005},
        "experiment": {"seed": 42},
        "storage": {"db_root": "factor_db"},
    }


class _Report:
    def as_dict(self):
        return {"ok": True, "critical": [], "warnings": [], "stats": {}}


class TestSplitResolution(unittest.TestCase):
    def test_disabled_when_not_configured(self):
        sp = uni.resolve_split(_cfg())
        self.assertFalse(sp.enabled)
        self.assertEqual(len(sp.train_pairs), 10)
        self.assertEqual(sp.holdout_pairs, [])

    def test_split_of_existing_universe_is_not_clean(self):
        sp = uni.resolve_split(_cfg(holdout=["A7/USDT:USDT", "A8/USDT:USDT",
                                             "A9/USDT:USDT"]))
        self.assertTrue(sp.enabled)
        self.assertFalse(sp.clean)
        self.assertEqual(sp.source, "explicit")
        self.assertEqual(len(sp.train_pairs), 7)
        self.assertEqual(len(sp.holdout_pairs), 3)
        self.assertFalse(set(sp.train_pairs) & set(sp.holdout_pairs))
        self.assertTrue(sp.split_hash)

    def test_disjoint_symbols_are_clean(self):
        fresh = ["NEW1/USDT:USDT", "NEW2/USDT:USDT", "NEW3/USDT:USDT"]
        sp = uni.resolve_split(_cfg(holdout=fresh))
        self.assertTrue(sp.clean)
        self.assertEqual(len(sp.train_pairs), 10)
        self.assertEqual(sp.holdout_pairs, sorted(fresh))

    def test_fraction_split_is_deterministic(self):
        cfg = _cfg(universe_holdout_frac=0.3)
        a = uni.resolve_split(cfg)
        b = uni.resolve_split(cfg)
        self.assertTrue(a.enabled)
        self.assertEqual(a.holdout_pairs, b.holdout_pairs)
        self.assertEqual(a.split_hash, b.split_hash)
        self.assertEqual(len(a.holdout_pairs), 3)

    def test_mixed_old_and_new_is_rejected(self):
        with self.assertRaises(ValueError):
            uni.resolve_split(_cfg(holdout=["A1/USDT:USDT", "NEW1/USDT:USDT",
                                            "NEW2/USDT:USDT"]))

    def test_too_few_assets_is_rejected(self):
        with self.assertRaises(ValueError):
            uni.resolve_split(_cfg(n=4, holdout=["A2/USDT:USDT",
                                                 "A3/USDT:USDT"]))

    def test_config_builders_set_the_right_universe(self):
        sp = uni.resolve_split(_cfg(holdout=["A7/USDT:USDT", "A8/USDT:USDT",
                                             "A9/USDT:USDT"]))
        cfg = _cfg(holdout=sp.holdout_pairs)
        tr = uni.train_config(cfg, sp)
        ho = uni.holdout_config(cfg, sp)
        self.assertEqual(tr["data_feeds"]["pairs"], sp.train_pairs)
        self.assertEqual(tr["data_feeds"]["universe_role"], "train")
        self.assertEqual(ho["data_feeds"]["pairs"], sp.holdout_pairs)
        self.assertEqual(ho["data_feeds"]["universe_role"], "holdout")
        self.assertEqual(cfg["data_feeds"]["pairs"], [f"A{i}/USDT:USDT" for i in range(10)])


class TestUniverseLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")
        self.cfg = _cfg(holdout=["A7/USDT:USDT", "A8/USDT:USDT", "A9/USDT:USDT"])
        self.sp = uni.resolve_split(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_use_is_recorded_and_second_is_blocked(self):
        first = uni.guard(self.sp, source="test:first", ledger_path=self.path)
        self.assertTrue(first["recorded"])
        self.assertFalse(first["reused"])
        with self.assertRaises(uni.UniverseReuseError):
            uni.guard(self.sp, source="test:second", ledger_path=self.path)
        self.assertEqual(uni.status(self.path)["n_revealed"], 1)

    def test_audit_mode_allows_reuse(self):
        self.sp.mode = "audit"
        uni.guard(self.sp, ledger_path=self.path)
        second = uni.guard(self.sp, ledger_path=self.path)
        self.assertTrue(second["allowed"])
        self.assertTrue(second["reused"])

    def test_allow_reuse_for_recomputing_published_number(self):
        uni.guard(self.sp, ledger_path=self.path)
        again = uni.guard(self.sp, ledger_path=self.path, allow_reuse=True)
        self.assertTrue(again["allowed"])
        self.assertEqual(again["reason"], "allow_reuse")

    def test_require_fresh_rejects_pseudo_holdout(self):
        self.sp.require_fresh = True
        with self.assertRaises(uni.UniverseNotCleanError):
            uni.guard(self.sp, ledger_path=self.path)
        self.assertEqual(uni.status(self.path)["n_revealed"], 0)

    def test_clean_split_is_recorded_as_clean(self):
        fresh = uni.resolve_split(_cfg(
            holdout=["N1/USDT:USDT", "N2/USDT:USDT", "N3/USDT:USDT"]))
        fresh.require_fresh = True
        fresh.mode = "strict"
        uni.guard(fresh, ledger_path=self.path)
        self.assertEqual(uni.status(self.path)["n_clean"], 1)


class TestOOSRecomputeUsesReservedClient(unittest.TestCase):
    def test_values_are_computed_from_holdout_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "in" / "metadata").mkdir(parents=True)
            (root / "oos" / "values").mkdir(parents=True)
            (root / "oos" / "metadata").mkdir(parents=True)

            train_client = MagicMock()
            oos_client = MagicMock()
            oos_client.get_test_data.return_value = "HOLDOUT_PANEL"
            candidate = MagicMock()
            candidate.compute.return_value = "VALUES"

            with patch("core.storage.factor_storage.LocalFactorStorage") as storage:
                storage._snapshot_frame.return_value = MagicMock(empty=False)
                out = pipeline._stage_oos_recompute(
                    {"storage": {"db_root": str(root / "in")}},
                    train_client, MagicMock(), [candidate], ["fac_x"],
                    str(root / "oos"), client_oos=oos_client,
                )
            train_client.get_test_data.assert_not_called()
            oos_client.get_test_data.assert_called_once()
            candidate.compute.assert_called_once_with("HOLDOUT_PANEL")
            self.assertEqual(out["universe_role"], "holdout")


class TestSmallUniverseBacktest(unittest.TestCase):
    def test_book_is_scaled_down_for_a_reserved_universe(self):
        from core.backtest.backtest_engine import adapt_book_to_universe

        six = adapt_book_to_universe(6, 5, 5)
        self.assertEqual(six["n_long"], 2)
        self.assertEqual(six["n_short"], 2)
        self.assertEqual(six["min_assets"], 4)

    def test_normal_universe_is_unchanged(self):
        from core.backtest.backtest_engine import adapt_book_to_universe

        twenty = adapt_book_to_universe(20, 5, 5)
        self.assertEqual(twenty["n_long"], 5)
        self.assertEqual(twenty["n_short"], 5)
        self.assertEqual(twenty["min_assets"], 10)

    def test_explicit_min_assets_is_respected(self):
        from core.backtest.backtest_engine import adapt_book_to_universe

        self.assertEqual(adapt_book_to_universe(6, 5, 5, 3)["min_assets"], 3)

    def test_single_asset_is_rejected_instead_of_silently_empty(self):
        from core.backtest.backtest_engine import adapt_book_to_universe

        with self.assertRaises(ValueError):
            adapt_book_to_universe(1, 5, 5)

    def test_real_small_universe_backtest_actually_trades(self):
        """Regression: 6 reserved assets used to yield 0 trades / 0% return.

        Builds a temporary factor snapshot over 6 real local symbols so the
        check works on a fresh clone too (no dependence on archived runs).
        """
        import numpy as np
        import pandas as pd

        from core.backtest.backtest_engine import run_backtest

        assets = ["DOT/USDT:USDT", "ETC/USDT:USDT", "FIL/USDT:USDT",
                  "LINK/USDT:USDT", "LTC/USDT:USDT", "TRX/USDT:USDT"]
        if not Path("data/binance/futures").exists():
            self.skipTest("local OHLCV library not present")
        rng = np.random.default_rng(7)
        rows = []
        for t in pd.date_range("2025-01-01", periods=60, freq="D"):
            for i, asset in enumerate(assets):
                rows.append({
                    "timestamp": t,
                    "asset": asset,
                    "factor": float(i) + rng.normal(scale=0.2),
                    "forward_return": 0.0005 * i + rng.normal(scale=0.001),
                })
        with tempfile.TemporaryDirectory() as tmp:
            values = Path(tmp) / "values"
            values.mkdir(parents=True)
            (Path(tmp) / "metadata").mkdir()
            pd.DataFrame(rows).to_parquet(values / "fac_small.parquet", index=False)
            summary = run_backtest(
                ["fac_small"], db_root=str(tmp),
                start="2025-01-01", end="2025-03-01",
                commission_rate=0.0004, slippage_rate=0.0005,
                use_real_funding=True,
                # Synthetic snapshot has no pre-registered metadata orientation;
                # opt in explicitly to the legacy infer-from-evaluation path.
                allow_inferred_orientation=True,
            )["summary"]
        self.assertEqual(summary["n_assets"], 6)
        self.assertEqual(summary["n_long"], 2)
        self.assertGreater(summary["num_trades"], 0)
        self.assertGreater(summary["num_fills"], 0)


class TestPipelineUniverseWiring(unittest.TestCase):
    def test_run_experiment_uses_two_data_clients(self):
        cfg = _cfg(holdout=["A7/USDT:USDT", "A8/USDT:USDT", "A9/USDT:USDT"],
                   universe_holdout_mode="audit")
        train_client, holdout_client = MagicMock(), MagicMock()
        train_client.get_data.return_value = "TRAIN_DATA"
        train_client.get_returns.return_value = "TRAIN_RET"
        holdout_client.get_test_data.return_value = "HOLDOUT_DATA"
        holdout_client.get_test_returns.return_value = "HOLDOUT_RET"

        mining_result = {
            "n_candidates": 1, "factor_ids": ["fac_a"], "verdicts": [],
            "n_promoted": 0, "val_read_count": 5, "hypothesis_budget": {},
            "budget_snapshot": {},
            "director": MagicMock(), "candidates": [MagicMock()],
        }
        oos_result = {"oos_root": "/tmp/oos", "written": ["fac_a"],
                      "n_written": 1, "orientation_db_root": "/tmp/in",
                      "universe_role": "holdout"}
        bt_result = {"summary": {"scope": "2025-01-01..2025-06-30",
                                 "total_return_pct": -1.0, "sharpe": -0.2}}

        with tempfile.TemporaryDirectory() as tmp:
            ledger = str(Path(tmp) / "universe_ledger.json")
            with patch.dict(os.environ, {"FACTORMINER_UNIVERSE_LEDGER_PATH": ledger}), \
                 patch("core.data_feed.real_client.RealDataClient",
                       side_effect=[train_client, holdout_client]) as client_cls, \
                 patch("core.data_feed.panel_validator.validate_panel",
                       return_value=_Report()), \
                 patch("core.evaluation.holdout_guard.guard_config"), \
                 patch("core.evaluation.experiment_trace.HoldoutLedger") as hl, \
                 patch.object(pipeline, "_stage_mining", return_value=mining_result), \
                 patch.object(pipeline, "_stage_oos_recompute",
                              return_value=oos_result) as oos_mock, \
                 patch("core.experiment.walk_forward.run_walk_forward",
                       return_value={"n_folds": 3, "folds": []}), \
                 patch("core.backtest.backtest_engine.run_backtest",
                       return_value=bt_result):
                hl.return_value.snapshot.return_value = []
                result = pipeline.run_experiment(
                    cfg, config_path="cfg.json", root=Path(tmp) / "runs",
                    progress=lambda m: None,
                )

            self.assertEqual(client_cls.call_count, 2)
            first_cfg = client_cls.call_args_list[0].args[0]
            second_cfg = client_cls.call_args_list[1].args[0]
            self.assertEqual(len(first_cfg["data_feeds"]["pairs"]), 7)
            self.assertEqual(first_cfg["data_feeds"]["universe_role"], "train")
            self.assertEqual(len(second_cfg["data_feeds"]["pairs"]), 3)
            self.assertEqual(second_cfg["data_feeds"]["universe_role"], "holdout")
            self.assertIs(oos_mock.call_args.kwargs["client_oos"], holdout_client)

            run = Path(result["run_dir"])
            metrics = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
            self.assertTrue(metrics["universe"]["enabled"])
            self.assertFalse(metrics["universe"]["clean"])
            report = (run / "report.md").read_text(encoding="utf-8")
            self.assertIn("Universe holdout", report)
            self.assertIn("NOT clean", report)

    def test_report_marks_clean_holdout_without_warning(self):
        text = pipeline._report_text("run_x", {"created_at": "t"}, {
            "mining": {"verdicts": []},
            "universe": {"enabled": True, "clean": True, "source": "explicit",
                         "n_train": 20, "n_holdout": 6,
                         "guard": {"reused": False}},
            "oos_prepared": {"universe_role": "holdout"},
        })
        self.assertIn("clean = `True`", text)
        self.assertNotIn("NOT clean", text)


if __name__ == "__main__":
    unittest.main()
