import unittest

import numpy as np
import pandas as pd

from core.evaluation.evaluator import ParallelEvaluator
from core.evaluation.significance import deflated_sharpe_ratio


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

    def test_cost_gate_uses_returns_not_ic(self):
        # Phase P0.5: the cost gate must be denominated in returns. The same
        # factor (identical statistics) has to pass with cheap trading and fail
        # once the round-trip cost exceeds its edge. Before this,
        # cost_adj_return was `mean IC - cost`, a dimensionless number that
        # could not express "this book does not make money".
        rng = np.random.default_rng(8)
        T, N = 250, 30
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        f = rng.standard_normal((T, N))
        # realistic crypto scale: ~1% daily vol, a genuine but modest edge
        r = 0.002 * f + 0.01 * rng.standard_normal((T, N))
        fdf = pd.DataFrame(f, index=idx, columns=cols)
        rdf = pd.DataFrame(r, index=idx, columns=cols)

        cheap = ParallelEvaluator(
            data_client=None, config={"fitness": {"gate_cost_rate": 0.0018}}
        )._build_verdict(fdf, rdf, {})
        pricey = ParallelEvaluator(
            data_client=None, config={"fitness": {"gate_cost_rate": 0.02}}
        )._build_verdict(fdf, rdf, {})

        # the IC-based statistics are untouched by the cost assumption
        self.assertAlmostEqual(cheap["hac_t"], pricey["hac_t"])
        self.assertAlmostEqual(cheap["ir"], pricey["ir"])
        self.assertAlmostEqual(cheap["ls_return"], pricey["ls_return"])
        # only the economics change
        self.assertGreater(cheap["cost_adj_return"], 0.0)
        self.assertLess(pricey["cost_adj_return"], 0.0)
        # ... and because DSR deflates the *net* strategy Sharpe, it moves too
        self.assertGreater(cheap["dsr"], pricey["dsr"])

    def test_verdict_reports_long_short_diagnostics(self):
        rng = np.random.default_rng(9)
        T, N = 250, 30
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        f = rng.standard_normal((T, N))
        r = 0.4 * f + 0.8 * rng.standard_normal((T, N))
        fdf = pd.DataFrame(f, index=idx, columns=cols)
        rdf = pd.DataFrame(r, index=idx, columns=cols)
        v = self.evalr._build_verdict(fdf, rdf, {})
        for key in ("ls_return", "ls_sharpe", "ls_turnover", "ls_cost", "ls_t"):
            self.assertIn(key, v)
        self.assertGreater(v["ls_return"], 0.0)
        self.assertGreater(v["ls_turnover"], 0.0)

    def test_dsr_deflates_strategy_sharpe(self):
        # Phase P1: DSR must be computed on the traded book's Sharpe (net of
        # cost), not on the scale-free IC information ratio. This is what makes
        # the gate magnitude-aware: a stable-but-tiny IC no longer scores as
        # high as a strategy that actually earns money per unit of risk.
        rng = np.random.default_rng(13)
        T, N = 250, 30
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        f = rng.standard_normal((T, N))
        r = 0.002 * f + 0.01 * rng.standard_normal((T, N))
        v = self.evalr._build_verdict(
            pd.DataFrame(f, index=idx, columns=cols),
            pd.DataFrame(r, index=idx, columns=cols),
            {},
        )
        self.assertAlmostEqual(
            v["sr"], v["cost_adj_return"] / v["ls_vol"], places=10
        )
        self.assertAlmostEqual(
            v["dsr"],
            deflated_sharpe_ratio(v["sr"], n_obs=v["sr_n_obs"], n_trials=1),
            places=10,
        )
        # The IC IR and the strategy Sharpe are different quantities here.
        self.assertNotAlmostEqual(v["ir"], v["sr"], places=3)

    def test_sequential_mode_uses_timing_strategy(self):
        # Single-asset factors cannot be ranked cross-sectionally, but they can
        # be traded as a timing rule. They must therefore get a real verdict
        # instead of the old neutral one (which made RL/NN/single-coin GP
        # factors impossible to promote).
        rng = np.random.default_rng(5)
        T = 800
        idx = pd.date_range("2018-01-01", periods=T, freq="D")
        s = np.zeros(T)
        for t in range(1, T):
            s[t] = 0.98 * s[t - 1] + rng.standard_normal()
        f = pd.Series(s, index=idx)
        r = pd.Series(0.08 * s + 0.6 * rng.standard_normal(T), index=idx)

        v = self.evalr._build_verdict(f, r, {})
        self.assertEqual(v["mode"], "temporal")
        self.assertGreater(v["hac_t"], 2.0)
        self.assertLess(v["p_value"], 0.05)
        self.assertGreater(v["cost_adj_return"], 0.0)
        self.assertGreater(v["ls_exposure"], 0.0)
        self.assertGreater(v["sr"], 0.0)

    def test_sequential_noise_is_not_significant(self):
        rng = np.random.default_rng(15)
        T = 600
        idx = pd.date_range("2018-01-01", periods=T, freq="D")
        f = pd.Series(rng.standard_normal(T), index=idx)
        r = pd.Series(rng.standard_normal(T), index=idx)
        v = self.evalr._build_verdict(f, r, {})
        self.assertGreater(v["p_value"], 0.05)
        # A lucky positive stretch is possible, which is exactly why the gate
        # needs the t-stat / DSR and not just "net return > 0".
        self.assertLess(v["dsr"], 0.9)

    def test_sequential_too_short_is_neutral(self):
        idx = pd.date_range("2020-01-01", periods=20, freq="D")
        f = pd.Series(np.arange(20.0), index=idx)
        r = pd.Series(np.arange(20.0), index=idx)
        v = self.evalr._build_verdict(f, r, {})
        self.assertEqual(v["p_value"], 1.0)
        self.assertEqual(v["hac_t"], 0.0)

    def test_horizon_overlap_discounts_dsr_observations(self):
        """M-08: with horizon h>1 the forward returns overlap by h-1 bars.

        The DSR observational count must shrink to ~T/h so an overlapping
        h=5 factor is not credited with T independent observations. With the
        default h=1 the discount is a no-op.
        """
        rng = np.random.default_rng(31)
        T, N = 500, 30
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        f = rng.standard_normal((T, N))
        r = 0.002 * f + 0.01 * rng.standard_normal((T, N))
        fdf = pd.DataFrame(f, index=idx, columns=cols)
        rdf = pd.DataFrame(r, index=idx, columns=cols)

        h1 = ParallelEvaluator(
            data_client=None, config={"fitness": {"gate_rebalance_bars": 1}}
        )._build_verdict(fdf, rdf, {})
        h5 = ParallelEvaluator(
            data_client=None, config={"fitness": {"gate_rebalance_bars": 5}}
        )._build_verdict(fdf, rdf, {})

        # h=1: no discount -> sr_n_obs equals the traded-book obs count.
        self.assertEqual(h1["sr_n_obs"], h1["n_obs"])
        # h=5: independent obs approximate T/5 (ceil), far below raw T.
        self.assertLess(h5["sr_n_obs"], h5["n_obs"])
        self.assertAlmostEqual(h5["sr_n_obs"], int(np.ceil(h5["n_obs"] / 5.0)))
        # DSR is deflated on the reduced count -> no more optimistic than h=1.
        self.assertLessEqual(h5["dsr"], h1["dsr"] + 1e-12)

    def test_target_horizon_discount_applies_without_explicit_fitness_key(self):
        """P1-6: the discount must follow ``target.horizon_bars`` too.

        ``_build_verdict`` used to read the merged ``DEFAULT_FITNESS_CFG`` dict,
        whose ``gate_rebalance_bars`` default is 1. ``cfg.get(...) or
        self.gate_rebalance_bars`` therefore short-circuited to 1 for every
        config that declares the horizon in the target block (the normal case),
        so the M-08 overlap discount never actually fired. Resolving through
        ``self.gate_rebalance_bars`` (explicit fitness key > target spec) fixes
        it; this test pins the target-spec path.
        """
        rng = np.random.default_rng(32)
        T, N = 500, 30
        idx = pd.date_range("2020-01-01", periods=T, freq="D")
        cols = [f"A{i}" for i in range(N)]
        f = rng.standard_normal((T, N))
        r = 0.002 * f + 0.01 * rng.standard_normal((T, N))
        fdf = pd.DataFrame(f, index=idx, columns=cols)
        rdf = pd.DataFrame(r, index=idx, columns=cols)

        class _Spec:
            horizon_bars = 5

        class _Client:
            def get_target_spec(self):
                return _Spec()

        evaluator = ParallelEvaluator(data_client=_Client(), config={})
        self.assertEqual(evaluator.gate_rebalance_bars, 5)

        v = evaluator._build_verdict(fdf, rdf, {})
        self.assertLess(v["sr_n_obs"], v["n_obs"])
        self.assertAlmostEqual(v["sr_n_obs"], int(np.ceil(v["n_obs"] / 5.0)))


class EvaluateOnVerdictIntegrationTest(unittest.TestCase):
    def test_sequential_evaluate_on_sets_temporal_verdict(self):
        rng = np.random.default_rng(21)
        T = 700
        idx = pd.date_range("2018-01-01", periods=T, freq="D")
        s = np.zeros(T)
        for t in range(1, T):
            s[t] = 0.97 * s[t - 1] + rng.standard_normal()
        f = pd.Series(s, index=idx)
        r = pd.Series(0.08 * s + 0.6 * rng.standard_normal(T), index=idx)

        evalr = ParallelEvaluator(data_client=None, config={})
        expr = _FakeExpr(f)
        feedback = evalr.evaluate_on([expr], f, r)
        metrics = feedback.results[0].metrics
        self.assertEqual(metrics.get("mode"), "temporal")
        self.assertIn("ls_t", metrics)
        self.assertLess(metrics["p_value"], 0.05)

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
