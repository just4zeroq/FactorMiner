"""Unit tests for the static AlphaPool (Phase 2)."""

import math
import unittest

import numpy as np
import pandas as pd

from core.miner.pool import AlphaPool, long_to_wide, _safe_pearson


def _make_data(seed=0, T=300, N=6):
    rng = np.random.default_rng(seed)
    signal1 = rng.standard_normal((T, N))
    signal2 = rng.standard_normal((T, N))
    # Returns driven by BOTH latent signals -> A and C each have predictive IC,
    # and they are mutually independent.
    returns = 0.7 * signal1 + 0.3 * signal2 + 0.4 * rng.standard_normal((T, N))
    # A: signal1 + moderate noise
    factorA = signal1 + 0.6 * rng.standard_normal((T, N))
    # B: near-clone of A (A plus a tiny perturbation) -> redundant to A
    factorB = factorA + 0.01 * rng.standard_normal((T, N))
    # C: signal2 + moderate noise
    factorC = signal2 + 0.6 * rng.standard_normal((T, N))
    # D: near-clone of C -> redundant to C
    factorD = factorC + 0.01 * rng.standard_normal((T, N))
    return returns, factorA, factorB, factorC, factorD


class AlphaPoolCoreTests(unittest.TestCase):
    def setUp(self):
        self.returns, self.A, self.B, self.C, self.D = _make_data()

    def _fresh(self):
        p = AlphaPool(capacity=10, mutual_ic_max=0.99, weight_fit_shift=0,
                      reg_lambda=1.0, min_obs=50)
        p.set_target(self.returns)
        return p

    def test_rejects_redundant_clone(self):
        p = self._fresh()
        ok_a, _ = p.admit("A", self.A)
        self.assertTrue(ok_a)
        mic_b = p.mutual_ic(self.B)
        self.assertGreater(mic_b, 0.99, "clone B should be ~perfectly redundant")
        ok_b, reason_b = p.admit("B", self.B)
        self.assertFalse(ok_b)
        self.assertIn("redundant", reason_b)

    def test_admits_independent_factor(self):
        p = self._fresh()
        p.admit("A", self.A)
        mic_c = p.mutual_ic(self.C)
        self.assertLess(mic_c, 0.99, "independent factor C should not be redundant")
        ok_c, _ = p.admit("C", self.C)
        self.assertTrue(ok_c)

    def test_dedup_then_pooled_ic_improves(self):
        p = self._fresh()
        p.admit("A", self.A)
        p.admit("B", self.B)  # rejected
        p.admit("C", self.C)
        p.admit("D", self.D)  # rejected
        self.assertEqual(p._ids, ["A", "C"])
        self.assertEqual(p.report()["size"], 2)
        pooled = p.pooled_rankic()
        single_a = p.factor_rankic(self.A)
        single_c = p.factor_rankic(self.C)
        # Combining two independent signals should not be worse than either alone.
        self.assertGreaterEqual(pooled, min(single_a, single_c) - 1e-9)
        self.assertGreater(pooled, 0.0)

    def test_weights_normalized_l1(self):
        p = self._fresh()
        p.admit("A", self.A)
        p.admit("C", self.C)
        self.assertAlmostEqual(sum(abs(w) for w in p._weights), 1.0, places=6)

    def test_capacity_replacement(self):
        p = AlphaPool(capacity=1, mutual_ic_max=0.99, weight_fit_shift=0,
                      reg_lambda=1.0, min_obs=50)
        p.set_target(self.returns)
        ok_a, _ = p.admit("A", self.A)
        self.assertTrue(ok_a)
        # Pool full: admitting C triggers a replace-or-reject decision.
        ok_c, reason_c = p.admit("C", self.C)
        self.assertEqual(p.report()["size"], 1)
        self.assertIn(reason_c, ("replaced A", "no_improvement"))
        if ok_c:
            self.assertEqual(p._ids, ["C"])

    def test_no_target_guard(self):
        p = AlphaPool(capacity=5)
        ok, reason = p.admit("A", self.A)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_target")

    def test_long_to_wide(self):
        rng = np.random.default_rng(1)
        long_df = pd_dummy(rng)
        wide = long_to_wide(long_df)
        self.assertEqual(wide.shape, (3, 2))
        self.assertAlmostEqual(float(wide.iloc[0, 0]), 1.0)


