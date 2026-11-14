import unittest

import numpy as np
import pandas as pd

from core.evaluation.evaluator import ParallelEvaluator


def _panel(T, N, seed):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=T, freq="D")
    cols = [f"A{i}" for i in range(N)]
    return pd.DataFrame(rng.standard_normal((T, N)), index=idx, columns=cols)


class _FakeExpr:
    """Minimal stand-in for a FactorExpression with a fixed output panel."""

    def __init__(self, panel):
        self._panel = panel
        self.metrics = {}

    def compute(self, data):  # noqa: D401 - returns the fixed panel
        return self._panel


class BuildVerdictTest(unittest.TestCase):
    def setUp(self):
        self.evalr = ParallelEvaluator(data_client=None, config={})

    def test_strong_signal_low_p(self):
        rng = np.random.default_rng(3)
        T, N = 250, 30
        f = rng.standard_normal((T, N))
        r = 0.4 * f + 0.8 * rng.standard_normal((T, N))
        fdf = pd.DataFrame(f, index=pd.date_range("2020-01-01", periods=T, freq="D"),
                           columns=[f"A{i}" for i in range(N)])
        rdf = pd.DataFrame(r, index=fdf.index, columns=fdf.columns)
        v = self.evalr._build_verdict(fdf, rdf, {"Turnover": 0.0, "oos_consistent": True})
        self.assertIn("hac_t", v)
        self.assertIn("p_value", v)
        self.assertLess(v["p_value"], 0.05)   # significant
        self.assertGreater(v["dsr"], 0.9)     # strong IR -> high DSR

    def test_noise_high_p(self):
        rng = np.random.default_rng(4)
        T, N = 250, 30
        f = rng.standard_normal((T, N))
        r = rng.standard_normal((T, N))        # no link to factor
        fdf = pd.DataFrame(f, index=pd.date_range("2020-01-01", periods=T, freq="D"),
                           columns=[f"A{i}" for i in range(N)])
        rdf = pd.DataFrame(r, index=fdf.index, columns=fdf.columns)
        v = self.evalr._build_verdict(fdf, rdf, {"Turnover": 0.0, "oos_consistent": True})
        self.assertGreater(v["p_value"], 0.05)  # not significant

    def test_negative_ic_is_oriented_before_gating(self):
        # A factor whose IC is consistently negative is a valid long-short
        # signal: the verdict must orient it in-sample (like the backtest does)
        # instead of reporting negative IR / negative cost-adjusted return.
        rng = np.random.default_rng(6)
        T, N = 250, 30
        f = rng.standard_normal((T, N))
        r = -0.4 * f + 0.8 * rng.standard_normal((T, N))
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        fdf = pd.DataFrame(f, index=idx, columns=cols)
        rdf = pd.DataFrame(r, index=idx, columns=cols)
        v = self.evalr._build_verdict(fdf, rdf, {"Turnover": 0.0, "oos_consistent": True})
        self.assertEqual(v["orientation"], -1.0)
        self.assertGreater(v["ir"], 0.0)
        self.assertGreater(v["hac_t"], 0.0)
        self.assertGreater(v["cost_adj_return"], 0.0)
        self.assertLess(v["p_value"], 0.05)

    def test_sequential_mode_neutral(self):
        rng = np.random.default_rng(5)
        T = 200
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        f = pd.Series(rng.standard_normal(T), index=idx)
        r = pd.Series(rng.standard_normal(T), index=idx)
        v = self.evalr._build_verdict(f, r, {"Turnover": 0.0, "oos_consistent": True})
        # cross-section does not exist -> neutral verdict, factor not validated
        self.assertEqual(v["p_value"], 1.0)
        self.assertEqual(v["hac_t"], 0.0)


class EvaluateOnVerdictIntegrationTest(unittest.TestCase):
    def test_verdict_merged_into_metrics(self):
        rng = np.random.default_rng(7)
        T, N = 250, 30
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        f_sig = pd.DataFrame(0.4 * rng.standard_normal((T, N)) + 0.3 * rng.standard_normal((T, N)),
                             index=idx, columns=cols)
        r = pd.DataFrame(0.4 * f_sig.to_numpy() + 0.8 * rng.standard_normal((T, N)),
                         index=idx, columns=cols)
        # a pure-noise factor for the second candidate
        f_noise = pd.DataFrame(rng.standard_normal((T, N)), index=idx, columns=cols)

        evalr = ParallelEvaluator(data_client=None, config={})
        fb = _FakeExpr(f_sig)
        nb = _FakeExpr(f_noise)
        fb.metrics = {}
        nb.metrics = {}
        feedback = evalr.evaluate_on([fb, nb], f_sig, r)
        m_sig = feedback.results[0].metrics
        m_noise = feedback.results[1].metrics
        self.assertIn("hac_t", m_sig)
        self.assertIn("dsr", m_sig)
        # A real signal must be *more* significant than a pure-noise factor.
        # (Note: because the IC time series averages over N=30 cross-sectional
        # assets, its standard error is tiny, so a raw per-factor t-test over-
        # states significance even for noise — which is exactly why the gate
        # relies on BH-FDR / DSR / OOS-consistency, not the raw p alone.)
        self.assertLess(m_sig["p_value"], m_noise["p_value"])
        self.assertGreater(m_sig["dsr"], m_noise["dsr"])


if __name__ == "__main__":
    unittest.main()
