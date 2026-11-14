"""P7-1: positive-control calibration of the promotion gate.

These tests exist because the project had plenty of negative evidence
("0 promoted") but no proof that the gate would ever say yes. A gate that
rejects everything is perfectly safe and completely useless.
"""

from __future__ import annotations

import unittest

import numpy as np

from core.evaluation.significance import ic_series, multiple_testing_bar
from core.experiment.gate_calibration import (
    calibrate,
    candidate_metrics,
    evaluate_gate,
    render_report,
    synthetic_panels,
)


class TestControlledDGP(unittest.TestCase):
    def test_measured_ic_tracks_injected_signal(self):
        measured = []
        for rho in (0.0, 0.10, 0.25):
            panels = synthetic_panels(rho, n_bars=240, n_oos_bars=90, seed=7)
            measured.append(float(np.mean(ic_series(
                panels["factor_in"], panels["returns_in"]
            ))))
        self.assertLess(abs(measured[0]), 0.03)
        self.assertLess(measured[0], measured[1])
        self.assertLess(measured[1], measured[2])
        self.assertGreater(measured[2], 0.15)

    def test_in_sample_and_oos_shapes(self):
        panels = synthetic_panels(0.08, n_bars=200, n_oos_bars=70, n_assets=12, seed=3)
        self.assertEqual(panels["factor_in"].shape, (200, 12))
        self.assertEqual(panels["returns_oos"].shape, (70, 12))
        for key, arr in panels.items():
            self.assertTrue(np.isfinite(arr).all(), key)

    def test_same_seed_is_deterministic(self):
        a = synthetic_panels(0.12, n_bars=120, n_oos_bars=60, seed=42)
        b = synthetic_panels(0.12, n_bars=120, n_oos_bars=60, seed=42)
        np.testing.assert_array_equal(a["returns_in"], b["returns_in"])


class TestCandidateMetrics(unittest.TestCase):
    def test_null_signal_produces_negative_net_return(self):
        panels = synthetic_panels(0.0, n_bars=300, n_oos_bars=100, seed=5)
        metrics, null = candidate_metrics(panels, n_perm=10, seed=5)
        self.assertLess(metrics["cost_adj_return"], 0.0)
        self.assertLess(metrics["sr"], 0.0)
        self.assertTrue(metrics["oos_enabled"])
        self.assertEqual(len(null), 10)

    def test_strong_signal_metrics_are_positive(self):
        panels = synthetic_panels(0.20, n_bars=300, n_oos_bars=100, seed=5)
        metrics, null = candidate_metrics(panels, n_perm=10, seed=5)
        self.assertGreater(metrics["hac_t"], 4.0)
        self.assertGreater(metrics["cost_adj_return"], 0.0)
        self.assertGreater(metrics["RankIC_oos"], 0.02)
        self.assertTrue(metrics["oos_consistent"])

    def test_base_dsr_is_not_a_placeholder_zero(self):
        """n_trials=1 must not inherit a hand-written dsr=0.0."""
        panels = synthetic_panels(0.20, n_bars=300, n_oos_bars=100, seed=11)
        metrics, _ = candidate_metrics(panels, n_perm=8, seed=11)
        result = evaluate_gate(metrics, [], n_trials=1, seed=11)
        self.assertGreater(result.dsr, 0.0)
        self.assertTrue(result.passed)


class TestGateDiscrimination(unittest.TestCase):
    def test_empirical_null_separates_luck_from_signal(self):
        weak, null_w = candidate_metrics(
            synthetic_panels(0.0, n_bars=300, n_oos_bars=100, seed=9),
            n_perm=15, seed=9,
        )
        strong, null_s = candidate_metrics(
            synthetic_panels(0.20, n_bars=300, n_oos_bars=100, seed=9),
            n_perm=15, seed=9,
        )
        rw = evaluate_gate(weak, null_w, n_trials=20, seed=9)
        rs = evaluate_gate(strong, null_s, n_trials=20, seed=9)
        self.assertGreater(rw.empirical_p, 0.10)
        self.assertLess(rs.empirical_p, 0.10)
        self.assertFalse(rw.passed)
        self.assertTrue(rs.passed)

    def test_more_trials_make_the_gate_harder(self):
        bars = [multiple_testing_bar(641, n)["bar_per_period_sharpe"]
                for n in (1, 20, 182, 787)]
        self.assertEqual(bars, sorted(bars))
        self.assertGreater(bars[-1], bars[0])

    def test_gate_checks_cover_every_expected_key(self):
        metrics, null = candidate_metrics(
            synthetic_panels(0.15, n_bars=240, n_oos_bars=90, seed=2),
            n_perm=8, seed=2,
        )
        r = evaluate_gate(metrics, null, n_trials=100, seed=2)
        for key in ("hac_t_significant", "fdr_pass", "dsr_pass",
                    "cost_adj_positive", "empirical_null_pass"):
            self.assertIn(key, r.gate_checks)


class TestCalibrationReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = calibrate(
            (0.0, 0.20), (1, 20),
            n_repeats=2, n_bars=180, n_assets=12, n_oos_bars=80,
            n_perm=8, seed=4,
        )

    def test_grid_is_complete(self):
        self.assertEqual(len(self.result["grid"]), 4)
        self.assertIn("0.0", self.result["per_signal"])
        self.assertIn("1", self.result["mde"])

    def test_false_positive_rate_is_low(self):
        self.assertIsNotNone(self.result["false_positive_rate"])
        self.assertLessEqual(self.result["false_positive_rate"], 0.25)

    def test_strong_signal_is_detected_more_often_than_null(self):
        lookup = {(g["signal"], g["n_trials"]): g["detection_rate"]
                  for g in self.result["grid"]}
        self.assertGreaterEqual(lookup[(0.20, 1)], lookup[(0.0, 1)])

    def test_report_states_its_own_limits(self):
        text = render_report(self.result)
        self.assertIn("不是「挖到了 alpha」", text)
        self.assertIn("不触碰", text)
        self.assertIn("检出率矩阵", text)
        self.assertIn("最小可检出效应", text)
        self.assertIn("谁是瓶颈", text)


if __name__ == "__main__":
    unittest.main()
