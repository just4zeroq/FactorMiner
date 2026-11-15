"""Regression tests for review item 10 (A1-A5 look-ahead / data-integrity leaks).

A1  mining loop must not read the validation split
A2  negative shift / bfill blocked statically AND dynamically
A3  backtest direction must be pre-registered, not learned from the test window
A4  BH-FDR family must not collapse to one p-value
A5  constant / exploding factors must never be evaluated or ranked
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from core.backtest.backtest_engine import load_factor_signals
from core.evaluation.code_sandbox import (
    FactorOutputError,
    RestrictedSandbox,
    SecurityError,
    causality_probe,
)
from core.evaluation.evaluator import DEFAULT_FITNESS_CFG, ParallelEvaluator
from core.evaluation.significance import apply_promotion_gates
from core.experiment import budget
from core.miner.operator_runtime import (
    OperatorRuntimeError,
    _safe_divide,
    _ts_ref,
)
from core.miner.registry import ExtensionRegistrationError, OperatorRegistry


def _frame(n: int = 60) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": np.arange(100, 100 + n, dtype=float)},
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


class A1SearchOosIsolationTests(unittest.TestCase):
    def _evaluator(self, allow: bool = False):
        client = MagicMock()
        client.get_data.return_value = _frame()
        client.get_returns.return_value = _frame()
        client.get_test_data.return_value = _frame()
        client.get_test_returns.return_value = _frame()
        cfg = {"fitness": {"allow_oos_in_search": allow}}
        return ParallelEvaluator(client, cfg), client

    def test_evaluate_does_not_touch_the_validation_split(self):
        ev, client = self._evaluator()
        ev.evaluate([])
        client.get_test_data.assert_not_called()
        client.get_test_returns.assert_not_called()

    def test_explicit_optin_restores_the_legacy_path(self):
        ev, client = self._evaluator(allow=True)
        ev.evaluate([])
        client.get_test_data.assert_called_once()
        client.get_test_returns.assert_called_once()

    def test_evaluate_final_reads_validation_exactly_once(self):
        ev, client = self._evaluator()
        ev.evaluate_final([])
        client.get_test_data.assert_called_once()
        client.get_test_returns.assert_called_once()

    def test_config_default_is_fail_closed(self):
        self.assertFalse(DEFAULT_FITNESS_CFG["allow_oos_in_search"])

    def test_two_oneshot_oos_paths_agree_on_signed_rankic(self):
        """evaluate_final and the director's _attach_final_oos_metrics must
        report the SAME signed OOS RankIC for the same factor/split.

        Before the fix one path emitted ``abs(RankIC)`` and the other the
        signed value; the promotion gate takes abs() so verdicts matched, but
        stored metadata disagreed in sign. This pins the two surfaces together.
        """
        from core.miner.director import FactorMinerDirector
        from core.miner.expressions import FactorExpressionAST

        n = 120
        idx = pd.date_range("2025-01-01", periods=n, freq="D")
        rng = np.random.default_rng(7)
        # A weak negative-IC factor so the *signed* OOS RankIC is negative and
        # abs() would hide the sign.
        factor_series = pd.Series(rng.standard_normal(n), index=idx)
        returns_series = pd.Series(-0.2 * factor_series + rng.standard_normal(n) * 0.5,
                                   index=idx)

        class _SeriesClient:
            def get_data(self):
                return factor_series

            def get_returns(self):
                return returns_series

            def get_test_data(self):
                return factor_series

            def get_test_returns(self):
                return returns_series

        class _ConstExpr(FactorExpressionAST):
            def __init__(self, values):
                super().__init__(ast_dict={"op": "add", "left": "close", "right": "close"})
                self._values = values

            def compute(self, data):
                return self._values

        expr = _ConstExpr(factor_series)

        ev = ParallelEvaluator(_SeriesClient(), {})
        final_fb = ev.evaluate_final([expr])
        final_metrics = final_fb.for_candidate(expr).metrics
        rk_final = final_metrics.get("RankIC_oos")

        # Director path (unbound method reuse with a minimal stand-in self).
        director = FactorMinerDirector.__new__(FactorMinerDirector)
        director.data_client = _SeriesClient()
        director.evaluator = ev
        merged = director._attach_final_oos_metrics([expr], [{"RankIC": final_metrics.get("RankIC", 0.0)}])
        rk_director = merged[0].get("RankIC_oos")

        self.assertAlmostEqual(float(rk_final), float(rk_director), places=12)
        # Both must preserve the sign (negative here), not abs it away.
        self.assertLess(float(rk_final), 0.0)
        self.assertLess(float(rk_director), 0.0)


class A2CausalityTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = RestrictedSandbox()
        self._registry_before = set(OperatorRegistry._registry)

    def tearDown(self):
        for key in set(OperatorRegistry._registry) - self._registry_before:
            OperatorRegistry._registry.pop(key, None)

    def test_static_rejects_negative_shift(self):
        for code in (
            "factor = df['close'].shift(-1)",
            "factor = df['close'].diff(periods=-2)",
            "factor = df['close'].rolling(-3).mean()",
        ):
            with self.subTest(code=code), self.assertRaises(SecurityError):
                self.sandbox.validate_code(code)

    def test_static_rejects_backward_fill(self):
        with self.assertRaises(SecurityError):
            self.sandbox.validate_code(
                "factor = df['close'].fillna(method='bfill')"
            )

    def test_static_allows_positive_lag(self):
        self.sandbox.validate_code("factor = df['close'].shift(1)")

    def test_dynamic_probe_catches_indirect_negative_lag(self):
        code = compile(
            "n = -1\nfactor = df['close'].shift(n)", "<probe>", "exec"
        )
        g = {"np": np, "pd": pd}
        frame = _frame()
        local_vars = {"df": frame}
        exec(code, dict(g), local_vars)
        with self.assertRaises(SecurityError):
            causality_probe(code, g, frame, local_vars["factor"])

    def test_dynamic_probe_allows_a_causal_rolling_mean(self):
        code = compile(
            "factor = df['close'].rolling(3).mean()", "<probe>", "exec"
        )
        g = {"np": np, "pd": pd}
        frame = _frame()
        local_vars = {"df": frame}
        exec(code, dict(g), local_vars)
        causality_probe(code, g, frame, local_vars["factor"])

    def test_ast_ref_rejects_negative_lag(self):
        with self.assertRaises(OperatorRuntimeError):
            _ts_ref(_frame()["close"], -1)

    def test_registry_rejects_lookahead_operator(self):
        with self.assertRaises(ExtensionRegistrationError):
            @OperatorRegistry.register(arity=1)
            def bad_shift(s):
                return s.shift(-1)

    def test_registry_accepts_causal_operator(self):
        @OperatorRegistry.register(arity=1)
        def causal_mean(s):
            return s.rolling(4).mean()
        self.assertIn("causal_mean", OperatorRegistry._registry)


class A4FdrFamilyTests(unittest.TestCase):
    def test_single_factor_call_with_history_still_corrects(self):
        m = [{
            "p_value": 0.01, "hac_t": 3.0, "dsr": 0.95,
            "oos_consistent": True, "cost_adj_return": 0.01,
            "oos_enabled": True, "RankIC_oos": 0.05, "ls_t": 2.0,
        }]
        out = apply_promotion_gates(m, n_trials=100)
        self.assertGreater(out[0]["p_adj"], 0.01)
        self.assertNotAlmostEqual(out[0]["p_adj"], 0.01, places=6)

    def test_resolve_unions_all_three_families(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            budget.sync(50, [0.8] * 3, path=path)
            _, family, _ = budget.resolve(50, [0.2], [0.3], path=path)
            self.assertEqual(len(family), 5)
            self.assertIn(0.8, family)
            self.assertIn(0.2, family)
            self.assertIn(0.3, family)

    def test_reaudit_no_longer_gates_one_factor_at_a_time(self):
        from pathlib import Path

        src = Path("scripts/reaudit_factor_db.py").read_text(encoding="utf-8")
        self.assertNotIn("apply_promotion_gates([gate_input]", src)
        self.assertIn("family_p_values=family", src)


class A5ConstantFactorTests(unittest.TestCase):
    def test_validate_rejects_constant_panel(self):
        idx = pd.date_range("2025-01-01", periods=40, freq="D")
        const = pd.DataFrame({"A": np.ones(40), "B": np.ones(40)}, index=idx)
        with self.assertRaises(FactorOutputError):
            ParallelEvaluator._validate_metric_inputs(const, const.copy())

    def test_validate_rejects_exploding_panel(self):
        idx = pd.date_range("2025-01-01", periods=40, freq="D")
        vals = np.arange(40, dtype=float) * 1e14
        big = pd.DataFrame({"A": vals, "B": vals / 3}, index=idx)
        with self.assertRaises(FactorOutputError):
            ParallelEvaluator._validate_metric_inputs(big, big.copy())

    def test_safe_divide_returns_nan_not_1e9(self):
        s = pd.Series([1.0, 2.0, 3.0])
        out = _safe_divide(s, pd.Series([0.0, 1.0, 2.0]))
        self.assertTrue(np.isnan(out.iloc[0]))
        self.assertAlmostEqual(out.iloc[1], 2.0)
        self.assertTrue(np.isfinite(out).iloc[1:].all())

    def test_backtest_rank_ignores_constant_cross_sections(self):
        import os
        import tempfile

        from core.storage.factor_storage import LocalFactorStorage

        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            for t in pd.date_range("2025-01-01", periods=12, freq="D"):
                for i, asset in enumerate(("A", "B", "C")):
                    rows.append({
                        "timestamp": t, "asset": asset,
                        # First 6 timestamps are constant across assets.
                        "factor": 1.0 if t.day <= 6 else float(i),
                        "forward_return": 0.001 * i,
                    })
            values = os.path.join(tmp, "values")
            os.makedirs(values)
            pd.DataFrame(rows).to_parquet(
                os.path.join(values, "f1.parquet"), index=False
            )
            os.makedirs(os.path.join(tmp, "metadata"))
            LocalFactorStorage(tmp)  # ensure dirs
            fake_meta = type("M", (), {"metrics": {"orientation": 1.0}})()
            from unittest.mock import patch

            with patch(
                "core.storage.factor_storage.LocalFactorStorage.get_metadata",
                return_value=fake_meta,
            ):
                signals = load_factor_signals(["f1"], db_root=tmp)
            # All-equal cross-sections must not contribute ranks.
            self.assertEqual(signals["timestamp"].nunique(), 6)
            self.assertNotIn("2025-01-01", set(signals["timestamp"].astype(str)))


if __name__ == "__main__":
    unittest.main()
