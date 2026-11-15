"""Tests for strict-OOS signal construction in the backtest adapter.

The key invariant: signal *direction* must be learnable from an in-sample root
while the evaluated *factor values* come from a separate held-out root. Inferring
direction from the evaluation window itself is look-ahead leakage.
"""

import os
import tempfile
import unittest

import numpy as np
import pandas as pd

from core.backtest.backtest_engine import infer_orientation, load_factor_signals


def _write_snapshot(root: str, factor_id: str, assets: list[str], sign: float) -> None:
    """Write a snapshot whose factor/forward_return Spearman sign is ``sign``."""
    val_dir = os.path.join(root, "values")
    os.makedirs(val_dir, exist_ok=True)
    n = 30
    rng = np.random.default_rng(7)
    rows = []
    for asset in assets:
        base = rng.standard_normal(n)
        factor = base
        fwd = sign * base + rng.standard_normal(n) * 0.01  # near-perfect +/- correlation
        for i in range(n):
            rows.append({
                "timestamp": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i),
                "asset": asset,
                "factor": float(factor[i]),
                "forward_return": float(fwd[i]),
            })
    pd.DataFrame(rows).to_parquet(os.path.join(val_dir, f"{factor_id}.parquet"), index=False)


class StrictOOSOrientationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.is_root = os.path.join(self.tmp.name, "insample")
        self.oos_root = os.path.join(self.tmp.name, "heldout")
        # In-sample: positive IC (+1 direction), single asset (only used for sign).
        _write_snapshot(self.is_root, "f1", ["A"], sign=1.0)
        # Held-out: negative IC (-1 direction), TWO assets (the evaluated values).
        _write_snapshot(self.oos_root, "f1", ["A", "B"], sign=-1.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_orientation_inferred_from_snapshot_sign(self):
        from core.storage.factor_storage import LocalFactorStorage

        is_df = LocalFactorStorage(self.is_root).load_factor_values("f1")
        oos_df = LocalFactorStorage(self.oos_root).load_factor_values("f1")
        self.assertEqual(infer_orientation(is_df), 1.0)
        self.assertEqual(infer_orientation(oos_df), -1.0)

    def test_legacy_behaviour_uses_evaluation_root_for_direction(self):
        signals = load_factor_signals(["f1"], db_root=self.oos_root, allow_inferred_orientation=True)
        self.assertEqual(set(signals["asset"]), {"A", "B"})
        self.assertTrue((signals["prediction"] < 0).all(),
                        "legacy mode should infer -1 from the held-out snapshot")

    def test_orientation_db_root_freezes_direction_without_changing_values(self):
        signals = load_factor_signals(
            ["f1"], db_root=self.oos_root, orientation_db_root=self.is_root, allow_inferred_orientation=True)
        # Values still come from the held-out root (both assets present)...
        self.assertEqual(set(signals["asset"]), {"A", "B"})
        # ...but the sign is frozen from the in-sample root (+1).
        self.assertTrue((signals["prediction"] > 0).all(),
                        "strict OOS must use the in-sample direction, not the OOS one")

    def test_different_roots_produce_opposite_signals(self):
        legacy = load_factor_signals(["f1"], db_root=self.oos_root, allow_inferred_orientation=True)
        strict = load_factor_signals(
            ["f1"], db_root=self.oos_root, orientation_db_root=self.is_root, allow_inferred_orientation=True)
        merged = legacy.merge(strict, on=["timestamp", "asset"], suffixes=("_legacy", "_strict"))
        self.assertEqual(len(merged), len(legacy))
        self.assertTrue((merged["prediction_legacy"] * merged["prediction_strict"] < 0).all())

    def test_missing_orientation_snapshot_excludes_factor(self):
        """A3: no in-sample snapshot means NO direction, not a peek at OOS."""
        empty_root = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty_root, exist_ok=True)
        with self.assertRaises(ValueError):
            load_factor_signals(
                ["f1"], db_root=self.oos_root, orientation_db_root=empty_root
            )

    def test_default_refuses_orientation_from_evaluation_window(self):
        """Without metadata or an in-sample root, the factor is excluded."""
        with self.assertRaises(ValueError):
            load_factor_signals(["f1"], db_root=self.oos_root)

    def test_pre_registered_metadata_orientation_is_used(self):
        from unittest.mock import patch

        fake_meta = type("M", (), {"metrics": {"orientation": 1.0}})()
        with patch(
            "core.storage.factor_storage.LocalFactorStorage.get_metadata",
            return_value=fake_meta,
        ):
            signals = load_factor_signals(["f1"], db_root=self.oos_root)
        self.assertEqual(
            signals.attrs["orientation_sources"]["f1"], "metadata:pre_registered"
        )
        self.assertTrue((signals["prediction"] > 0).all())


if __name__ == "__main__":
    unittest.main()
