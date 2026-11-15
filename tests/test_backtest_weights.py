"""Ensemble-weight resolution for the backtest adapter.

Two invariants are covered here:

1. A stored linear-combination factor (``type == "linear_combination"`` with
   ``member_factor_ids``) must be expanded into its members with the *fitted*
   weights. Before this existed the adapter averaged members equally, which
   silently threw away the AlphaPool ridge solution.
2. Explicit ``--weights`` must reach the signal construction instead of being
   ignored.
"""

import os
import tempfile
import unittest

import numpy as np
import pandas as pd

from core.backtest.backtest_engine import load_factor_signals, resolve_signal_weights
from core.storage.factor_storage import LocalFactorStorage
from core.storage.factor_storage import FactorMetadata


def _write_snapshot(root: str, factor_id: str, assets: list[str], sign: float, seed: int = 3) -> None:
    n = 24
    rng = np.random.default_rng(seed)
    rows = []
    for asset in assets:
        base = rng.standard_normal(n)
        fwd = sign * base + rng.standard_normal(n) * 0.01
        for i in range(n):
            rows.append(
                {
                    "timestamp": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i),
                    "asset": asset,
                    "factor": float(base[i]),
                    "forward_return": float(fwd[i]),
                }
            )
    value_dir = os.path.join(root, "values")
    os.makedirs(value_dir, exist_ok=True)
    pd.DataFrame(rows).to_parquet(os.path.join(value_dir, f"{factor_id}.parquet"), index=False)


def _write_metadata(root: str, factor_id: str, logic_reference) -> None:
    storage = LocalFactorStorage(root)
    metadata = FactorMetadata(
        factor_id=factor_id,
        miner_type="MyCustomGP",
        user_id="tester",
        metrics={},
        logic_hash="",
        generation_config={},
        created_at="2026-01-01T00:00:00",
        lifecycle_status="DISCOVERED",
    )
    metadata.logic_reference = logic_reference
    storage._save_meta(metadata)


class ResolveWeightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "db")
        _write_snapshot(self.root, "a", ["X", "Y", "Z"], sign=1.0, seed=1)
        _write_snapshot(self.root, "b", ["X", "Y", "Z"], sign=1.0, seed=2)
        _write_metadata(self.root, "a", {"type": "json_ast", "ast": {}})
        _write_metadata(self.root, "b", {"type": "json_ast", "ast": {}})
        _write_metadata(
            self.root,
            "combo",
            {
                "type": "linear_combination",
                "weights": [0.75, -0.25],
                "sub_asts": [{}, {}],
                "member_factor_ids": ["a", "b"],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_combination_expands_into_members_with_fitted_weights(self):
        ids, weights, expanded = resolve_signal_weights(["combo"], db_root=self.root)
        self.assertEqual(ids, ["a", "b"])
        self.assertAlmostEqual(weights[0], 0.75)
        self.assertAlmostEqual(weights[1], -0.25)
        self.assertIn("combo", expanded)
        self.assertEqual(expanded["combo"]["members"], ["a", "b"])

    def test_explicit_weights_scale_the_expansion(self):
        ids, weights, _ = resolve_signal_weights(
            ["combo"], db_root=self.root, weights={"combo": 2.0}
        )
        self.assertAlmostEqual(weights[0], 1.5)
        self.assertAlmostEqual(weights[1], -0.5)

    def test_combination_without_member_ids_stays_opaque(self):
        _write_metadata(
            self.root,
            "orphan",
            {"type": "linear_combination", "weights": [0.6, 0.4], "sub_asts": [{}, {}]},
        )
        _write_snapshot(self.root, "orphan", ["X", "Y", "Z"], sign=1.0, seed=4)
        ids, weights, expanded = resolve_signal_weights(["orphan"], db_root=self.root)
        self.assertEqual(ids, ["orphan"])
        self.assertEqual(weights, [1.0])
        self.assertEqual(expanded, {})

    def test_positional_weights_must_align(self):
        with self.assertRaises(ValueError):
            resolve_signal_weights(["a", "b"], db_root=self.root, weights=[0.5])

    def test_signals_record_effective_weights(self):
        signals = load_factor_signals(["combo"], db_root=self.root, allow_inferred_orientation=True)
        effective = signals.attrs["effective_weights"]
        self.assertAlmostEqual(effective["a"], 0.75)
        self.assertAlmostEqual(effective["b"], -0.25)
        self.assertIn("prediction", signals.columns)


class WeightedSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "db")
        _write_snapshot(self.root, "strong", ["X", "Y", "Z"], sign=1.0, seed=11)
        _write_snapshot(self.root, "weak", ["X", "Y", "Z"], sign=1.0, seed=12)

    def tearDown(self):
        self.tmp.cleanup()

    def _rank_of(self, signals: pd.DataFrame, asset: str, ts) -> float:
        row = signals[(signals["asset"] == asset) & (signals["timestamp"] == ts)]
        return float(row["prediction"].iloc[0])

    def test_zero_weight_removes_a_factor_from_the_ensemble(self):
        ts = pd.Timestamp("2025-01-05")
        equal = load_factor_signals(["strong", "weak"], db_root=self.root, allow_inferred_orientation=True)
        only_strong = load_factor_signals(
            ["strong", "weak"], db_root=self.root, weights={"strong": 1.0, "weak": 0.0}, allow_inferred_orientation=True)
        # With weak zeroed out the signal must equal the single-factor signal.
        single = load_factor_signals(["strong"], db_root=self.root, allow_inferred_orientation=True)
        for asset in ("X", "Y", "Z"):
            self.assertAlmostEqual(
                self._rank_of(only_strong, asset, ts), self._rank_of(single, asset, ts), places=9
            )
        # And the equal-weight blend must differ somewhere, otherwise the test
        # would pass even if weights were being ignored.
        self.assertFalse(
            all(
                abs(self._rank_of(equal, a, ts) - self._rank_of(single, a, ts)) < 1e-12
                for a in ("X", "Y", "Z")
            )
        )

    def test_negative_weight_inverts_a_factor_contribution(self):
        ts = pd.Timestamp("2025-01-05")
        mixed = load_factor_signals(
            ["strong", "weak"], db_root=self.root, weights={"strong": 1.0, "weak": -1.0}, allow_inferred_orientation=True)
        inverted_weak = load_factor_signals(
            ["weak"], db_root=self.root, weights={"weak": -1.0}, allow_inferred_orientation=True)
        strong = load_factor_signals(["strong"], db_root=self.root, allow_inferred_orientation=True)
        for asset in ("X", "Y", "Z"):
            expected = (
                self._rank_of(strong, asset, ts) + self._rank_of(inverted_weak, asset, ts)
            ) / 2.0
            self.assertAlmostEqual(self._rank_of(mixed, asset, ts), expected, places=9)

    def test_explicit_weights_honored_without_combination_expansion(self):
        # #4 regression: when resolve_combinations is False the old code silently
        # discarded --weights and collapsed to equal weight. It must still apply.
        ts = pd.Timestamp("2025-01-05")
        only_strong = load_factor_signals(
            ["strong", "weak"], db_root=self.root,
            weights={"strong": 1.0, "weak": 0.0},
            resolve_combinations=False, allow_inferred_orientation=True)
        single = load_factor_signals(
            ["strong"], db_root=self.root, allow_inferred_orientation=True)
        for asset in ("X", "Y", "Z"):
            self.assertAlmostEqual(
                self._rank_of(only_strong, asset, ts), self._rank_of(single, asset, ts), places=9)
        self.assertAlmostEqual(only_strong.attrs["effective_weights"]["strong"], 1.0)
        self.assertAlmostEqual(only_strong.attrs["effective_weights"]["weak"], 0.0)

    def test_positional_weights_must_align_without_expansion(self):
        # The no-expansion branch must still validate weight length (not silently
        # fall back to equal weight when the count is wrong).
        with self.assertRaises(ValueError):
            load_factor_signals(
                ["strong", "weak"], db_root=self.root,
                weights=[0.5], resolve_combinations=False, allow_inferred_orientation=True)


if __name__ == "__main__":
    unittest.main()
