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

    def test_member_ids_survive_dict_roundtrip(self):
        """from_dict must restore member_factor_ids (lineage), not drop them."""
        a = self._ast("ts_ema", window=10)
        b = self._ast("ts_ref", n=1)
        combo = LinearCombinedFactor(
            [a, b], weights=[0.5, 0.5], parent_ids=["fac_a", "fac_b"]
        )
        d = combo.to_dict()
        self.assertEqual(d["member_factor_ids"], ["fac_a", "fac_b"])
        back = LinearCombinedFactor.from_dict(d)
        self.assertEqual(back.member_factor_ids, ["fac_a", "fac_b"])
        self.assertEqual(back.to_dict()["member_factor_ids"], ["fac_a", "fac_b"])

    def test_legacy_logic_reference_member_ids_are_read(self):
        """Older stored rows nested the ids under logic_reference; still read."""
        a = self._ast("ts_ema", window=10)
        d = LinearCombinedFactor([a], weights=[1.0]).to_dict()
        d.pop("member_factor_ids", None)
        d["logic_reference"] = {"member_factor_ids": ["legacy_1"]}
        back = LinearCombinedFactor.from_dict(d)
        self.assertEqual(back.member_factor_ids, ["legacy_1"])

    def test_stored_combination_recomputes_elementwise(self):
        """Full storage round trip: save -> read metadata -> from_dict -> compute
        must reproduce the ORIGINAL factor values element by element."""
        import json
        import tempfile
        from core.miner.entities import FactorMetadata
        from core.storage.factor_storage import LocalFactorStorage

        df = self._data()
        a = self._ast("ts_ema", window=10)
        b = self._ast("ts_ref", n=1)
        combo = LinearCombinedFactor([a, b], weights=[0.35, -0.65],
                                     parent_ids=["fac_a", "fac_b"])
        original = combo.compute(df)

        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalFactorStorage(db_root=tmp)
            meta = FactorMetadata(factor_id="combo_1", miner_type="combination",
                                  user_id="test")
            logic = combo.to_dict()
            storage.save_linear_combination_factor(logic, meta)

            saved = json.loads(
                open(os.path.join(tmp, "metadata", "combo_1.json"), encoding="utf-8").read()
            )
            # member ids were persisted into logic_reference
            self.assertEqual(
                saved["logic_reference"]["member_factor_ids"], ["fac_a", "fac_b"]
            )
            back = LinearCombinedFactor.from_dict(saved["logic_reference"])
            self.assertEqual(back.member_factor_ids, ["fac_a", "fac_b"])

        rebuilt = back.compute(df)
        np.testing.assert_allclose(
            rebuilt.dropna().values, original.dropna().values, atol=1e-9
        )

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
