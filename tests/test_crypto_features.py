"""Tests for the crypto-native feature library and its mining integration."""

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from core.data_feed.crypto_streams import (
    CRYPTO_STREAM_NAMES,
    _funding_settlement_interval,
    _paginate,
    align_series_to_index,
    build_basis_frame,
    crypto_stream_path,
    download_funding_rate,
    load_crypto_streams,
    materialize_crypto_streams,
    oi_period_for,
    timeframe_to_timedelta,
    _write_stream,
)
from core.miner.crypto_features import (
    CRYPTO_FEATURE_TEMPLATES,
    CRYPTO_STREAM_SPECS,
    build_crypto_feature_asts,
    crypto_feature_catalog,
    crypto_streams_in,
    crypto_terminals,
    describe_available_streams,
    streams_required_by_library,
)
from core.miner.expressions import FactorExpressionAST
from core.miner.operator_runtime import evaluate_ast


T, N = 240, 5


def _synthetic_panel(streams=CRYPTO_STREAM_NAMES):
    rng = np.random.default_rng(7)
    idx = pd.date_range("2025-01-01", periods=T, freq="1h")
    cols = [f"A{i}" for i in range(N)]

    def _walk(scale, mean):
        return pd.DataFrame(
            rng.normal(0, scale, (T, N)).cumsum(axis=0) + mean,
            index=idx,
            columns=cols,
        )

    panel = {
        "close": _walk(0.01, 100.0),
        "open": _walk(0.01, 100.0),
        "high": _walk(0.01, 101.0),
        "low": _walk(0.01, 99.0),
        "volume": pd.DataFrame(
            np.abs(rng.normal(1e6, 1e5, (T, N))), index=idx, columns=cols
        ),
    }
    if "funding_rate" in streams:
        panel["funding_rate"] = pd.DataFrame(
            rng.normal(0, 2e-4, (T, N)), index=idx, columns=cols
        )
    if "open_interest" in streams:
        panel["open_interest"] = pd.DataFrame(
            np.abs(rng.normal(1e8, 1e7, (T, N))), index=idx, columns=cols
        )
    if "basis" in streams:
        panel["basis"] = pd.DataFrame(
            rng.normal(0, 1e-3, (T, N)), index=idx, columns=cols
        )
    if "long_liquidations" in streams:
        panel["long_liquidations"] = pd.DataFrame(
            np.abs(rng.normal(1e5, 5e4, (T, N))), index=idx, columns=cols
        )
    if "short_liquidations" in streams:
        panel["short_liquidations"] = pd.DataFrame(
            np.abs(rng.normal(1e5, 5e4, (T, N))), index=idx, columns=cols
        )
    return panel


class _FakeDataClient:
    def __init__(self, panel):
        self._panel = panel

    def get_data(self):
        return self._panel

    def get_returns(self):
        return None

    def get_test_data(self):
        return None

    def get_test_returns(self):
        return None


