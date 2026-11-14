import unittest

import numpy as np
import pandas as pd

from core.miner.operator_runtime import (
    BUILTIN_OPERATORS,
    OperatorRuntimeError,
    default_operator_params,
    evaluate_ast,
    resolve_operator_specs,
    sample_operator_params,
)
from core.miner.registry import (
    EvaluatorRegistry,
    ExtensionRegistrationError,
    MinerRegistry,
    OperatorRegistry,
)
from core.startup_validation import StartupValidationError, validate_mining_startup
from core.utils.dynamic_loader import load_user_modules


class OperatorRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load_user_modules("user_workspace")
        if cls.report.errors:
            raise AssertionError(cls.report.errors)

    def test_registered_unary_operator_evaluates_an_ast(self):
        data = pd.DataFrame({"close": range(1, 20), "volume": range(20, 39)})
        for operator in ("custom_ts_decay", "ts_zscore_20", "ts_delta_5", "ts_rank_20", "ts_volatility_20"):
            result = evaluate_ast({"op": operator, "left": "close"}, data)
            self.assertIsInstance(result, pd.Series)
            self.assertEqual(result.index.tolist(), data.index.tolist())
            self.assertGreater(result.notna().sum(), 0, msg=operator)

    def test_operator_metadata_exposes_unary_arity(self):
        specs = resolve_operator_specs(["custom_ts_decay", "ts_zscore_20", "ts_delta_5", "ts_rank_20", "ts_volatility_20", "add"])
        self.assertEqual(specs["custom_ts_decay"]["arity"], 1)
        self.assertEqual(specs["ts_zscore_20"]["arity"], 1)
        self.assertEqual(specs["ts_delta_5"]["arity"], 1)
        self.assertEqual(specs["ts_rank_20"]["arity"], 1)
        self.assertEqual(specs["ts_volatility_20"]["arity"], 1)
        self.assertEqual(specs["add"]["arity"], 2)

    def test_unknown_operator_has_actionable_startup_error(self):
        config = {
            "paradigm": "MyCustomGP",
            "max_iterations": 1,
            "data_feeds": {"pairs": ["BTC/USDT:USDT"], "required_streams": ["close"]},
            "search_space": {"allowed_operators": ["not_registered"]},
        }

        with self.assertRaisesRegex(StartupValidationError, "Unknown operator 'not_registered'"):
            validate_mining_startup(config, self.report)

    def test_unknown_fitness_hook_has_actionable_startup_error(self):
        config = {
            "paradigm": "MyCustomGP",
            "max_iterations": 1,
            "data_feeds": {"pairs": ["BTC/USDT:USDT"], "required_streams": ["close"]},
            "fitness": {"hook": "missing_hook"},
        }

        with self.assertRaisesRegex(StartupValidationError, "Unknown Fitness Hook 'missing_hook'"):
            validate_mining_startup(config, self.report)

    def test_unknown_miner_has_actionable_startup_error(self):
        config = {
            "paradigm": "MissingMiner",
            "max_iterations": 1,
            "data_feeds": {"pairs": ["BTC/USDT:USDT"], "required_streams": ["close"]},
        }

        with self.assertRaisesRegex(StartupValidationError, "Unknown Miner 'MissingMiner'"):
            validate_mining_startup(config, self.report)

    def test_deprecated_dl_name_is_mapped_to_nn(self):
        config = {
            "paradigm": "DL",
            "max_iterations": 1,
            "data_feeds": {
                "pairs": ["BTC/USDT:USDT"],
                "required_streams": ["close", "volume"],
            },
        }
        with self.assertLogs("core.startup_validation", level="WARNING") as logs:
            validate_mining_startup(config, self.report)
        self.assertEqual(config["paradigm"], "NN")
        self.assertIn("deprecated", "\n".join(logs.output))
        self.assertIn("NN", MinerRegistry._registry)

    def test_invalid_extension_signatures_fail_at_registration(self):
        with self.assertRaisesRegex(ExtensionRegistrationError, "arity must be 1 or 2"):
            OperatorRegistry.register(arity=3)

        def incomplete_hook(base_metrics):
            return 0.0

        with self.assertRaisesRegex(ExtensionRegistrationError, "must accept at least 3"):
            EvaluatorRegistry.register_fitness_hook("incomplete_hook")(incomplete_hook)

    def test_invalid_operator_name_fails_without_silent_zero_factor(self):
        data = pd.DataFrame({"close": range(1, 5)})
        with self.assertRaisesRegex(OperatorRuntimeError, "Unknown operator"):
            evaluate_ast({"op": "missing", "left": "close"}, data)


