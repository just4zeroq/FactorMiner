"""Diversity filter: hard (source-hash) and soft (value-correlation) dedup.

The soft layer exists because hash dedup is blind to the same signal written
two ways. `rank(close)` and `zscore(close)` have different sources but identical
cross-sectional ordering, so they are the same hypothesis and must not each
consume a slot in the multiple-testing denominator.
"""

import unittest

import numpy as np
import pandas as pd

from core.miner.paradigms.base import DiversityFilter, factor_signature, signature_correlation


class FakeFactor:
    def __init__(self, name: str, values):
        self.name = name
        self.values = values
        self.logic_hash = ""

    def get_source(self):
        return self.name

    def compute(self, data):
        return self.values

    def __repr__(self):
        return f"FakeFactor({self.name})"


def _panel(seed: int = 0, n_days: int = 120, n_assets: int = 8) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        rng.standard_normal((n_days, n_assets)),
        index=pd.date_range("2025-01-01", periods=n_days, freq="D"),
        columns=[f"A{i}" for i in range(n_assets)],
    )


class SignatureTests(unittest.TestCase):
    def test_rank_signature_ignores_monotone_scaling(self):
        panel = _panel(1)
        a = factor_signature(panel)
        b = factor_signature(panel * 1000 + 7)
        self.assertIsNotNone(a)
        self.assertGreater(signature_correlation(a, b), 0.999)

    def test_signature_handles_series_and_none(self):
        series = pd.Series(np.linspace(0, 1, 50))
        self.assertEqual(factor_signature(series).shape, (50,))
        self.assertIsNone(factor_signature(None))
        self.assertIsNone(factor_signature(pd.DataFrame()))

    def test_correlation_requires_overlap(self):
        a = np.array([1.0, 2.0, 3.0])
        b = np.array([1.0, 2.0, 3.0])
        self.assertIsNone(signature_correlation(a, b))  # fewer than 50 joint points


class HardDedupTests(unittest.TestCase):
    def test_identical_source_is_dropped(self):
        values = _panel(2)
        first = FakeFactor("ts_mean(close/ TS_N(5))", values)
        second = FakeFactor("ts_mean(close/ TS_N(5))", values)
        result = DiversityFilter(value_threshold=0.0).filter_redundant([first, second], None)
        self.assertEqual(result, [first])

    def test_hashes_accumulate_across_calls(self):
        values = _panel(3)
        flt = DiversityFilter(value_threshold=0.0)
        first = FakeFactor("rank(close)", values)
        flt.filter_redundant([first], None)
        again = FakeFactor("rank(close)", values)
        self.assertEqual(flt.filter_redundant([again], None), [])


class SoftDedupTests(unittest.TestCase):
    def test_same_signal_different_source_is_dropped(self):
        panel = _panel(4)
        # Same cross-sectional ordering, different expression text.
        first = FakeFactor("rank(close)", panel)
        clone = FakeFactor("zscore(close)", panel.rank(axis=1))
        flt = DiversityFilter(value_threshold=0.99)
        kept = flt.filter_redundant([first, clone], None)
        self.assertEqual(kept, [first])
        self.assertEqual(flt.value_rejected, 1)
        self.assertGreater(flt.max_observed_corr, 0.99)

    def test_inverted_signal_is_also_redundant(self):
        panel = _panel(5)
        first = FakeFactor("a", panel)
        inverted = FakeFactor("b", -panel)
        flt = DiversityFilter(value_threshold=0.99)
        kept = flt.filter_redundant([first, inverted], None)
        self.assertEqual(kept, [first])

    def test_independent_signals_survive(self):
        first = FakeFactor("a", _panel(6))
        second = FakeFactor("b", _panel(7))
        flt = DiversityFilter(value_threshold=0.99)
        kept = flt.filter_redundant([first, second], None)
        self.assertEqual(len(kept), 2)
        self.assertEqual(flt.value_rejected, 0)

    def test_threshold_zero_disables_soft_layer(self):
        panel = _panel(8)
        first = FakeFactor("a", panel)
        clone = FakeFactor("b", panel)
        flt = DiversityFilter(value_threshold=0.0)
        kept = flt.filter_redundant([first, clone], None)
        self.assertEqual(len(kept), 2)
        self.assertEqual(flt.summary()["archive_size"], 0)

    def test_elites_are_kept_but_seed_the_archive(self):
        panel = _panel(9)
        elite = FakeFactor("elite", panel)
        clone = FakeFactor("clone", panel)
        flt = DiversityFilter(value_threshold=0.99)
        kept = flt.filter_redundant([elite, clone], None, preserved_candidates=[elite])
        self.assertEqual(kept, [elite])
        self.assertEqual(flt.value_rejected, 1)

    def test_uncomputable_candidate_is_kept(self):
        class Broken(FakeFactor):
            def compute(self, data):
                raise RuntimeError("no data")

        healthy = FakeFactor("healthy", _panel(10))
        broken = Broken("broken", None)
        flt = DiversityFilter(value_threshold=0.99)
        kept = flt.filter_redundant([healthy, broken], None)
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