class TestCryptoFeatureLibrary(unittest.TestCase):
    def test_every_template_evaluates_on_full_panel(self):
        panel = _synthetic_panel()
        # One window -> exactly one AST per template.
        single = build_crypto_feature_asts(panel, windows=(12,))
        self.assertEqual(len(single), len(CRYPTO_FEATURE_TEMPLATES))
        for ast in single:
            out = evaluate_ast(ast, panel)
            self.assertIsInstance(out, (pd.Series, pd.DataFrame), ast)
            self.assertEqual(out.shape, (T, N), ast)

        # Multiple windows -> window-independent templates are deduped.
        asts = build_crypto_feature_asts(panel, windows=(6, 12))
        self.assertEqual(len(asts), len({json.dumps(a, sort_keys=True) for a in asts}))
        self.assertGreaterEqual(len(asts), len(CRYPTO_FEATURE_TEMPLATES))

    def test_templates_filtered_by_available_streams(self):
        panel = _synthetic_panel(streams=("funding_rate",))
        self.assertEqual(crypto_streams_in(panel), {"funding_rate"})
        asts = build_crypto_feature_asts(panel, windows=(6,))
        funding_only = [
            t for t in CRYPTO_FEATURE_TEMPLATES if t.required == ("funding_rate",)
        ]
        self.assertEqual(len(asts), len(funding_only))
        for ast in asts:
            self.assertIn("funding_rate", str(ast))
            evaluate_ast(ast, panel)

    def test_no_crypto_streams_yields_no_seeds(self):
        panel = {k: v for k, v in _synthetic_panel().items()
                 if k not in CRYPTO_STREAM_NAMES}
        self.assertEqual(build_crypto_feature_asts(panel), [])
        self.assertEqual(crypto_streams_in(panel), set())

    def test_deterministic_limit(self):
        panel = _synthetic_panel()
        a = build_crypto_feature_asts(panel, rng=random.Random(3), limit=5)
        b = build_crypto_feature_asts(panel, rng=random.Random(3), limit=5)
        self.assertEqual(len(a), 5)
        self.assertEqual(a, b)

    def test_catalog_and_requirements(self):
        panel = _synthetic_panel(streams=("funding_rate", "open_interest"))
        catalog = crypto_feature_catalog(crypto_streams_in(panel))
        self.assertEqual(len(catalog), len(CRYPTO_FEATURE_TEMPLATES))
        by_name = {c["name"]: c for c in catalog}
        self.assertTrue(by_name["funding_zscore"]["applicable"])
        self.assertTrue(by_name["funding_oi_squeeze"]["applicable"])
        self.assertFalse(by_name["liq_pressure"]["applicable"])
        needed = streams_required_by_library()
        self.assertIn("funding_rate", needed["funding"])
        self.assertIn("long_liquidations", needed["liquidation"])

    def test_crypto_terminals_extension(self):
        panel = _synthetic_panel(streams=("funding_rate", "basis"))
        out = crypto_terminals(["close", "volume"], panel)
        self.assertEqual(out[:2], ["close", "volume"])
        self.assertIn("funding_rate", out)
        self.assertIn("basis", out)
        self.assertNotIn("open_interest", out)
        # idempotent
        self.assertEqual(crypto_terminals(out, panel), out)

    def test_describe_available_streams(self):
        self.assertIn("OHLCV", describe_available_streams(set()))
        text = describe_available_streams({"funding_rate"})
        self.assertIn("funding_rate", text)
        self.assertIn(CRYPTO_STREAM_SPECS["funding_rate"]["label"], text)


