"""Hypothesis-budget accounting.

The DSR benchmark is a function of how many ideas were tried, so the bar has to
be visible: at 787 trials over 610 observations it sits near 2.35 annualised,
which is why single-factor promotion stopped happening after the gate was
tightened. These tests pin the arithmetic and the reporting surface.
"""

import math
import os
import tempfile
import unittest

from core.evaluation.experiment_trace import FdrLedger
from core.evaluation.significance import multiple_testing_bar


class MultipleTestingBarTests(unittest.TestCase):
    def test_bar_rises_with_trials(self):
        small = multiple_testing_bar(n_obs=610, n_trials=10)
        large = multiple_testing_bar(n_obs=610, n_trials=787)
        self.assertLess(small["bar_per_period_sharpe"], large["bar_per_period_sharpe"])

    def test_bar_falls_with_more_observations(self):
        short = multiple_testing_bar(n_obs=250, n_trials=787)
        long = multiple_testing_bar(n_obs=1200, n_trials=787)
        self.assertGreater(short["bar_per_period_sharpe"], long["bar_per_period_sharpe"])

    def test_known_magnitude_for_the_holdout_run(self):
        # 787 trials x 610 obs was the P0 configuration; the annualised bar was
        # reported as ~2.35 in the gate audit.
        report = multiple_testing_bar(n_obs=610, n_trials=787)
        self.assertAlmostEqual(report["bar_annualised_sharpe"], 2.35, delta=0.05)

    def test_annualisation_is_applied(self):
        daily = multiple_testing_bar(n_obs=610, n_trials=100, annualisation=252)
        ratio = daily["bar_annualised_sharpe"] / daily["bar_per_period_sharpe"]
        self.assertAlmostEqual(ratio, math.sqrt(252), places=6)

    def test_degenerate_inputs(self):
        report = multiple_testing_bar(n_obs=1, n_trials=5)
        self.assertTrue(math.isnan(report["bar_per_period_sharpe"]))


class BudgetReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ledger.jsonl")
        self.ledger = FdrLedger(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_ledger(self):
        report = self.ledger.budget_report(n_obs=600, budget=1000)
        self.assertEqual(report["cumulative_tests"], 0)
        self.assertEqual(report["runs"], 0)
        self.assertEqual(report["utilization"], 0.0)

    def test_accumulates_across_runs(self):
        self.ledger.append([0.01, 0.02, 0.03], run_id="run-a")
        self.ledger.append([0.04, 0.05], run_id="run-b")
        report = self.ledger.budget_report(n_obs=600, budget=10)
        self.assertEqual(report["cumulative_tests"], 5)
        self.assertEqual(report["runs"], 2)
        self.assertAlmostEqual(report["utilization"], 0.5)

    def test_utilization_is_none_without_a_budget(self):
        self.ledger.append([0.01], run_id="run-a")
        self.assertIsNone(self.ledger.budget_report()["utilization"])

    def test_bar_grows_as_the_ledger_grows(self):
        self.ledger.append([0.5] * 5, run_id="small")
        before = self.ledger.budget_report(n_obs=600)["bar_per_period_sharpe"]
        self.ledger.append([0.5] * 500, run_id="large")
        after = self.ledger.budget_report(n_obs=600)["bar_per_period_sharpe"]
        self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main()
