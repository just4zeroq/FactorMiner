"""Scale-up toolkit: funnel, trial families, overrides, sweep, factor-db, cache.

Every test here corresponds to a concrete operational gap found in
``docs/reviews/cli_mining_gap_analysis.md``. They are grouped in one file
because they share the same synthetic factor library fixture.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.config.overrides import OverrideError, apply_overrides
from core.evaluation.funnel import screen_candidate, stage_a
from core.experiment import budget as budget_mod
from core.experiment.estimate import estimate
from core.experiment.sweep import SweepError, expand, select_shard
from core.miner.seeding import load_seed_asts


def _ast(window: int = 20) -> dict:
    return {"op": "ts_zscore", "left": "close", "window": window}


class FunnelTests(unittest.TestCase):
    def test_negative_cost_adjacent_is_rejected(self):
        keep, reason = screen_candidate(
            {"RankIC": 0.05, "cost_adj_return": -0.001, "coverage": 0.9}
        )
        self.assertFalse(keep)
        self.assertEqual(reason, "cost_adj_negative")

    def test_low_rankic_is_rejected(self):
        keep, reason = screen_candidate(
            {"RankIC": 0.001, "cost_adj_return": 0.001, "coverage": 0.9}
        )
        self.assertFalse(keep)
        self.assertEqual(reason, "rankic_below_floor")

    def test_turnover_cap_is_a_hard_reject(self):
        keep, reason = screen_candidate(
            {"RankIC": 0.05, "cost_adj_return": 0.001, "coverage": 0.9,
             "ls_turnover": 0.4},
            turnover_max=0.15,
        )
        self.assertFalse(keep)
        self.assertEqual(reason, "turnover_above_cap")

    def test_survivor_and_cap(self):
        metrics = [
            {"RankIC": 0.05, "cost_adj_return": 0.002, "coverage": 0.9,
             "fitness_score": 5.0, "ls_turnover": 0.05},
            {"RankIC": 0.06, "cost_adj_return": 0.003, "coverage": 0.9,
             "fitness_score": 7.0, "ls_turnover": 0.05},
            {"RankIC": 0.0001, "cost_adj_return": -0.01, "coverage": 0.2},
        ]
        outcome = stage_a(metrics, {"prefilter_keep": 1})
        self.assertEqual(outcome["survivors"], [1], "cap keeps the fittest")
        self.assertEqual(outcome["reasons"].get("rank_cut"), 1)
        self.assertEqual(outcome["reasons"].get("coverage_below_floor"), 1)

    def test_missing_diagnostics_do_not_fake_a_pass(self):
        keep, reason = screen_candidate({})
        self.assertFalse(keep)
        self.assertEqual(reason, "no_metrics")


class TrialFamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "budget.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_families_do_not_share_a_denominator(self):
        budget_mod.sync(100, [0.01, 0.02], path=self.path, family="fam_a")
        budget_mod.sync(10, [0.5], path=self.path, family="fam_b")
        base_a, family_a, snap = budget_mod.resolve(
            0, [], [], path=self.path, family="fam_a"
        )
        base_b, family_b, _ = budget_mod.resolve(
            0, [], [], path=self.path, family="fam_b"
        )
        self.assertEqual(base_a, 100)
        self.assertEqual(base_b, 10)
        self.assertEqual(len(family_a), 2)
        self.assertEqual(len(family_b), 1)
        self.assertGreaterEqual(snap["global_n_trials"], 100)

    def test_unknown_family_falls_back_to_global(self):
        budget_mod.sync(50, [0.1], path=self.path)
        base, _, _ = budget_mod.resolve(0, [], [], path=self.path, family="nope")
        self.assertEqual(base, 50)

    def test_legacy_snapshot_without_families_still_loads(self):
        self.path.write_text(
            json.dumps({"n_trials": 7, "p_values": [0.2], "runs": 1}),
            encoding="utf-8",
        )
        base, family, _ = budget_mod.resolve(0, [], [], path=self.path)
        self.assertEqual(base, 7)
        self.assertEqual(family, [0.2])


class OverrideTests(unittest.TestCase):
    def test_dotted_path_and_json_values(self):
        config, applied = apply_overrides(
            {"gp": {"max_depth": 3}, "flag": False},
            ["gp.max_depth=5", "flag=true", "target.horizon_bars=20"],
        )
        self.assertEqual(config["gp"]["max_depth"], 5)
        self.assertTrue(config["flag"])
        self.assertEqual(config["target"]["horizon_bars"], 20)
        self.assertEqual(len(applied), 3)

    def test_malformed_assignment_is_rejected(self):
        with self.assertRaises(OverrideError):
            apply_overrides({}, ["gp.max_depth"])

    def test_descending_into_a_scalar_is_rejected(self):
        with self.assertRaises(OverrideError):
            apply_overrides({"gp": 3}, ["gp.max_depth=5"])

    def test_original_config_is_not_mutated(self):
        original = {"gp": {"max_depth": 3}}
        apply_overrides(original, ["gp.max_depth=9"])
        self.assertEqual(original["gp"]["max_depth"], 3)


class SweepTests(unittest.TestCase):
    def _sweep(self):
        return {
            "name": "h",
            "grid": {"target.horizon_bars": [5, 20], "rebalance_every": [1, 5]},
            "set": ["fitness.turnover_max=0.15"],
        }

    def test_expansion_is_the_cartesian_product(self):
        cells = expand({"target": {}}, self._sweep())
        self.assertEqual(len(cells), 4)
        self.assertTrue(all(c["config"]["fitness"]["turnover_max"] == 0.15 for c in cells))
        families = {c["family"] for c in cells}
        self.assertEqual(len(families), 4, "每个 cell 必须有自己的 trial family")

    def test_shards_partition_without_overlap(self):
        cells = expand({"target": {}}, self._sweep())
        shard1 = select_shard(cells, "1/2")
        shard2 = select_shard(cells, "2/2")
        self.assertEqual(len(shard1) + len(shard2), len(cells))
        self.assertFalse({c["label"] for c in shard1} & {c["label"] for c in shard2})

    def test_bad_shard_is_rejected(self):
        with self.assertRaises(SweepError):
            select_shard([], "3/2")

    def test_empty_grid_is_rejected(self):
        with self.assertRaises(SweepError):
            expand({}, {"grid": {"a.b": []}})


class EstimateTests(unittest.TestCase):
    def test_projects_candidates_and_flags_1bar_economics(self):
        payload = estimate(
            {
                "population_size": 40,
                "max_iterations": 3,
                "survival_count": 20,
                "target": {"horizon_bars": 1},
                "fitness": {"turnover_max": 0.0},
                "evaluation": {"gates": {"null_n_perm": 0}},
            },
            n_trials=100,
        )
        self.assertGreater(payload["candidates_est"], 0)
        self.assertLessEqual(payload["stage_b_est"], 32)
        joined = " ".join(payload["warnings"])
        self.assertIn("1-bar rebalancing", joined)
        self.assertIn("turnover_max", joined)
        self.assertIn("null_n_perm", joined)

    def test_funnel_cuts_the_expensive_stage(self):
        payload = estimate(
            {
                "population_size": 200,
                "max_iterations": 5,
                "fitness": {"prefilter_enabled": True, "prefilter_keep": 16},
                "evaluation": {"gates": {"null_n_perm": 60}},
            }
        )
        self.assertEqual(payload["stage_b_est"], 16)
        self.assertGreater(payload["candidates_est"], 16)


class _LibraryFixture:
    """Two factors with value snapshots in a throwaway library."""

    def __init__(self, root: Path):
        self.root = root
        (root / "metadata").mkdir(parents=True, exist_ok=True)
        (root / "values").mkdir(parents=True, exist_ok=True)
        index = pd.date_range("2024-01-01", periods=120, freq="D")
        columns = [f"A{i}" for i in range(6)]
        rng = np.random.default_rng(7)
        self.panels = {}
        base = rng.normal(size=(120, 6))
        for fid, signal, gap in (
            ("fac_a", base, 0.05),
            ("fac_b", base * 0.8 + rng.normal(scale=0.05, size=(120, 6)), 0.06),
            ("fac_c", rng.normal(size=(120, 6)), 0.20),
        ):
            panel = pd.DataFrame(signal, index=index, columns=columns)
            self.panels[fid] = panel
            meta = {
                "factor_id": fid,
                "miner_type": "GP",
                "lifecycle_status": "INSPECTED",
                "created_at": "2026-01-01T00:00:00",
                "logic_reference": {"type": "json_ast", "ast": _ast(window=20)},
                "metrics": {
                    "gate_pass": True,
                    "fitness_score": 10.0 if fid == "fac_a" else 2.0,
                    "RankIC": gap,
                    "dsr": 0.95,
                    "hac_t": 3.0,
                    "p_adj": 0.01,
                },
                "data_lineage": {"snapshot_rows": 720},
            }
            (root / "metadata" / f"{fid}.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
            long = (
                panel.rename_axis(index="timestamp", columns="asset")
                .stack()
                .rename("factor")
                .reset_index()
            )
            long["forward_return"] = np.nan
            long.to_parquet(root / "values" / f"{fid}.parquet", index=False)


class FactorDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "factor_db"
        _LibraryFixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_reports_library_contents(self):
        from core.commands.factor_db import list_library

        payload = list_library(str(self.root))
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["n_gate_pass"], 3)
        self.assertEqual(payload["n_with_values"], 3)
        self.assertTrue(payload["factors"][0]["expression"])

    def test_corr_flags_redundant_pair(self):
        from core.commands.factor_db import correlation_matrix

        payload = correlation_matrix(str(self.root), ["fac_a", "fac_b", "fac_c"])
        pairs = {(p["a"], p["b"]): p["corr"] for p in payload["redundant_pairs"]}
        self.assertIn(("fac_a", "fac_b"), pairs)
        self.assertGreater(abs(pairs[("fac_a", "fac_b")]), 0.9)

    def test_combine_equal_is_persisted(self):
        from core.commands.factor_db import build_combination, persist_combination

        combo = build_combination(str(self.root), ["fac_a", "fac_b"], method="equal")
        self.assertAlmostEqual(sum(combo["weights"]), 1.0, places=6)
        result = persist_combination(str(self.root), combo, apply=True)
        self.assertTrue(Path(result["path"]).is_file())
        self.assertTrue((self.root / "values" / f"{result['factor_id']}.parquet").is_file())

    def test_combine_caps_single_weight(self):
        from core.commands.factor_db import build_combination

        # Three members -> a 0.4 cap is feasible.
        combo = build_combination(
            str(self.root), ["fac_a", "fac_b", "fac_c"],
            method="equal", max_weight=0.4,
        )
        self.assertLessEqual(max(combo["weights"]), 0.4 + 1e-9)
        self.assertAlmostEqual(sum(combo["weights"]), 1.0, places=6)

    def test_infeasible_cap_is_relaxed_not_violated(self):
        from core.commands.factor_db import build_combination

        combo = build_combination(
            str(self.root), ["fac_a", "fac_b"], method="equal", max_weight=0.2
        )
        self.assertAlmostEqual(sum(combo["weights"]), 1.0, places=6)
        self.assertAlmostEqual(max(combo["weights"]), 0.5, places=6)

    def test_remove_is_a_soft_move(self):
        from core.commands.factor_db import delete_factor

        plan = delete_factor(str(self.root), "fac_c", apply=False)
        self.assertTrue(plan["dry_run"])
        self.assertTrue((self.root / "metadata" / "fac_c.json").exists())

        done = delete_factor(str(self.root), "fac_c", apply=True)
        self.assertFalse((self.root / "metadata" / "fac_c.json").exists())
        self.assertTrue(Path(done["moved"][0]).is_file())
        self.assertIn("_deleted_", done["bucket"])


class FactorsExpressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "factor_db"
        _LibraryFixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expression_is_rendered_from_the_ast(self):
        from core.commands.factors import collect

        payload = collect("run", db_root=str(self.root))
        for row in payload["factors"]:
            self.assertIn("ts_zscore", row["expression"])

    def test_compare_reports_transitions(self):
        from core.commands.factors import compare_runs

        # Same library under two run ids: nothing changes, but the shape must
        # be stable and the baseline/latest must be echoed back.
        with tempfile.TemporaryDirectory() as tmp:
            for run_id in ("runA", "runB"):
                run_db = Path(tmp) / run_id / "factor_db"
                _LibraryFixture(run_db)
            payload = compare_runs(
                ["runA", "runB"], user_dir=tmp, output_root=tmp
            )
        self.assertEqual(payload["per_run"]["runA"]["total"], 3)
        self.assertEqual(payload["transitions"]["baseline"], "runA")
        self.assertEqual(payload["transitions"]["newly_passing"], [])


class PromoteBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_root = Path(self.tmp.name) / "runs"
        self.src = self.run_root / "r1" / "factor_db"
        _LibraryFixture(self.src)
        self.dst = Path(self.tmp.name) / "prod"

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_inspected_publishes_every_gated_factor(self):
        import argparse

        from core.commands.promote import run_promote_cmd, select_inspected

        ids = select_inspected("r1", user_dir=self.tmp.name)
        self.assertEqual(sorted(ids), ["fac_a", "fac_b", "fac_c"])

        code = run_promote_cmd(
            argparse.Namespace(
                run_id="r1",
                factor_id=None,
                all_inspected=True,
                db_root=str(self.dst),
                user_dir=self.tmp.name,
                output_root=str(self.run_root),
                dry_run=False,
                force=False,
                json=True,
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(p.name for p in (self.dst / "metadata").glob("*.json")),
            ["fac_a.json", "fac_b.json", "fac_c.json"],
        )
        audit = self.run_root.parent / "user_workspace" / "reports" / "promotions.jsonl"
        # audit lives next to the user dir passed in; just assert at least one
        # promotion record exists somewhere under the temp tree
        found = list(Path(self.tmp.name).rglob("promotions.jsonl"))
        self.assertTrue(found, "promotion audit trail must be written")

    def test_promoted_library_is_what_backtest_discovers(self):
        """End-to-end link P0-2: run -> promote -> backtest's discovery path.

        The backtest auto-discovers factors from ``--db-root``. That path had
        never run against a non-empty library, so a promotion that produced an
        unreadable snapshot would only be discovered at backtest time.
        """
        import argparse

        from core.commands.backtest import _discover_factor_ids
        from core.commands.promote import run_promote_cmd

        run_promote_cmd(
            argparse.Namespace(
                run_id="r1", factor_id=None, all_inspected=True,
                db_root=str(self.dst), user_dir=self.tmp.name,
                output_root=str(self.run_root), dry_run=False,
                force=False, json=True,
            )
        )
        discovered = _discover_factor_ids(str(self.dst))
        self.assertEqual(discovered, ["fac_a", "fac_b", "fac_c"])

    def test_second_batch_does_not_overwrite(self):
        import argparse

        from core.commands.promote import run_promote_cmd

        def run_once():
            return run_promote_cmd(
                argparse.Namespace(
                    run_id="r1", factor_id=None, all_inspected=True,
                    db_root=str(self.dst), user_dir=self.tmp.name,
                    output_root=str(self.run_root), dry_run=False,
                    force=False, json=True,
                )
            )

        self.assertEqual(run_once(), 0)
        # Everything already exists -> every item fails, nothing changes.
        self.assertEqual(run_once(), 1)


class SeedingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "factor_db"
        _LibraryFixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_promoted_asts_become_seeds(self):
        seeds = load_seed_asts(str(self.root), top_k=10)
        self.assertEqual(len(seeds), 3)
        self.assertIn("op", seeds[0])

    def test_unpromoted_library_yields_nothing_by_default(self):
        for path in (self.root / "metadata").glob("*.json"):
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["metrics"]["gate_pass"] = False
            path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(load_seed_asts(str(self.root), top_k=10), [])

    def test_top_k_orders_by_fitness(self):
        seeds = load_seed_asts(str(self.root), top_k=1)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0], _ast(window=20))


class EvaluationCacheTests(unittest.TestCase):
    def test_hit_roundtrip_and_fingerprint_sensitivity(self):
        from core.evaluation.cache import (
            EvaluationCache,
            economics_fingerprint,
            panel_fingerprint,
        )

        index = pd.date_range("2024-01-01", periods=30)
        panel = pd.DataFrame(
            np.arange(90, dtype=float).reshape(30, 3), index=index,
            columns=["a", "b", "c"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = EvaluationCache(enabled=True, root=tmp)
            econ = economics_fingerprint({"fitness": {"gate_cost_rate": 0.0018}}, 5)
            fp = panel_fingerprint({"close": panel}, panel)
            key = cache.key_for({"op": "close"}, fp, econ)
            self.assertIsNone(cache.get(key))
            cache.put(key, {"fitness_score": 1.0})
            self.assertEqual(cache.get(key), {"fitness_score": 1.0})

            # Different economics -> different key (never a stale verdict).
            econ2 = economics_fingerprint({"fitness": {"gate_cost_rate": 0.0036}}, 5)
            self.assertNotEqual(cache.key_for({"op": "close"}, fp, econ2), key)

            # Different data -> different panel fingerprint.
            shifted = panel * 2.0
            self.assertNotEqual(
                panel_fingerprint({"close": shifted}, shifted), fp
            )


if __name__ == "__main__":
    unittest.main()