class TestCryptoStreamIO(unittest.TestCase):
    def test_alignment_is_past_only(self):
        src = pd.Series(
            [1.0, 2.0, 3.0],
            index=pd.to_datetime(["2025-01-01", "2025-01-03", "2025-01-05"]),
        )
        idx = pd.to_datetime(
            ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-06"]
        )
        aligned = align_series_to_index(src, idx)
        self.assertEqual(aligned.tolist(), [1.0, 1.0, 2.0, 2.0, 3.0])

    def test_basis_frame(self):
        perp = pd.Series(
            [100.0, 102.0], index=pd.to_datetime(["2025-01-01", "2025-01-02"])
        )
        spot = pd.Series(
            [99.0, 100.0], index=pd.to_datetime(["2025-01-01", "2025-01-02"])
        )
        df = build_basis_frame(perp, spot)
        self.assertEqual(list(df.columns), ["date", "basis"])
        self.assertAlmostEqual(df["basis"].iloc[0], 1 / 99, places=8)
        self.assertAlmostEqual(df["basis"].iloc[1], 0.02, places=8)

    def test_oi_period_selection(self):
        self.assertEqual(oi_period_for("1h"), "1h")
        self.assertEqual(oi_period_for("4h"), "4h")
        self.assertEqual(oi_period_for("1d"), "1d")
        self.assertEqual(oi_period_for("1m"), "5m")
        self.assertEqual(timeframe_to_timedelta("12h"), pd.Timedelta(hours=12))

    def test_write_load_roundtrip(self):
        tmp = Path(tempfile.mkdtemp())
        path = crypto_stream_path(
            tmp, "binance", "BTC/USDT:USDT", "1h", "futures", "funding_rate"
        )
        df = pd.DataFrame({
            "date": pd.to_datetime(["2025-01-01 00:00", "2025-01-01 08:00"]),
            "funding_rate": [0.0001, 0.0003],
        })
        _write_stream(df, path, "funding_rate")
        idx = pd.date_range("2025-01-01", periods=12, freq="1h")
        loaded = load_crypto_streams(
            tmp, "binance", "BTC/USDT:USDT", "1h", "futures", idx,
            streams=["funding_rate", "open_interest"],
        )
        self.assertIn("funding_rate", loaded)
        self.assertNotIn("open_interest", loaded)  # missing file -> skipped
        self.assertEqual(len(loaded["funding_rate"]), len(idx))
        self.assertAlmostEqual(loaded["funding_rate"].iloc[7], 0.0001)

    def test_materialize_user_supplied_missing_file(self):
        tmp = Path(tempfile.mkdtemp())
        status = materialize_crypto_streams(
            tmp, "binance", "BTC/USDT:USDT", "1h", "futures",
            pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01"),
            streams=["long_liquidations"],
            exchange=object(), spot_exchange=object(),
        )
        self.assertFalse(status["long_liquidations"]["ok"])
        self.assertIn("no free public endpoint", status["long_liquidations"]["error"])

    def test_materialize_user_supplied_present_file(self):
        tmp = Path(tempfile.mkdtemp())
        path = crypto_stream_path(
            tmp, "binance", "BTC/USDT:USDT", "1h", "futures", "long_liquidations"
        )
        _write_stream(
            pd.DataFrame({
                "date": pd.to_datetime(["2025-01-01"]),
                "long_liquidations": [12345.0],
            }),
            path, "long_liquidations",
        )
        status = materialize_crypto_streams(
            tmp, "binance", "BTC/USDT:USDT", "1h", "futures",
            pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01"),
            streams=["long_liquidations"],
            exchange=object(), spot_exchange=object(),
        )
        self.assertTrue(status["long_liquidations"]["ok"])
        self.assertEqual(status["long_liquidations"]["rows"], 1)


class TestGPIntegration(unittest.TestCase):
    def _config(self, **gp):
        return {
            "paradigm": "GP",
            "population_size": 10,
            "search_space": {
                "allowed_operators": [
                    "ts_zscore", "ts_delta", "div", "sub", "mul", "sign",
                    "add", "abs", "ts_ema", "ts_rank", "ts_momentum",
                    "ts_realized_vol",
                ],
            },
            "gp": gp,
        }

    def test_terminals_auto_extended(self):
        from core.miner.paradigms.gp_miner import GPFactorMiner

        panel = _synthetic_panel(streams=("funding_rate", "open_interest"))
        miner = GPFactorMiner(_FakeDataClient(panel), self._config())
        miner.initialize_search_space()
        self.assertIn("funding_rate", miner.terminals)
        self.assertIn("open_interest", miner.terminals)
        self.assertNotIn("basis", miner.terminals)

    def test_ohlcv_only_unchanged(self):
        from core.miner.paradigms.gp_miner import GPFactorMiner

        panel = {k: v for k, v in _synthetic_panel().items()
                 if k not in CRYPTO_STREAM_NAMES}
        miner = GPFactorMiner(_FakeDataClient(panel), self._config())
        miner.initialize_search_space()
        self.assertEqual(miner.terminals, ["close", "open", "high", "low", "volume"])
        self.assertEqual(miner._crypto_asts, [])
        pop = miner.generate_candidates()
        self.assertEqual(len(pop), 10)
        self.assertTrue(all(isinstance(c, FactorExpressionAST) for c in pop))

    def test_initial_population_is_crypto_seeded(self):
        from core.miner.paradigms.gp_miner import GPFactorMiner

        panel = _synthetic_panel()
        miner = GPFactorMiner(
            _FakeDataClient(panel), self._config(crypto_seed_ratio=1.0)
        )
        miner.initialize_search_space()
        pop = miner.generate_candidates()
        self.assertEqual(len(pop), 10)
        seeded = [
            c for c in pop
            if any(s in str(c.get_source()) for s in CRYPTO_STREAM_NAMES)
        ]
        self.assertEqual(len(seeded), 10, "ratio=1.0 must fill with crypto seeds")

    def test_seed_ratio_zero_means_no_crypto_seeds(self):
        from core.miner.paradigms.gp_miner import GPFactorMiner

        panel = _synthetic_panel()
        miner = GPFactorMiner(
            _FakeDataClient(panel),
            self._config(crypto_seed_ratio=0.0),  # terminals still extended
        )
        miner.initialize_search_space()
        self.assertEqual(miner._crypto_asts, [])


