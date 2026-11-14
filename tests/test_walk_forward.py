"""P3: purge/embargo folds and the per-fold refit regression lock."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from core.experiment.walk_forward import (
    _net_return,
    build_folds,
    load_snapshots,
    walk_forward_validate,
)


def synthetic_panels(T: int = 400, N: int = 8):
    rng = np.random.default_rng(7)
    a = rng.normal(size=(T, N))
    b = rng.normal(size=(T, N))
    sign = np.where(np.arange(T) < T // 2, 1.0, -1.0)[:, None]
    r = 0.02 * sign * a + 0.005 * rng.normal(size=(T, N))
    idx = pd.date_range("2023-01-01", periods=T, freq="D")
    cols = [f"A{i}" for i in range(N)]
    panels = {
        "f1": pd.DataFrame(a, index=idx, columns=cols),
        "f2": pd.DataFrame(b, index=idx, columns=cols),
    }
    returns = pd.DataFrame(r, index=idx, columns=cols)
    return panels, returns


class TestFolds(unittest.TestCase):
    def test_purge_and_embargo_leave_a_real_gap(self):
        idx = pd.date_range("2023-01-01", periods=400, freq="D")
        folds = build_folds(idx, n_folds=5, horizon_bars=1, embargo_bars=5)
        self.assertEqual(len(folds), 5)
        for f in folds:
            self.assertGreaterEqual(f["gap_bars"], 1 + 5)
            self.assertLess(pd.Timestamp(f["train_end"]), pd.Timestamp(f["val_start"]))
            self.assertLess(f["train_slice"][1], f["val_slice"][0])
        for a, b in zip(folds, folds[1:]):
            self.assertLessEqual(a["val_slice"][1], b["val_slice"][0])

    def test_too_short_history_yields_no_folds(self):
        idx = pd.date_range("2023-01-01", periods=20, freq="D")
        self.assertEqual(build_folds(idx, n_folds=5, min_train_bars=60), [])

    def test_horizon_widens_the_gap(self):
        idx = pd.date_range("2023-01-01", periods=400, freq="D")
        h1 = build_folds(idx, n_folds=3, horizon_bars=1, embargo_bars=2)[0]["gap_bars"]
        h5 = build_folds(idx, n_folds=3, horizon_bars=5, embargo_bars=2)[0]["gap_bars"]
        self.assertEqual(h5 - h1, 4)


class TestWalkForward(unittest.TestCase):
    def test_each_fold_refits_its_own_weights(self):
        panels, returns = synthetic_panels()
        report = walk_forward_validate(panels, returns, {"target": {"horizon_bars": 1}})
        self.assertGreaterEqual(report["n_folds"], 3)
        self.assertTrue(report["refit_per_fold"])
        self.assertTrue(report["weights_differ_across_folds"])
        self.assertEqual(report["purge_bars"], 1)
        self.assertEqual(report["embargo_bars"], 5)
        for fold in report["folds"]:
            self.assertEqual(len(fold["weights"]), 2)
            self.assertIn("rank_ic", fold)
            self.assertIn("net_return", fold)

    def test_summary_fields_are_consistent(self):
        panels, returns = synthetic_panels()
        r = walk_forward_validate(panels, returns)
        ics = [f["rank_ic"] for f in r["folds"]]
        self.assertAlmostEqual(r["mean_rank_ic"], round(float(np.mean(ics)), 6), places=5)
        self.assertEqual(r["positive_folds"], sum(1 for f in r["folds"] if f["net_return"] > 0))
        self.assertIsInstance(r["direction_consistent"], bool)

    def test_empty_panels_raise(self):
        with self.assertRaises(ValueError):
            walk_forward_validate({}, pd.DataFrame())

    def test_net_return_never_selects_invalid_columns(self):
        # review F4/C2: when a bar has >= k invalid (NaN) columns, the old code
        # pushed them to the front of the ascending argsort and let the short leg
        # pick all-NaN positions, so np.nanmean silently dropped the whole bar.
        # The short leg must only ever come from valid assets.
        rng = np.random.default_rng(3)
        T, N = 6, 12
        quantile = 0.2
        k = max(int(N * quantile), 1)
        combined = rng.normal(size=(T, N))
        returns = rng.normal(size=(T, N)) * 0.01
        invalid = 6  # >= k invalid columns per bar
        combined[:, :invalid] = np.nan
        returns[:, :invalid] = np.nan

        net, turnover = _net_return(combined, returns, quantile, 0.0)

        # Oracle: each bar selects longs/shorts only from valid columns.
        gross_oracle = []
        for rc, rr in zip(combined, returns):
            ok = np.isfinite(rc) & np.isfinite(rr)
            valid = np.where(ok)[0]
            order = valid[np.argsort(rc[valid], kind="stable")]
            longs, shorts = order[-k:], order[:k]
            gross_oracle.append(float(np.mean(rr[longs]) - np.mean(rr[shorts])))
        expected = float(np.mean(gross_oracle))

        self.assertTrue(np.isfinite(net), "NaN short leg must not silently drop bars")
        self.assertTrue(np.isfinite(turnover))
        self.assertAlmostEqual(net, expected, places=9)

    def test_load_snapshots_from_long_parquet(self):
        import tempfile
        from pathlib import Path

        panels, returns = synthetic_panels(T=60, N=5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "db"
            (root / "values").mkdir(parents=True)
            for name, panel in panels.items():
                long = panel.stack().rename("factor").reset_index()
                long.columns = ["timestamp", "asset", "factor"]
                long["forward_return"] = returns.stack().values
                long.to_parquet(root / "values" / f"{name}.parquet", index=False)
            loaded, ret = load_snapshots(str(root))
            self.assertEqual(set(loaded), set(panels))
            self.assertEqual(ret.shape, (60, 5))
            report = walk_forward_validate(loaded, ret,
                                           {"experiment": {"walk_forward": {
                                               "n_folds": 3, "min_train_bars": 10}}})
            self.assertGreaterEqual(report["n_folds"], 1)


if __name__ == "__main__":
    unittest.main()
