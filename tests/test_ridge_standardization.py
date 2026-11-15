"""Regression tests for review item M-02 (scale-aware ridge combination).

The old solver used ``(X'X + lambda*I) w = X'y`` on raw factor panels. With
columns spanning 1e-2 .. 1e5 and non-zero means, ``lambda`` crushed the small
column and the target mean was absorbed into the slopes. These tests pin the
replacement: standardise + unpenalised intercept, mapped back to raw space.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from core.miner.expressions import (
    FactorExpressionAST,
    LinearCombinedFactor,
    _flatten_factor,
    fit_linear_combination,
)
from core.miner.pool import AlphaPool
from core.miner.ridge_weights import fit_scale_aware_ridge


def _scaled_design(seed: int = 0, n: int = 1200):
    """y = 7 + 0.8*z1 + 0.6*z2, with raw columns at 1e-2 and 1e5 scale.

    True raw slopes are [80, 6e-6] and the true raw intercept is
    7 - 80*3 - 6e-6*2e4 = -233.12.
    """
    rng = np.random.default_rng(seed)
    u1 = rng.standard_normal(n)
    u2 = rng.standard_normal(n)
    x1 = 0.01 * u1 + 3.0
    x2 = 1e5 * u2 + 2e4
    y = 7.0 + 0.8 * u1 + 0.6 * u2 + 0.01 * rng.standard_normal(n)
    return np.column_stack([x1, x2]), y


def _old_raw_ridge(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """The pre-fix solver, kept here as an explicit counterexample."""
    xtx = X.T @ X + lam * np.eye(X.shape[1])
    return np.linalg.solve(xtx, X.T @ y)


class ScaleAwareRidgeTests(unittest.TestCase):
    def test_recovers_true_slopes_where_old_solver_failed(self):
        X, y = _scaled_design()
        fit = fit_scale_aware_ridge(X, y, reg_lambda=1.0, min_obs=60)
        old = _old_raw_ridge(X, y, 1.0)

        self.assertFalse(fit.fallback)
        self.assertEqual(fit.method, "ridge")
        # True slopes: 80 and 6e-6. The old solver is off by >10x on the
        # small-scale column because its 3.0 mean dominates X'X.
        self.assertAlmostEqual(fit.weights[0] / 80.0, 1.0, delta=0.1)
        self.assertAlmostEqual(fit.weights[1] / 6e-6, 1.0, delta=0.1)
        self.assertLess(abs(old[0] / fit.weights[0]), 0.2)
        self.assertAlmostEqual(fit.intercept, -233.12, delta=2.0)

    def test_raw_space_prediction_matches_standardised_space(self):
        X, y = _scaled_design(seed=1)
        fit = fit_scale_aware_ridge(X, y)

        center = np.asarray(fit.center)
        scale = np.asarray(fit.scale)
        z = (X - center) / scale
        pred_z = z @ np.asarray(fit.standardized_weights) + fit.intercept_standardized
        pred_raw = X @ np.asarray(fit.weights) + fit.intercept
        self.assertTrue(np.allclose(pred_z, pred_raw, rtol=1e-8, atol=1e-6))
        self.assertGreater(float(np.corrcoef(pred_raw, y)[0, 1]), 0.99)

    def test_intercept_absorbs_target_mean(self):
        rng = np.random.default_rng(2)
        n = 500
        x = 100.0 + rng.standard_normal(n)          # large non-zero mean
        y = 5.0 + 2.0 * (x - 100.0) + 0.01 * rng.standard_normal(n)
        fit = fit_scale_aware_ridge(x.reshape(-1, 1), y)
        # y = 2*x - 195 in raw space; the intercept must be a free parameter.
        self.assertAlmostEqual(fit.weights[0], 2.0, delta=0.05)
        self.assertAlmostEqual(fit.intercept, -195.0, delta=1.0)
        # In z-space the intercept is the target mean; the sample mean of x
        # is not exactly 100, so it shifts by 2*(mean(x)-100).
        self.assertAlmostEqual(
            fit.intercept_standardized, 5.0 + 2.0 * (x.mean() - 100.0), delta=0.1
        )

    def test_constant_column_is_degenerate_not_explosive(self):
        rng = np.random.default_rng(3)
        n = 400
        x1 = rng.standard_normal(n)
        X = np.column_stack([x1, np.full(n, 1e12)])
        y = 3.0 * x1 + rng.standard_normal(n) * 0.01
        fit = fit_scale_aware_ridge(X, y)
        self.assertEqual(fit.degenerate, [1])
        self.assertEqual(fit.weights[1], 0.0)
        self.assertTrue(all(np.isfinite(fit.weights)))
        self.assertAlmostEqual(fit.weights[0], 3.0, delta=0.1)

    def test_nonfinite_rows_are_masked_before_scaling(self):
        X, y = _scaled_design(seed=4, n=300)
        X = X.copy()
        y = y.copy()
        X[0, 1] = np.nan
        y[0] = 1e9
        X[1, 0] = np.inf
        fit = fit_scale_aware_ridge(X, y, min_obs=60)
        self.assertEqual(fit.n_obs, 298)
        clean = X[2:]
        self.assertAlmostEqual(fit.center[0], float(clean[:, 0].mean()), places=8)
        self.assertAlmostEqual(fit.scale[0], float(clean[:, 0].std()), places=8)

    def test_fallbacks_return_finite_equal_weights(self):
        X, y = _scaled_design(seed=5, n=20)
        short = fit_scale_aware_ridge(X, y, min_obs=60)
        self.assertTrue(short.fallback)
        self.assertEqual(short.weights, [0.5, 0.5])

        X_full, _ = _scaled_design(seed=5, n=100)
        constant = fit_scale_aware_ridge(X_full, np.full(len(X_full), 2.0), min_obs=60)
        self.assertTrue(constant.fallback)
        self.assertEqual(constant.reason, "constant_target")

        # lambda=0 + perfectly collinear columns must not raise or emit NaN.
        collinear = np.column_stack([np.arange(100.0), np.arange(100.0)])
        fit = fit_scale_aware_ridge(collinear, np.arange(100.0) * 0.5,
                                    reg_lambda=0.0, min_obs=60)
        self.assertTrue(all(np.isfinite(fit.weights)))
        self.assertEqual(len(fit.weights), 2)

    def test_diagnostics_are_json_safe(self):
        X, y = _scaled_design(seed=6)
        diag = fit_scale_aware_ridge(X, y).diagnostics()
        for key in ("method", "n_obs", "center", "scale", "intercept"):
            self.assertIn(key, diag)
        self.assertIsInstance(diag["degenerate_columns"], list)


class RidgeIntegrationTests(unittest.TestCase):
    """Both callers must keep returning RAW-space weights."""

    def _frames(self, seed=7, t=200, n_assets=5):
        rng = np.random.default_rng(seed)
        u1 = rng.standard_normal((t, n_assets))
        u2 = rng.standard_normal((t, n_assets))
        idx = pd.date_range("2024-01-01", periods=t, freq="D")
        cols = [f"A{i}" for i in range(n_assets)]
        x1 = pd.DataFrame(0.01 * u1 + 3.0, index=idx, columns=cols)
        x2 = pd.DataFrame(1e5 * u2 + 2e4, index=idx, columns=cols)
        y = pd.DataFrame(7.0 + 0.8 * u1 + 0.6 * u2, index=idx, columns=cols)
        return x1, x2, y

    @staticmethod
    def _expr(panel):
        e = FactorExpressionAST({"op": "close"})
        e.compute = lambda d, p=panel: p
        return e

    def test_fit_linear_combination_weights_still_work_in_compute(self):
        x1, x2, y = self._frames()
        combo = fit_linear_combination(
            [self._expr(x1), self._expr(x2)], x1, y, reg_lambda=1.0, min_obs=60
        )
        self.assertIsInstance(combo, LinearCombinedFactor)
        self.assertEqual(len(combo.weights), 2)
        # Raw-space weights: the small-scale column needs a coefficient ~1e4
        # times larger than the 1e5-scale column.
        self.assertGreater(abs(combo.weights[0]), 10.0)
        self.assertLess(abs(combo.weights[1]), 1e-3)

        out = _flatten_factor(combo.compute(x1))
        target = _flatten_factor(y)
        mask = np.isfinite(out) & np.isfinite(target)
        self.assertGreater(float(np.corrcoef(out[mask], target[mask])[0, 1]), 0.95)

    def test_alphapool_exposes_scaled_fit_and_balanced_z_weights(self):
        x1, x2, y = self._frames(seed=8)
        pool = AlphaPool(capacity=5, mutual_ic_max=0.999, weight_fit_shift=0,
                         reg_lambda=1.0, min_obs=50)
        pool.set_target(y)
        self.assertTrue(pool.admit("small", x1)[0])
        self.assertTrue(pool.admit("large", x2)[0])

        diag = pool.report()["ridge"]
        self.assertFalse(diag["fallback"])
        self.assertEqual(diag["method"], "ridge")
        self.assertEqual(len(diag["scale"]), 2)
        # In z-space the two signals should receive comparable weight; the
        # old raw solver concentrated ~all of it on the huge-scale column.
        zw = [abs(v) for v in diag["standardized_weights"]]
        self.assertGreater(min(zw), 0.0)
        self.assertLess(max(zw) / min(zw), 5.0)
        self.assertAlmostEqual(sum(abs(w) for w in pool._weights), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