class TestLLMIntegration(unittest.TestCase):
    def test_prompt_lists_available_fields(self):
        from core.miner.paradigms.llm_miner import LLMFactorMiner

        panel = _synthetic_panel(streams=("funding_rate", "open_interest"))
        miner = LLMFactorMiner(_FakeDataClient(panel), {"llm_api_config": {}})
        miner.initialize_search_space()
        self.assertIn("Available data fields:", miner.system_prompt)
        self.assertIn("funding_rate", miner.system_prompt)
        self.assertIn("open_interest", miner.system_prompt)
        self.assertIn("Crypto-native field semantics", miner.system_prompt)

    def test_prompt_without_crypto_streams(self):
        from core.miner.paradigms.llm_miner import LLMFactorMiner

        panel = {k: v for k, v in _synthetic_panel().items()
                 if k not in CRYPTO_STREAM_NAMES}
        miner = LLMFactorMiner(_FakeDataClient(panel), {"llm_api_config": {}})
        miner.initialize_search_space()
        self.assertIn("Available data fields:", miner.system_prompt)
        self.assertNotIn("Crypto-native field semantics", miner.system_prompt)


class TestRealClientAttach(unittest.TestCase):
    def test_attach_crypto_streams(self):
        from core.data_feed.real_client import RealDataClient

        tmp = Path(tempfile.mkdtemp())
        path = crypto_stream_path(
            tmp / "data", "binance", "BTC/USDT:USDT", "1h", "futures", "funding_rate"
        )
        _write_stream(
            pd.DataFrame({
                "date": pd.date_range("2025-01-01", periods=4, freq="8h"),
                "funding_rate": [1e-4, 2e-4, 3e-4, 4e-4],
            }),
            path, "funding_rate",
        )
        idx = pd.date_range("2025-01-01", periods=24, freq="1h")
        df = pd.DataFrame({"close": np.linspace(100, 101, len(idx))}, index=idx)

        client = RealDataClient.__new__(RealDataClient)
        client.exchange = "binance"
        client.instrument_type = "futures"
        client.timeframe = "1h"
        client.crypto_streams = ["funding_rate"]
        # The attach step now reads the client's resolved root instead of
        # hardcoding ``cwd/data``, so the streams and the OHLCV panel cannot
        # come from different directories.
        client.data_root = tmp / "data"

        out = client._attach_crypto_streams(df, "BTC/USDT:USDT")

        self.assertIn("funding_rate", out.columns)
        self.assertAlmostEqual(out["funding_rate"].iloc[0], 1e-4)
        # 8h grid forward-filled onto 1h bars
        self.assertAlmostEqual(out["funding_rate"].iloc[10], 2e-4)
        # Missing dir must not raise and must leave the frame unchanged.
        client.crypto_streams = ["open_interest"]
        same = client._attach_crypto_streams(df.copy(), "BTC/USDT:USDT")
        self.assertNotIn("open_interest", same.columns)


