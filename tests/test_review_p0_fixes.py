"""Regression tests for the quant-review P0 fixes (review 2026-11-14).

These lock the behaviour of four correctness fixes uncovered by the quant
code review:

* M1  ``_spearman_row`` / ``ic_series`` must mask NaN pairwise instead of
        ranking NaN as the largest value (sparse-panel IC inflation).
* M2  ``ParallelEvaluator._coverage`` must count finite *cells*, not drop
        whole rows (dropna collapse on sparse crypto panels).
* H1  ``_fitness_score`` must penalise factors whose RankIC is positive but
        whose quantile long/short book loses money (search target must match
        the promotion gate target).
* H3  director surfaces the promotion verdict on ``cand.metrics`` so the
        pipeline can count promotions (covered in test_director_gating.py).
"""

import math
import unittest

import numpy as np
import pandas as pd

from core.evaluation.significance import _spearman_row, ic_series
from core.evaluation.evaluator import ParallelEvaluator, DEFAULT_FITNESS_CFG


def _naive_with_nan(a, b):
    """The pre-fix behaviour: NaN is ranked as the largest value."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    ar = a.argsort().argsort().astype(float)
    br = b.argsort().argsort().astype(float)
    da = ar - ar.mean()
    db = br - br.mean()
    denom = math.sqrt((da ** 2).sum() * (db ** 2).sum())
    return (da * db).sum() / denom if denom > 0 else 0.0


class TestSpearmanNanMask(unittest.TestCase):
    def test_spearman_row_ignores_nan_index(self):
        a = np.array([1.0, 2.0, 3.0, np.nan, 5.0])
        b = np.array([2.0, 1.0, 4.0, 99.0, 3.0])
        got = _spearman_row(a, b)
        # Reference: drop the NaN index, then rank the remaining pairs.
        am = np.array([1.0, 2.0, 3.0, 5.0])
        bm = np.array([2.0, 1.0, 4.0, 3.0])
        ar = am.argsort().argsort().astype(float)
        br = bm.argsort().argsort().astype(float)
        da = ar - ar.mean()
        db = br - br.mean()
        exp = (da * db).sum() / math.sqrt((da ** 2).sum() * (db ** 2).sum())
        self.assertAlmostEqual(got, exp, places=12)
        # The old implementation treated the NaN as the largest rank and gave a
        # materially different (wrong) coefficient.
        self.assertNotAlmostEqual(got, _naive_with_nan(a, b), places=6)

    def test_ic_series_per_row_mask(self):
        # 3 timestamps x 4 assets; asset 3 has NaN on rows 0 and 1 only.
        f = np.array([
            [1.0, 2.0, 3.0, np.nan],
            [3.0, 2.0, 1.0, np.nan],
            [1.0, 3.0, 2.0, 4.0],
        ])
        r = np.array([
            [1.0, 2.0, 3.0, 99.0],
            [3.0, 2.0, 1.0, 99.0],
            [1.0, 3.0, 2.0, 4.0],
        ])
        out = ic_series(f, r)
        # Each valid cross-section is perfectly monotone -> IC == 1.0, and the
        # stray 99.0 on the NaN rows must not leak into any period's rank.
        self.assertAlmostEqual(float(out[0]), 1.0, places=6)
        self.assertAlmostEqual(float(out[1]), 1.0, places=6)
        self.assertAlmostEqual(float(out[2]), 1.0, places=6)


class TestCoverageCellWise(unittest.TestCase):
    def test_complete_panel_is_one(self):
        df = pd.DataFrame(np.ones((10, 4)))
        self.assertAlmostEqual(ParallelEvaluator._coverage(df), 1.0, places=6)

    def test_counts_cells_not_rows(self):
        df = pd.DataFrame(np.ones((10, 4)))
        df.iloc[0, 0] = np.nan
        df.iloc[1, :] = np.nan  # one fully-missing row
        cov = ParallelEvaluator._coverage(df)
        # 40 cells, 5 missing -> 35/40 = 0.875 (old dropna logic dropped the
        # whole second row and reported 0.90, over-counting valid cells).
        self.assertAlmostEqual(cov, 35 / 40, places=6)

    def test_sparse_panel_not_collapsed(self):
        rng = np.random.default_rng(0)
        f = rng.normal(size=(500, 20))
        f[rng.random(f.shape) < 0.15] = np.nan
        cov = ParallelEvaluator._coverage(pd.DataFrame(f))
        # 15% scatter missing should report ~0.85; the old implementation
        # collapsed it toward ~0.04 and silently zeroed fitness.
        self.assertGreater(cov, 0.80)
        self.assertLess(cov, 0.90)


class TestFitnessTradeablePenalty(unittest.TestCase):
    def _ev(self):
        ev = ParallelEvaluator.__new__(ParallelEvaluator)
        ev.config = {}
        return ev

    def test_tradeable_penalty_applied(self):
        ev = self._ev()
        fv = pd.DataFrame(np.ones((20, 4)))
        rt = pd.DataFrame(np.ones((20, 4)))
        base = {"RankIC": 0.05, "IC": 0.04, "Turnover": 0.1}
        full = ev._build_fitness({**base, "ls_cost_adj_return": 0.003}, None, fv, rt)
        bad = ev._build_fitness({**base, "ls_cost_adj_return": -0.003}, None, fv, rt)
        self.assertGreater(full["fitness_score"], 0)
        # Positive RankIC but a losing long/short book -> multiplied by the
        # tradeable floor (0.1), so the director stops favouring it.
        self.assertAlmostEqual(
            bad["fitness_score"], full["fitness_score"] * 0.1, places=6
        )

    def test_positive_ic_and_return_keeps_full(self):
        ev = self._ev()
        fv = pd.DataFrame(np.ones((20, 4)))
        rt = pd.DataFrame(np.ones((20, 4)))
        base = {"RankIC": 0.05, "IC": 0.04, "Turnover": 0.1}
        out = ev._build_fitness({**base, "ls_cost_adj_return": 0.002}, None, fv, rt)
        # rankic_in=0.05, no OOS split -> oos auto-off, shrink forced to 1.0.
        # blended = 0.05^(0.5) * 0.05^(0.5) = 0.05, fitness = 100 * 0.05 = 5.0.
        self.assertAlmostEqual(out["fitness_score"], 5.0, places=6)


if __name__ == "__main__":
    unittest.main()
