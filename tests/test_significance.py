import numpy as np
import unittest

from core.evaluation.significance import (
    bh_fdr,
    holm,
    ic_series,
    newey_west_tstat,
    tstat_ic,
    target_permutation_tstats,
    random_formula_null,
    empirical_p,
    pvalue_from_t,
    expected_max_sr,
    deflated_sharpe_ratio,
    promotion_gates,
)


class CorrectionTest(unittest.TestCase):
    def test_bh_fdr_monotone_and_bounds(self):
        p = [0.01, 0.04, 0.03, 0.20, 0.0]
        adj = bh_fdr(p)
        self.assertTrue(np.all(adj >= 0) and np.all(adj <= 1))
        # monotone non-decreasing when read along sorted input
        self.assertTrue(np.all(np.diff(adj[np.argsort(p)]) >= 0))

    def test_holm_more_conservative_than_bh(self):
        p = [0.01, 0.04, 0.03, 0.20, 0.10]
        self.assertTrue(np.all(holm(p) >= bh_fdr(p) - 1e-12))

    def test_bh_controls_fdr_under_null(self):
        # 200 independent uniforms (null); BH should keep family-wise false-
        # positive rate well under 5% at the 0.05 threshold.
        rng = np.random.default_rng(0)
        pvals = rng.uniform(0, 1, size=200)
        adj = bh_fdr(pvals)
        rejected = np.sum(adj <= 0.05)
        # expected ~10 false rejections by chance; allow generous slack
        self.assertLess(rejected, 30)

    def test_holm_no_false_positives_easier(self):
        rng = np.random.default_rng(1)
        pvals = rng.uniform(0.2, 1.0, size=100)  # all null
        adj = holm(pvals)
        self.assertEqual(np.sum(adj <= 0.05), 0)


class NeweyWestTest(unittest.TestCase):
    def test_tstat_near_zero_for_noise(self):
        rng = np.random.default_rng(2)
        x = rng.standard_normal(300)
        t = newey_west_tstat(x)
        self.assertLess(abs(t), 3.0)  # should not be "significant"

    def test_tstat_large_for_strong_signal(self):
        rng = np.random.default_rng(3)
        # persistent IC with autocorrelation AND a non-zero mean (so mean IC != 0)
        sig = np.zeros(300)
        for i in range(1, 300):
            sig[i] = 0.8 * sig[i - 1] + 0.5 + 0.3 * rng.standard_normal()
        t = newey_west_tstat(sig)
        self.assertGreater(abs(t), 3.0)

    def test_tstat_ic_panel(self):
        rng = np.random.default_rng(4)
        T, N = 200, 30
        f = rng.standard_normal((T, N))
        # returns weakly correlated with factor (signal), plus noise
        r = 0.3 * f + 0.9 * rng.standard_normal((T, N))
        t = tstat_ic(f, r)
        self.assertGreater(t, 1.5)  # signal should yield a detectable t


class NullTest(unittest.TestCase):
    def test_permutation_null_centered(self):
        rng = np.random.default_rng(5)
        T, N = 150, 25
        f = rng.standard_normal((T, N))
        r = rng.standard_normal((T, N))  # pure noise linkage
        null = target_permutation_tstats(f, r, n_perm=50, seed=5)
        # under the null, |t| should mostly sit below the 95% gaussian ~1.96
        self.assertLess(np.percentile(null, 95), 3.5)

    def test_random_formula_null_runs(self):
        rng = np.random.default_rng(6)
        T, N = 120, 20
        r = rng.standard_normal((T, N))

        def sampler():
            return ("expr", rng.integers(0, 1000))

        def evaluator(expr):
            return rng.standard_normal((T, N))

        null = random_formula_null(20, sampler, evaluator, r, seed=6)
        self.assertEqual(null.shape, (20,))
        self.assertTrue(np.all(np.isfinite(null)))

    def test_empirical_p(self):
        # Continuity-corrected (Phipson-Smyth): p=(count+1)/(n+1), never 0.0.
        null = np.linspace(0, 5, 101)  # n=101; 2.5..5 inclusive = 51 samples
        self.assertAlmostEqual(empirical_p(2.5, null), 52.0 / 102.0, places=6)
        # Exceeding every null sample must floor at 1/(n+1), NOT 0.0 (E-04).
        self.assertAlmostEqual(empirical_p(10.0, null), 1.0 / 102.0, places=6)
        self.assertGreater(empirical_p(10.0, null), 0.0)

    def test_empirical_p_never_zero_and_monotone(self):
        rng = np.random.default_rng(11)
        null = rng.standard_normal(100)
        vals = [100.0, np.max(null), np.median(null), np.min(null) - 5.0]
        ps = [empirical_p(v, null) for v in vals]
        self.assertTrue(all(p > 0.0 for p in ps), ps)
        self.assertTrue(all(p <= 1.0 for p in ps), ps)
        # stricter evidence (larger value) yields a SMALLER p-value
        self.assertLessEqual(ps[0], ps[1])
        self.assertLessEqual(ps[1], ps[2])
        self.assertLessEqual(ps[2], ps[3])

    def test_empirical_p_nonfinite_returns_one(self):
        # A NaN/Inf statistic cannot reject the null; returning a spurious low
        # p-value would let a hac_t=NaN candidate falsely pass the empirical-null
        # gate (review F5/C1). It must return 1.0 instead.
        null = np.linspace(0, 5, 101)
        self.assertEqual(empirical_p(float("nan"), null), 1.0)
        self.assertEqual(empirical_p(float("inf"), null), 1.0)
        # empty null also yields the "cannot reject" answer
        self.assertEqual(empirical_p(1.0, []), 1.0)

    def test_empirical_null_gate_rejects_nan_hac_t(self):
        # When the empirical-null gate is active and the candidate's hac_t is NaN,
        # the gate must NOT pass. This is the exact bug from review F5/C1.
        cfg = {"empirical_p_max": 0.05, "null_n_perm": 100}
        metrics = {
            "hac_t": float("nan"),
            "p_adj": 1.0,
            "dsr": 0.0,
            "oos_consistent": False,
            "cost_adj_return": 0.0,
            "ls_t": 0.0,
            "RankIC_oos": 0.0,
            "oos_enabled": False,
            "empirical_p": empirical_p(float("nan"),
                                      np.linspace(0, 3, 101)),
        }
        gate = promotion_gates(metrics, cfg)
        self.assertFalse(gate["empirical_null_pass"])
        self.assertFalse(gate["pass"])