class TestFundingPagination(unittest.TestCase):
    """Funding settles at exchange-specific intervals; the downloader must
    capture *every* settlement regardless of the grid (1h/2h/4h/8h/12h)."""

    @staticmethod
    def _gen(start: pd.Timestamp, hours: int, interval_h: int = 1) -> pd.DataFrame:
        times = [start + pd.Timedelta(hours=interval_h) * i for i in range(hours // interval_h + 1)]
        return pd.DataFrame({
            "timestamp": [int(pd.Timestamp(t).timestamp() * 1000) for t in times],
            "fundingRate": [0.0001] * len(times),
        })

    def _make_exchange(self, rows, per_call_cap=None, with_market_info=None):
        captured = []

        def fetch(since, limit):
            out = [r for r in rows if r["timestamp"] >= (since or 0)]
            captured.append((since, limit))
            if per_call_cap is not None:
                return out[:per_call_cap]
            return out[:limit]

        class _Exch:
            def fetch_funding_rate_history(self, symbol, since=None, limit=None):
                return fetch(since, limit)

            def market(self, symbol):
                if with_market_info is None:
                    raise KeyError("no market info (simulate unsupported exchange)")
                return {"info": with_market_info}

        return _Exch(), captured

    def test_download_captures_all_1h_settlements(self):
        start = pd.Timestamp("2025-01-01").tz_localize(None)
        df = self._gen(start, hours=240, interval_h=1)  # 241 hourly settlements
        exch, _ = self._make_exchange(df.to_dict("records"), per_call_cap=3)  # force many pages
        out = download_funding_rate(exch, "BTC/USDT:USDT", start, start + pd.Timedelta(days=21), "1d")
        self.assertEqual(len(out), 241)
        diffs = out["date"].diff().dropna().dt.total_seconds() / 3600.0
        self.assertTrue((diffs == 1.0).all(), "every 1h settlement present, no gap")

    def test_download_captures_all_4h_settlements(self):
        start = pd.Timestamp("2025-01-01").tz_localize(None)
        df = self._gen(start, hours=240, interval_h=4)  # 61 four-hourly settlements
        exch, _ = self._make_exchange(df.to_dict("records"), per_call_cap=3)
        out = download_funding_rate(exch, "BTC/USDT:USDT", start, start + pd.Timedelta(days=21), "1d")
        self.assertEqual(len(out), 61)
        diffs = out["date"].diff().dropna().dt.total_seconds() / 3600.0
        self.assertTrue((diffs == 4.0).all())

    def test_old_8h_step_would_skip_rows(self):
        # Demonstrates the bug the fix removes: a coarse 8h floor with a small
        # per-call limit silently drops settlements that fall inside the jump.
        start = pd.Timestamp("2025-01-01").tz_localize(None)
        df = self._gen(start, hours=48, interval_h=1)  # 49 hourly settlements
        rows = df.to_dict("records")

        def fetch(since, limit):
            return [r for r in rows if r["timestamp"] >= (since or 0)][:limit]

        old = _paginate(fetch, start, start + pd.Timedelta(days=5), limit=3,
                        step=pd.Timedelta(hours=8), label="old")
        self.assertLess(len(old), 49, "old 8h floor skips settlements")

        new = _paginate(fetch, start, start + pd.Timedelta(days=5), limit=3,
                        step=pd.Timedelta(seconds=1), label="new")
        self.assertEqual(len(new), 49, "1s floor captures every settlement")

    def test_detected_interval_logged(self):
        start = pd.Timestamp("2025-01-01").tz_localize(None)
        df = self._gen(start, hours=24, interval_h=4)
        # Binance exposes fundingIntervalHours=4 for some pairs.
        exch, _ = self._make_exchange(df.to_dict("records"), with_market_info={"fundingIntervalHours": 4})
        self.assertEqual(_funding_settlement_interval(exch, "X/Y:USDT"), pd.Timedelta(hours=4))
        out = download_funding_rate(exch, "X/Y:USDT", start, start + pd.Timedelta(days=3), "1d")
        self.assertEqual(len(out), 7)

    def test_unsupported_exchange_has_no_interval(self):
        start = pd.Timestamp("2025-01-01").tz_localize(None)
        df = self._gen(start, hours=24, interval_h=1)
        exch, _ = self._make_exchange(df.to_dict("records"))  # market() raises
        self.assertIsNone(_funding_settlement_interval(exch, "X/Y:USDT"))
        out = download_funding_rate(exch, "X/Y:USDT", start, start + pd.Timedelta(days=3), "1d")
        self.assertEqual(len(out), 25)


if __name__ == "__main__":
    unittest.main()
