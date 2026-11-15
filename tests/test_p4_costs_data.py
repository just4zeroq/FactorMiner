"""P4: real funding, cost identity and RL terminal parity."""

from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from core.backtest.backtest_engine import build_funding, load_funding_series
from core.experiment.costs import resolve_cost_params


class TestRealFunding(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "data"
        streams = self.root / "binance" / "futures" / "crypto_streams"
        streams.mkdir(parents=True)
        pd.DataFrame({
            "date": pd.to_datetime([
                "2024-01-01 00:00", "2024-01-01 08:00", "2024-01-01 16:00",
                "2024-01-02 00:00",
            ]),
            "funding_rate": [0.0001, 0.0002, 0.0003, -0.0004],
        }).to_feather(streams / "BTC_USDT_USDT-1d-funding_rate.feather")
        self.addCleanup(self._tmp.cleanup)

    def test_three_8h_settlements_aggregate_to_one_daily_bar(self):
        series = load_funding_series(
            str(self.root), "binance", "futures", "1d", ["BTC/USDT:USDT"]
        )
        self.assertIn("BTC/USDT:USDT", series)
        self.assertAlmostEqual(
            float(series["BTC/USDT:USDT"].loc[pd.Timestamp("2024-01-01")]), 0.0006
        )
        self.assertAlmostEqual(
            float(series["BTC/USDT:USDT"].loc[pd.Timestamp("2024-01-02")]), -0.0004
        )

    def test_build_funding_uses_real_rates(self):
        series = load_funding_series(
            str(self.root), "binance", "futures", "1d", ["BTC/USDT:USDT"]
        )
        prices = pd.DataFrame({
            "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
            "asset": ["BTC/USDT:USDT"] * 3,
        })
        grid = build_funding(prices, 0.0001, funding_series=series)
        self.assertAlmostEqual(float(grid.loc[0, "rate"]), 0.0006)
        self.assertAlmostEqual(float(grid.loc[1, "rate"]), -0.0004)
        # Missing bar falls back to the configured constant, not to zero.
        self.assertAlmostEqual(float(grid.loc[2, "rate"]), 0.0001)
        self.assertIn("crypto_streams", grid.attrs["funding_source"])
        self.assertEqual(grid.attrs["funding_assets_matched"], 1)

    def test_constant_grid_still_backward_compatible(self):
        prices = pd.DataFrame({
            "timestamp": pd.to_datetime(["2024-01-01"]),
            "asset": ["BTC/USDT:USDT"],
        })
        grid = build_funding(prices, 0.0002)
        self.assertEqual(float(grid.loc[0, "rate"]), 0.0002)
        self.assertEqual(grid.attrs["funding_source"], "constant")

    def test_daily_rate_scaled_to_per_bar_by_timeframe(self):
        # #3 regression: a *daily* funding rate must be spread across bars so
        # intraday backtests are not overcharged 24x / 288x.
        prices = pd.DataFrame({
            "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
            "asset": ["BTC/USDT:USDT"] * 3,
        })
        for timeframe, factor in (("1d", 1.0), ("1h", 1 / 24.0), ("5m", 1 / 288.0)):
            with self.subTest(timeframe=timeframe):
                grid = build_funding(prices, 0.0001, timeframe=timeframe)
                expected = round(0.0001 * factor, 12)
                self.assertAlmostEqual(float(grid.loc[0, "rate"]), expected, places=12)
                self.assertAlmostEqual(grid.attrs["funding_per_bar_rate"], expected, places=12)
                self.assertEqual(grid.attrs["funding_timeframe"], timeframe)
        # 1d must stay byte-for-byte identical to the legacy constant grid.
        grid_1d = build_funding(prices, 0.0002, timeframe="1d")
        self.assertEqual(float(grid_1d.loc[0, "rate"]), 0.0002)


    def test_missing_stream_is_reported(self):
        out = load_funding_series(
            str(self.root), "binance", "futures", "1d", ["ETH/USDT:USDT"]
        )
        self.assertEqual(out, {})


class TestCostIdentity(unittest.TestCase):
    def test_defaults_are_consistent(self):
        r = resolve_cost_params({})
        self.assertTrue(r["consistent"])
        self.assertAlmostEqual(r["gate_cost_rate"], 0.0018)
        self.assertAlmostEqual(r["implied_round_trip"], 0.0018)

    def test_mismatch_is_detected(self):
        r = resolve_cost_params({"fitness": {"gate_cost_rate": 0.0005}})
        self.assertFalse(r["consistent"])
        self.assertGreater(abs(r["delta"]), 0.001)

    def test_explicit_config_round_trips(self):
        r = resolve_cost_params({
            "fitness": {"gate_cost_rate": 0.0024},
            "backtest": {"commission_rate": 0.0006, "slippage_rate": 0.0006},
        })
        self.assertTrue(r["consistent"])


class TestRLTerminals(unittest.TestCase):
    def test_crypto_streams_become_rl_terminals(self):
        from core.miner.paradigms.rl_miner import RLFactorMiner

        panel = {
            "close": pd.DataFrame([[1.0]]),
            "funding_rate": pd.DataFrame([[0.0001]]),
            "basis": pd.DataFrame([[0.2]]),
        }
        miner = RLFactorMiner(panel, {
            "paradigm": "RL",
            "search_space": {"allowed_operators": ["add", "sub"]},
        })
        miner.initialize_search_space()
        self.assertIn("funding_rate", miner.terminals)
        self.assertIn("basis", miner.terminals)
        self.assertIn("close", miner.terminals)

    def test_ohlcv_only_panel_is_unchanged(self):
        from core.miner.paradigms.rl_miner import RLFactorMiner

        panel = {"close": pd.DataFrame([[1.0]])}
        miner = RLFactorMiner(panel, {
            "paradigm": "RL", "search_space": {"allowed_operators": ["add"]}
        })
        miner.initialize_search_space()
        self.assertNotIn("funding_rate", miner.terminals)


class TestCLIFlag(unittest.TestCase):
    def test_real_funding_flag_registered(self):
        from core.cli import setup_parser

        args = setup_parser().parse_args([
            "backtest", "--factors", "fac_x", "--real-funding"
        ])
        self.assertTrue(args.real_funding)


class TestCostSingleSourceOfTruth(unittest.TestCase):
    """The round-trip cost must be defined exactly once (review A2 / F2).

    Previously ``0.0018`` was hardcoded in five modules, so a change in one
    place silently broke the ``gate_cost_rate == 2 * (commission + slippage)``
    identity everywhere else. Every consumer now reads ``DEFAULT_COST_RATE``
    from ``core.config.defaults`` (which itself derives from
    ``core.experiment.costs.DEFAULTS``), so there is a single numeric literal.
    """

    def test_all_consumers_share_one_cost_constant(self):
        from core.config.defaults import DEFAULT_COST_RATE
        from core.experiment.costs import DEFAULTS
        from core.evaluation.evaluator import DEFAULT_FITNESS_CFG
        from core.evaluation.portfolio_eval import DEFAULT_LS_CFG
        from core.experiment.walk_forward import DEFAULT_WF_CFG
        from core.experiment.gate_calibration import DEFAULT_COST_RATE as GC_RATE

        self.assertEqual(DEFAULT_COST_RATE, DEFAULTS["gate_cost_rate"])
        self.assertEqual(DEFAULT_FITNESS_CFG["gate_cost_rate"], DEFAULT_COST_RATE)
        self.assertEqual(DEFAULT_LS_CFG["cost_rate"], DEFAULT_COST_RATE)
        self.assertEqual(DEFAULT_WF_CFG["cost_rate"], DEFAULT_COST_RATE)
        self.assertEqual(GC_RATE, DEFAULT_COST_RATE)

    def test_only_one_numeric_cost_literal_remains(self):
        # After the refactor the only hard-coded round-trip cost literal should be
        # the canonical one inside core/experiment/costs.py. Any other occurrence
        # in core/ (outside comments) means the single-source rule regressed.
        import subprocess

        out = subprocess.check_output(
            ["grep", "-rn", "0.0018", "core/", "--include=*.py"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
        ).decode()
        # strip the two documenting comments we intentionally kept
        code_lines = [
            ln for ln in out.splitlines()
            if "DO NOT re-hardcode" not in ln
            and "lived in five different files" not in ln
        ]
        # Expect exactly one real literal: costs.py:18 (the canonical DEFAULTS).
        self.assertEqual(len(code_lines), 1, f"unexpected costs literal:\n{out}")
        self.assertIn("core/experiment/costs.py", code_lines[0])


if __name__ == "__main__":
    unittest.main()