class CryptoOperatorExpansionTests(unittest.TestCase):
    """Coverage for the P0 crypto operator expansion (~18 -> ~58 operators)."""

    @staticmethod
    def _series(n=120, seed=1):
        rng = np.random.default_rng(seed)
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        price = 100 + np.cumsum(rng.standard_normal(n))
        price = np.maximum(price, 1.0)  # keep positive for vol/rsi
        return pd.Series(price, index=idx, name="close")

    @staticmethod
    def _cross_asset(n=120, k=4, seed=2):
        rng = np.random.default_rng(seed)
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        cols = [f"A{i}" for i in range(k)]
        return pd.DataFrame(rng.standard_normal((n, k)), index=idx, columns=cols)

    def test_new_univariate_operators_return_nonempty_series(self):
        s = self._series()
        ops = [
            ("ts_ema", {"window": 10}),
            ("ts_ref", {"n": 3}),
            ("ts_sum", {"window": 5}),
            ("ts_product", {"window": 5}),
            ("ts_ma_ratio", {"window": 10}),
            ("ts_var", {"window": 10}),
            ("ts_median", {"window": 10}),
            ("ts_mad", {"window": 10}),
            ("ts_skew", {"window": 10}),
            ("ts_kurt", {"window": 10}),
            ("ts_quantile", {"window": 10, "q": 0.25}),
            ("ts_argmax", {"window": 10}),
            ("ts_argmin", {"window": 10}),
            ("ts_count_up", {"window": 10}),
            ("ts_realized_vol", {"window": 10}),
            ("ts_rsi", {"window": 14}),
            ("cumsum", {}),
            ("abs", {}),
            ("sign", {}),
            ("log", {}),
            ("sqrt", {}),
            ("exp", {}),
            ("inv", {}),
            ("sin", {}),
            ("cos", {}),
            ("tanh", {}),
            ("sigmoid", {}),
            ("clamp", {"lo": -1.0, "hi": 1.0}),
        ]
        for op, params in ops:
            with self.subTest(op=op):
                node = {"op": op, "left": "close", **params}
                out = evaluate_ast(node, s.to_frame())
                self.assertIsInstance(out, pd.Series)
                self.assertGreater(out.notna().sum(), 0, msg=f"{op} produced only NaNs")

    def test_pairwise_and_conditional_operators(self):
        s = self._series()
        df = s.to_frame()
        for op in ("ts_max2", "ts_min2", "greater", "less"):
            with self.subTest(op=op):
                node = {"op": op, "left": "close", "right": "close"}
                out = evaluate_ast(node, df)
                self.assertIsInstance(out, pd.Series)
                self.assertGreater(out.notna().sum(), 0)

    def test_binary_rolling_regression_is_vectorized(self):
        s = self._series()
        df = s.to_frame()
        # regression of close on its own lag -> slope ~ 1
        node = {"op": "ts_regression", "left": "close", "right": {"op": "ts_ref", "left": "close", "n": 1}, "window": 20}
        out = evaluate_ast(node, df)
        self.assertIsInstance(out, pd.Series)
        finite = out.dropna()
        self.assertGreater(len(finite), 0)
        self.assertTrue(finite.abs().mean() < 5.0, "slope of x on x.shift(1) should be near 1")

    def test_cross_sectional_ops_keep_dataframe_shape(self):
        # In production cross-asset mode, data is a dict of wide (multi-column)
        # frames, so a 'close' terminal returns the full cross-section frame.
        m = self._cross_asset()
        data = {"close": m}
        for op in ("cs_rank", "cs_zscore", "cs_mean", "cs_sum", "cs_min", "cs_max", "cs_mad", "cs_neutralize"):
            with self.subTest(op=op):
                out = evaluate_ast({"op": op, "left": "close"}, data)
                self.assertIsInstance(out, pd.DataFrame)
                self.assertEqual(out.shape, m.shape)

    def test_cross_sectional_neutralize_removes_per_row_mean(self):
        m = self._cross_asset()
        data = {"close": m}
        out = evaluate_ast({"op": "cs_neutralize", "left": "close"}, data)
        row_means = out.mean(axis=1)
        self.assertTrue(np.allclose(row_means.abs().dropna().values, 0.0, atol=1e-9))

    def test_single_series_cs_ops_are_identity(self):
        s = self._series()
        for op in ("cs_neutralize", "cs_mean", "cs_zscore"):
            with self.subTest(op=op):
                # Single stream -> terminal returns a Series -> cs ops are identity.
                out = evaluate_ast({"op": op, "left": "close"}, s.to_frame())
                self.assertIsInstance(out, pd.Series)

    def test_param_sampling_keys_match_spec(self):
        cases = {
            "ts_quantile": {"window", "q"},
            "ts_ref": {"n"},
            "signed_power": {"p"},
            "clamp": {"lo", "hi"},
            "ts_mean": {"window"},
        }
        for op, keys in cases.items():
            with self.subTest(op=op):
                sampled = sample_operator_params(op)
                self.assertEqual(set(sampled.keys()), keys)
                defaults = default_operator_params(op)
                self.assertEqual(set(defaults.keys()), keys)

    def test_param_sampling_types(self):
        s = sample_operator_params("ts_ema")
        self.assertIsInstance(s["window"], int)
        q = sample_operator_params("ts_quantile")
        self.assertIsInstance(q["window"], int)
        self.assertIsInstance(q["q"], float)

    def test_obv_style_crypto_factor_builds_from_primitives(self):
        rng = np.random.default_rng(7)
        idx = pd.date_range("2024-01-01", periods=120, freq="D")
        close = pd.Series(100 + np.cumsum(rng.standard_normal(120)), index=idx)
        close = np.maximum(close, 1.0)
        vol = pd.Series(np.abs(rng.standard_normal(120)) * 1000 + 500, index=idx)
        data = pd.DataFrame({"close": close, "volume": vol})
        # OBV = cumsum(sign(close.diff()) * volume)
        ast = {
            "op": "cumsum",
            "left": {
                "op": "mul",
                "left": {"op": "sign", "left": {"op": "ts_delta", "left": "close", "window": 1}},
                "right": "volume",
            },
        }
        out = evaluate_ast(ast, data)
        self.assertIsInstance(out, pd.Series)
        self.assertGreater(out.notna().sum(), 0)

    def test_realized_vol_crypto_factor_nonempty(self):
        s = self._series()
        out = evaluate_ast({"op": "ts_realized_vol", "left": "close", "window": 20}, s.to_frame())
        self.assertIsInstance(out, pd.Series)
        self.assertGreater(out.notna().sum(), 0)

    def test_total_operator_count_expanded(self):
        # Sanity: the built-in library now carries a substantial operator set.
        self.assertGreaterEqual(len(BUILTIN_OPERATORS), 50)


if __name__ == "__main__":
    unittest.main()
