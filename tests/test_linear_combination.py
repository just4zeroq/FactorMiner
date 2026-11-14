import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.miner.expressions import (
    FactorExpressionAST,
    LinearCombinedFactor,
    fit_linear_combination,
)


class TestLinearCombination(unittest.TestCase):
    def _data(self):
        idx = pd.date_range("2024-01-01", periods=200, freq="D")
        rng = np.random.default_rng(0)
        df = pd.DataFrame(
            {
                "close": np.cumsum(rng.standard_normal(200)) + 100.0,
                "volume": np.abs(rng.standard_normal(200)) * 1e3 + 500.0,
            },
            index=idx,
        )
        return df

    def _ast(self, op, **kw):
        node = {"op": op, "left": "close"}
        node.update(kw)
        return FactorExpressionAST(ast_dict=node)

    def test_compute_is_weighted_sum(self):
        df = self._data()
        a = self._ast("ts_zscore")
        b = self._ast("ts_rank")
        combo = LinearCombinedFactor([a, b], weights=[0.3, -0.7])
        out = combo.compute(df)
        expected = a.compute(df) * 0.3 + b.compute(df) * (-0.7)
        self.assertTrue(
            np.allclose(out.dropna().values, expected.dropna().values, atol=1e-9)
        )

    def test_fit_recovers_sign_and_reconstructs(self):
        df = self._data()
        rng = np.random.default_rng(2)
        a = self._ast("ts_mean", window=5)
        b = self._ast("ts_std", window=20)
        fa = a.compute(df)
        fb = b.compute(df)
        true_w = np.array([0.6, -0.4])
        noise = rng.standard_normal(len(df)) * 1e-6
        y = fa.values * true_w[0] + fb.values * true_w[1] + noise
        returns = pd.Series(y, index=df.index)

        combo = fit_linear_combination([a, b], df, returns, reg_lambda=1.0)
        w = np.array(combo.weights)
        self.assertEqual(len(w), 2)
        # sign should match the true generating weights
        self.assertAlmostEqual(np.sign(w[0]), 1.0, delta=0.05)
        self.assertAlmostEqual(np.sign(w[1]), -1.0, delta=0.05)

        recon = fa.values * w[0] + fb.values * w[1]
        mask = np.isfinite(recon) & np.isfinite(y)
        corr = np.corrcoef(recon[mask], y[mask])[0, 1]
        self.assertGreater(corr, 0.9)

    def test_to_from_dict_roundtrip(self):
        df = self._data()
        a = self._ast("ts_ema", window=10)
        b = self._ast("ts_ref", n=1)
        combo = LinearCombinedFactor([a, b], weights=[0.5, 0.5])
        d = combo.to_dict()
        self.assertEqual(d["type"], "linear_combination")
        self.assertEqual(len(d["sub_asts"]), 2)
        back = LinearCombinedFactor.from_dict(d)
        self.assertEqual(len(back.sub_expressions), 2)
        out1 = combo.compute(df).dropna()
        out2 = back.compute(df).dropna()
        self.assertTrue(np.allclose(out1.values, out2.values, atol=1e-9))

    def test_insufficient_subs_falls_back_to_equal(self):
        df = self._data()
        a = self._ast("ts_mean", window=5)
        b = self._ast("ts_std", window=20)
        returns = pd.Series(np.random.default_rng(3).standard_normal(len(df)), index=df.index)
        # Only one usable sub -> equal-weight fallback (K=1 usable -> handled).
        combo = fit_linear_combination([a], df, returns)
        self.assertEqual(len(combo.weights), 1)
        # Two subs but degenerate returns (no variance) -> equal fallback.
        flat = pd.Series(np.zeros(len(df)), index=df.index)
        combo2 = fit_linear_combination([a, b], df, flat)
        self.assertEqual(len(combo2.weights), 2)


if __name__ == "__main__":
    unittest.main()