class PValueFromTTest(unittest.TestCase):
    """E-03: HAC t must use the Student-t tail, not N(0,1)."""

    def test_student_t_tail_is_fatter_than_normal(self):
        t = 2.4
        p_norm = pvalue_from_t(t)                 # legacy normal tail
        p_t59 = pvalue_from_t(t, df=59)           # finite-sample reference
        self.assertGreater(p_t59, p_norm)         # t tail is more conservative
        # magnitude sanity: N(0,1) gives ~0.0164 at t=2.4; t(59) ~0.0197
        self.assertLess(p_t59, p_norm * 1.5)

    def test_large_df_converges_to_normal(self):
        t = 3.0
        p_norm = pvalue_from_t(t)
        p_big = pvalue_from_t(t, df=100000)
        self.assertAlmostEqual(p_big, p_norm, places=4)

    def test_small_df_is_clearly_looser(self):
        # df=5 is a very heavy tail: p should be markedly larger than normal.
        self.assertGreater(pvalue_from_t(2.0, df=5), pvalue_from_t(2.0) * 1.3)

    def test_nonfinite_and_tiny_df_are_safe(self):
        self.assertEqual(pvalue_from_t(float("nan"), df=30), 1.0)
        self.assertEqual(pvalue_from_t(2.0, df=0), pvalue_from_t(2.0))  # fallback
        self.assertLessEqual(pvalue_from_t(5.0, df=3), 1.0)


class DSRTest(unittest.TestCase):
    def test_expected_max_sr_grows_with_trials(self):
        self.assertLess(expected_max_sr(1), expected_max_sr(1000))

    def test_dsr_high_for_strong_sharpe(self):
        dsr = deflated_sharpe_ratio(sharpe=2.5, n_obs=250, n_trials=1)
        self.assertGreater(dsr, 0.95)

    def test_dsr_low_for_noise_sharpe_many_trials(self):
        # a mediocre SR with many trials gets penalised by the benchmark
        dsr = deflated_sharpe_ratio(sharpe=0.5, n_obs=120, n_trials=5000)
        self.assertLess(dsr, 0.95)

    def test_dsr_insufficient_obs(self):
        self.assertEqual(deflated_sharpe_ratio(2.0, n_obs=1), 0.0)

    def test_dsr_benchmark_scaled_to_per_period_units(self):
        # A daily IC IR of 0.45 over 240 obs is a ~7-sigma signal and should
        # survive a 1200-trial search; IR 0.05 is noise and should not. This
        # guards the unit bug where the benchmark stayed at sqrt(2 ln N) while
        # `sharpe` was a per-period IC ratio.
        strong = deflated_sharpe_ratio(sharpe=0.45, n_obs=240, n_trials=1200)
        noise = deflated_sharpe_ratio(sharpe=0.05, n_obs=240, n_trials=1200)
        self.assertGreater(strong, 0.95)
        self.assertLess(noise, 0.5)


class PromotionGateTest(unittest.TestCase):
    def test_all_green_passes(self):
        m = {
            "hac_t": 3.1, "p_adj": 0.01, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
        }
        g = promotion_gates(m)
        self.assertTrue(g["pass"])

    def test_failed_fdr_blocks(self):
        m = {
            "hac_t": 3.1, "p_adj": 0.2, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
        }
        g = promotion_gates(m)
        self.assertFalse(g["pass"])
        self.assertFalse(g["fdr_pass"])

    def test_oos_inconsistent_blocks(self):
        m = {
            "hac_t": 3.1, "p_adj": 0.01, "dsr": 0.97,
            "oos_consistent": False, "cost_adj_return": 0.05,
        }
        g = promotion_gates(m)
        self.assertFalse(g["pass"])
        self.assertFalse(g["oos_consistent"])

    def test_oos_strength_silent_without_holdout(self):
        # Backward-compatible: when no test_period was held out (oos_enabled
        # False), the OOS-strength gate must NOT block an otherwise-green factor.
        m = {
            "hac_t": 3.1, "p_adj": 0.01, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
            "RankIC_oos": 0.0, "oos_enabled": False,
        }
        g = promotion_gates(m)
        self.assertTrue(g["pass"])
        self.assertTrue(g["oos_strength"])

    def test_oos_strength_blocks_collapsed_oos(self):
        # When a test_period WAS held out, a factor whose OOS |RankIC| collapsed
        # below the floor must be blocked even if everything else is green.
        m = {
            "hac_t": 3.1, "p_adj": 0.01, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
            "RankIC_oos": 0.003, "oos_enabled": True,
        }
        g = promotion_gates(m)
        self.assertFalse(g["pass"])
        self.assertFalse(g["oos_strength"])

    def test_oos_strength_passes_real_oos(self):
        m = {
            "hac_t": 3.1, "p_adj": 0.01, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
            "RankIC_oos": 0.05, "oos_enabled": True,
        }
        g = promotion_gates(m)
        self.assertTrue(g["pass"])
        self.assertTrue(g["oos_strength"])


if __name__ == "__main__":
    unittest.main()
