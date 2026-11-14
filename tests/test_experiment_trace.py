import os
import tempfile
import unittest

from core.evaluation.experiment_trace import (
    ExperimentTrace,
    HoldoutLedger,
    HoldoutReuseError,
    TrialRecord,
    signature_of,
)


class HoldoutLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = HoldoutLedger(os.path.join(self.tmp, "ledger.json"))

    def test_reveal_once_ok(self):
        self.ledger.reveal("2025H1", "2025-01-01", "2025-06-30", note="first")
        self.assertTrue(self.ledger.is_revealed("2025H1"))
        self.assertEqual(len(self.ledger.snapshot()), 1)

    def test_re_reveal_same_period_raises(self):
        self.ledger.reveal("2025H1", "2025-01-01", "2025-06-30")
        with self.assertRaises(HoldoutReuseError):
            self.ledger.reveal("2025H1", "2025-01-01", "2025-06-30")

    def test_overlapping_reveal_raises(self):
        self.ledger.reveal("A", "2025-01-01", "2025-06-30")
        # adjacent (no overlap) is fine
        self.ledger.reveal("B", "2025-07-01", "2025-12-31")
        # partial overlap must fail
        with self.assertRaises(HoldoutReuseError):
            self.ledger.reveal("C", "2025-06-01", "2025-08-31")

    def test_persistence_enforced_across_instances(self):
        self.ledger.reveal("2025H1", "2025-01-01", "2025-06-30")
        # new instance reads the same file -> still enforced
        ledger2 = HoldoutLedger(os.path.join(self.tmp, "ledger.json"))
        self.assertTrue(ledger2.is_revealed("2025H1"))
        with self.assertRaises(HoldoutReuseError):
            ledger2.reveal("2025H1", "2025-03-01", "2025-09-01")

    def test_force_allows_re_reveal(self):
        self.ledger.reveal("X", "2025-01-01", "2025-06-30")
        self.ledger.reveal("X", "2025-01-01", "2025-06-30", force=True)
        self.assertEqual(len(self.ledger.snapshot()), 2)


class ExperimentTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.trace = ExperimentTrace(os.path.join(self.tmp, "trace.jsonl"))

    def test_num_trials_counts_distinct(self):
        for i in range(5):
            self.trace.record(TrialRecord(signature=f"sig{i}", fitness=float(i)))
        self.assertEqual(self.trace.num_trials, 5)

    def test_duplicate_signature_updates_not_double_counts(self):
        self.trace.record(TrialRecord(signature="dup", fitness=1.0))
        self.trace.record(TrialRecord(signature="dup", fitness=9.0))
        self.assertEqual(self.trace.num_trials, 1)
        self.assertAlmostEqual(self.trace.all()[0].fitness, 9.0)

    def test_has_seen(self):
        self.trace.record(TrialRecord(signature="seen1"))
        self.assertTrue(self.trace.has_seen("seen1"))
        self.assertFalse(self.trace.has_seen("other"))

    def test_significant_rate(self):
        recs = [
            TrialRecord(signature=f"s{i}", significant=(i < 2),
                        meta={"p_adj": 0.01 if i < 2 else 0.3})
            for i in range(10)
        ]
        for r in recs:
            self.trace.record(r)
        # 2 significant out of 10
        self.assertEqual(self.trace.num_significant(0.05), 2)
        self.assertAlmostEqual(self.trace.significant_rate(0.05), 0.2)

    def test_persistence(self):
        self.trace.record(TrialRecord(signature="p1", fitness=3.0))
        trace2 = ExperimentTrace(os.path.join(self.tmp, "trace.jsonl"))
        self.assertEqual(trace2.num_trials, 1)
        self.assertAlmostEqual(trace2.all()[0].fitness, 3.0)

    def test_signature_of_stable(self):
        a = {"op": "ts_zscore", "left": "close", "window": 10}
        b = {"op": "ts_zscore", "left": "close", "window": 10}
        self.assertEqual(signature_of(a), signature_of(b))
        c = {"op": "ts_zscore", "left": "close", "window": 20}
        self.assertNotEqual(signature_of(a), signature_of(c))


if __name__ == "__main__":
    unittest.main()
