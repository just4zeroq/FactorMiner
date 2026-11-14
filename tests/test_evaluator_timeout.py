"""Per-candidate evaluation budget.

A single pathological factor must not stall an entire mining round. The
evaluator waits at most ``evaluation.timeout_seconds`` per candidate (times the
batch size) and records the stragglers as ``timeout`` instead of hanging.

Note: a healthy evaluation costs ~1.3s on first call (statistics warm-up), so
the budgets below are deliberately generous — they test the mechanism, not the
latency.
"""

import time
import unittest

import numpy as np
import pandas as pd

from core.evaluation.evaluator import ParallelEvaluator

_INDEX = pd.date_range("2025-01-01", periods=40, freq="D")


class _Factor:
    def __init__(self, name, delay=0.0, fail=False):
        self.name = name
        self.delay = delay
        self.fail = fail
        self.metrics = {}

    def compute(self, data):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return pd.Series(np.linspace(0, 1, 40), index=_INDEX)


def _returns() -> pd.Series:
    return pd.Series(np.linspace(1, 0, 40), index=_INDEX)


def _evaluator(timeout: float, workers: int = 2) -> ParallelEvaluator:
    return ParallelEvaluator(
        None,
        {
            "evaluation": {"timeout_seconds": timeout, "max_workers": workers},
            "fitness": {},
        },
    )


class EvaluationTimeoutTests(unittest.TestCase):
    def test_straggler_is_reported_as_timeout(self):
        fast = _Factor("fast")
        slow = _Factor("slow", delay=6.0)
        evaluator = _evaluator(timeout=2.5)  # budget = 5s over two candidates

        started = time.monotonic()
        feedback = evaluator.evaluate_on([fast, slow], None, _returns())
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.9, "the round must not wait for the slow candidate")
        statuses = {res.candidate.name: res.status for res in feedback.results}
        self.assertEqual(statuses["fast"], "success")
        self.assertEqual(statuses["slow"], "timeout")
        self.assertEqual(evaluator.timed_out, 1)
        self.assertIn("budget", feedback.results[1].error_message)

    def test_timeout_results_keep_candidate_order(self):
        candidates = [_Factor("a"), _Factor("slow", delay=5.0), _Factor("c")]
        feedback = _evaluator(timeout=1.5).evaluate_on(candidates, None, _returns())
        self.assertEqual([res.candidate.name for res in feedback.results], ["a", "slow", "c"])

    def test_zero_timeout_waits_forever(self):
        evaluator = _evaluator(timeout=0)
        feedback = evaluator.evaluate_on([_Factor("fast")], None, _returns())
        self.assertEqual(feedback.results[0].status, "success")
        self.assertEqual(evaluator.timed_out, 0)

    def test_failures_still_flow_through(self):
        feedback = _evaluator(timeout=30).evaluate_on(
            [_Factor("bad", fail=True)], None, _returns()
        )
        self.assertEqual(feedback.results[0].status, "error")
        self.assertEqual(feedback.results[0].error_type, "RuntimeError")


if __name__ == "__main__":
    unittest.main()
