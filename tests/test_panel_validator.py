"""Pre-mining panel quality gate.

Regression target: the duplicated 08:00 BTC bars that slipped into a strict
holdout. A panel with duplicated timestamps must be flagged as critical before
the first evaluation, not discovered months later.
"""

import unittest

import numpy as np
import pandas as pd

from core.data_feed.panel_validator import (
    DataQualityError,
    enforce,
    validate_panel,
)


def _panel(n=200, assets=4, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        rng.standard_normal((n, assets)),
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
        columns=[f"A{i}" for i in range(assets)],
    )


def _returns(panel: pd.DataFrame) -> pd.DataFrame:
    return panel / 100.0


class PanelValidationTests(unittest.TestCase):
    def test_clean_panel_passes(self):
        panel = _panel()
        report = validate_panel(panel, _returns(panel), timeframe="1d")
        self.assertTrue(report.ok)
        self.assertEqual(report.critical, [])
        self.assertGreater(report.stats["coverage"], 0.99)

    def test_duplicate_timestamps_are_critical(self):
        panel = _panel()
        duplicated = pd.concat([panel, panel.iloc[[10]]]).sort_index()
        report = validate_panel(duplicated, None, timeframe="1d")
        self.assertFalse(report.ok)
        self.assertTrue(any("duplicate" in issue for issue in report.critical))
        self.assertEqual(report.stats["duplicate_timestamps"], 1)

    def test_unsorted_index_is_critical(self):
        panel = _panel()
        shuffled = panel.iloc[np.random.default_rng(1).permutation(len(panel))]
        report = validate_panel(shuffled, None, timeframe="1d")
        self.assertFalse(report.ok)
        self.assertTrue(any("sorted" in issue for issue in report.critical))

    def test_gap_is_a_warning_not_a_blocker(self):
        panel = _panel(n=200)
        holed = panel.drop(panel.index[50:70])
        report = validate_panel(holed, None, timeframe="1d")
        self.assertTrue(report.ok)
        self.assertEqual(report.stats["missing_bars"], 20)
        self.assertTrue(any("missing" in warning for warning in report.warnings))

    def test_low_coverage_is_flagged(self):
        panel = _panel(n=100)
        sparse = panel.copy()
        sparse.iloc[:, 0] = np.nan
        sparse.iloc[::2, 1] = np.nan
        report = validate_panel(sparse, None, timeframe="1d")
        self.assertTrue(any("coverage" in warning for warning in report.warnings))

    def test_all_nan_asset_is_reported(self):
        panel = _panel(n=100)
        panel["DEAD"] = np.nan
        report = validate_panel(panel, None, timeframe="1d")
        self.assertTrue(any("DEAD" in warning for warning in report.warnings))

    def test_extreme_returns_are_reported(self):
        panel = _panel(n=50)
        returns = _returns(panel)
        returns.iloc[3, 1] = 5.0  # +500% single period
        report = validate_panel(panel, returns, timeframe="1d")
        self.assertEqual(report.stats["extreme_returns"], 1)
        self.assertTrue(any("exceed" in warning for warning in report.warnings))

    def test_strict_mode_raises_on_critical(self):
        panel = _panel()
        duplicated = pd.concat([panel, panel.iloc[[0]]]).sort_index()
        report = validate_panel(duplicated, None, timeframe="1d")
        with self.assertRaises(DataQualityError):
            enforce(report, {"strict": True})

    def test_strict_mode_passes_clean_panel(self):
        panel = _panel()
        enforce(validate_panel(panel, None, timeframe="1d"), {"strict": True})

    def test_dict_panel_is_supported(self):
        panel = _panel()
        report = validate_panel(
            {"close": panel, "volume": panel * 10},
            _returns(panel),
            timeframe="1d",
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.stats["fields"], 2)

    def test_series_panel_is_supported(self):
        series = _panel(n=120, assets=1).iloc[:, 0]
        report = validate_panel(series, series / 100.0, timeframe="1d")
        self.assertTrue(report.ok)

    def test_none_panel_is_critical(self):
        report = validate_panel(None, None, timeframe="1d")
        self.assertFalse(report.ok)

    def test_timeframe_parsing(self):
        from core.data_feed.panel_validator import _expected_interval

        self.assertEqual(_expected_interval(pd.DatetimeIndex([]), "1d"), pd.Timedelta(days=1))
        self.assertEqual(_expected_interval(pd.DatetimeIndex([]), "4h"), pd.Timedelta(hours=4))
        self.assertEqual(_expected_interval(pd.DatetimeIndex([]), "15m"), pd.Timedelta(minutes=15))
        self.assertIsNone(_expected_interval(pd.DatetimeIndex([]), "weird"))


if __name__ == "__main__":
    unittest.main()
