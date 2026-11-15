"""P2: the statistical capabilities that existed but were never wired in."""

from __future__ import annotations

import asyncio
import unittest

import numpy as np

from core.evaluation.significance import (
    apply_promotion_gates,
    empirical_p,
    promotion_gates,
)
from core.experiment import budget


def strong_metrics():
    return [{
        "hac_t": 3.5, "p_value": 0.001, "dsr": 0.99,
        "oos_consistent": True, "cost_adj_return": 0.05,
        "ls_t": 2.0, "RankIC_oos": 0.05, "oos_enabled": True,
    }]


class TestEmpiricalNullGate(unittest.TestCase):
    def test_disabled_by_default(self):
        out = apply_promotion_gates(strong_metrics())
        self.assertTrue(out[0]["gate"]["pass"])
        self.assertIsNone(out[0]["empirical_p"])
        self.assertTrue(out[0]["gate"]["empirical_null_pass"])

    def test_null_without_threshold_is_silent(self):
        out = apply_promotion_gates(
            strong_metrics(), null_tstats=[np.linspace(0, 10, 100)]
        )
        self.assertTrue(out[0]["gate"]["empirical_null_pass"])
        self.assertTrue(out[0]["gate"]["pass"])
        self.assertGreater(out[0]["empirical_p"], 0.3)

    def test_lucky_factor_blocked_by_null(self):
        # |t|=3.5 is "significant" analytically, but 65% of permuted targets
        # reach the same |t| for this factor shape -> pure luck.
        out = apply_promotion_gates(
            strong_metrics(),
            cfg={"empirical_p_max": 0.05},
            null_tstats=[np.linspace(0, 10, 100)],
        )
        self.assertFalse(out[0]["gate"]["empirical_null_pass"])
        self.assertEqual(out[0]["status"], "DISCOVERED_UNVERIFIED")

    def test_tail_factor_passes(self):
        out = apply_promotion_gates(
            strong_metrics(),
            cfg={"empirical_p_max": 0.05},
            null_tstats=[np.full(100, 0.1)],
        )
        self.assertEqual(out[0]["empirical_p"], 0.0)
        self.assertTrue(out[0]["gate"]["empirical_null_pass"])
        self.assertEqual(out[0]["status"], "INSPECTED")

    def test_missing_samples_leave_gate_silent(self):
        out = apply_promotion_gates(
            strong_metrics(), cfg={"empirical_p_max": 0.05}, null_tstats=[[]]
        )
        self.assertIsNone(out[0]["empirical_p"])
        self.assertTrue(out[0]["gate"]["empirical_null_pass"])

    def test_empirical_p_is_one_sided(self):
        self.assertAlmostEqual(empirical_p(2.5, [1.0, 2.0, 3.0, 4.0]), 0.5)

    def test_promotion_gates_reports_new_key(self):
        g = promotion_gates({**strong_metrics()[0], "empirical_p": 0.9},
                            {"empirical_p_max": 0.05})
        self.assertFalse(g["empirical_null_pass"])


class TestPortableBudget(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = self._tmp.name + "/budget.json"
        self.addCleanup(self._tmp.cleanup)

    def test_fresh_clone_inherits_denominator_and_family(self):
        budget.sync(787, [0.001] * 10, path=self.path, increment_runs=True)
        base, family, snap = budget.resolve(0, [], [0.02], path=self.path)
        self.assertEqual(base, 787)
        self.assertEqual(len(family), 11)
        self.assertEqual(snap["runs"], 1)

    def test_sync_is_monotonic(self):
        budget.sync(787, [0.01], path=self.path)
        budget.sync(3, [], path=self.path)
        self.assertEqual(budget.load(self.path)["n_trials"], 787)

    def test_local_trace_wins_when_ahead(self):
        budget.sync(50, [], path=self.path)
        base, _, _ = budget.resolve(120, [0.4], [0.3], path=self.path)
        self.assertEqual(base, 120)

    def test_snapshot_pvalues_are_unioned_with_local_ledger(self):
        # A4: the old `or` discarded the whole historical snapshot whenever a
        # local ledger existed. The BH family must be the UNION of all sources.
        budget.sync(10, [0.9] * 5, path=self.path)
        _, family, _ = budget.resolve(10, [0.4], [0.3], path=self.path)
        self.assertEqual(family[0], 0.4)
        self.assertIn(0.9, family)
        self.assertIn(0.3, family)
        self.assertEqual(len(family), 7)
        _, family2, _ = budget.resolve(10, [], [0.3], path=self.path)
        self.assertEqual(len(family2), 6)

    def test_corrupt_file_is_flagged_and_sync_refuses_to_reset(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        loaded = budget.load(self.path)
        self.assertEqual(loaded["n_trials"], 0)
        self.assertTrue(loaded["corrupt"])
        with self.assertRaises(budget.BudgetSnapshotError):
            budget.sync(1, [0.5], path=self.path)

    def test_status_reports_path(self):
        s = budget.status(self.path)
        self.assertIn("n_trials", s)
        self.assertTrue(s["snapshot_path"].endswith("budget.json"))


class TestHoldoutProductEndpoint(unittest.TestCase):
    def test_endpoint_exposes_revealed_windows_and_mode(self):
        from api.main import get_holdout_status

        result = asyncio.run(get_holdout_status())
        self.assertIn("mode", result)
        self.assertIn("revealed", result)
        self.assertIn("n_revealed", result)
        self.assertIn("pre_registered_window", result)
        self.assertIsInstance(result["revealed"], list)

    def test_budget_endpoint_includes_portable_snapshot(self):
        from api.main import get_hypothesis_budget

        result = asyncio.run(get_hypothesis_budget())
        self.assertIn("portable_snapshot", result)


if __name__ == "__main__":
    unittest.main()