def pd_dummy(rng):
    import pandas as pd
    return pd.DataFrame({
        "timestamp": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03"],
        "asset": ["X", "Y", "X", "Y", "X", "Y"],
        "factor": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    })


class HelpersTests(unittest.TestCase):
    def test_safe_pearson_perfect(self):
        a = np.arange(10.0)
        self.assertAlmostEqual(_safe_pearson(a, a), 1.0, places=6)

    def test_safe_pearson_short(self):
        self.assertIsNone(_safe_pearson(np.array([1.0, 2.0]), np.array([1.0, 2.0])))


class AlphaPoolFinalizeIntegrationTests(unittest.TestCase):
    """Exercise BaseFactorMiner._finalize with finalize_method='alphapool'."""

    def _make_miner(self, finalize_method):
        from core.miner.paradigms.base import BaseFactorMiner
        from core.miner.entities import EvaluationFeedback, CandidateEvaluation

        rng = np.random.default_rng(3)
        T, N = 200, 5
        data = pd.DataFrame(
            rng.standard_normal((T, N)),
            index=pd.date_range("2024-01-01", periods=T, freq="D"),
            columns=[f"A{i}" for i in range(N)],
        )
        returns = pd.DataFrame(
            0.6 * rng.standard_normal((T, N)),
            index=data.index, columns=data.columns,
        )

        class _FakeEval:
            def evaluate(self, cands):
                results = [
                    CandidateEvaluation(candidate=c, status="success",
                                       metrics={"RankIC": 0.05, "fitness_score": 1.0,
                                                "RankIC_oos": 0.04, "oos_enabled": True})
                    for c in cands
                ]
                return EvaluationFeedback(results=results)

        class _FakeData:
            def get_data(self):
                return data

            def get_returns(self):
                return returns

            def get_test_data(self):
                return None

            def get_test_returns(self):
                return None

        class _Miner(BaseFactorMiner):
            def initialize_search_space(self):
                pass

            def generate_candidates(self):
                return []

            def evaluate_candidates(self, candidates):
                return None

            def update_model(self, candidates, feedback):
                pass

        m = _Miner(_FakeData(), {"finalize_method": finalize_method, "paradigm": "GP"})
        m.evaluator = _FakeEval()
        return m, data, returns

    def _fake_expr(self, panel, fit=1.0):
        from core.miner.expressions import FactorExpressionAST
        e = FactorExpressionAST({"op": "close"})
        e._panel = panel
        e.metrics = {"fitness_score": fit}
        e.logic_hash = str(id(e))
        e.compute = lambda d: panel  # noqa: E731
        return e

    def test_alphapool_finalize_returns_combined(self):
        m, data, returns = self._make_miner("alphapool")
        rng = np.random.default_rng(9)
        # Two independent predictive signals -> both admitted, combined returned.
        s1 = rng.standard_normal((len(data), len(data.columns)))
        s2 = rng.standard_normal((len(data), len(data.columns)))
        exprs = [self._fake_expr(s1, 1.0), self._fake_expr(s2, 1.0)]
        out = m._finalize(exprs)
        self.assertGreaterEqual(len(out), 2)
        from core.miner.expressions import LinearCombinedFactor
        self.assertIsInstance(out[0], LinearCombinedFactor)
        self.assertEqual(len(out[0].sub_expressions), 2)

    def test_linear_finalize_still_default(self):
        m, data, returns = self._make_miner("linear")
        rng = np.random.default_rng(9)
        s1 = rng.standard_normal((len(data), len(data.columns)))
        exprs = [self._fake_expr(s1, 1.0)]
        out = m._finalize(exprs)
        self.assertGreaterEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
