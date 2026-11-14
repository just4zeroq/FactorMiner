"""Pre-mining data quality gate.

``DataHealthChecker`` runs on the *download* path, so a panel that was already on
disk never gets inspected before a mining run. That is how 31 duplicated 08:00
bars in the BTC daily file survived into a strict holdout window: the download
happened long ago, and nothing looked at the file again.

This module validates the assembled mining panel (and its forward returns) right
before the first evaluation, so the failure is loud at the start of a run instead
of silent inside a RankIC number.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_QUALITY_CFG = {
    "check_on_load": True,
    "strict": False,          # True => raise instead of warn on critical issues
    "min_coverage": 0.90,     # fraction of non-NaN cells in the factor panel
    "max_duplicate_ratio": 0.0,
    "max_abs_return": 1.0,    # |forward return| above this is a data error, not alpha
    "max_gap_ratio": 0.05,    # share of expected bars that are missing
}


class DataQualityError(RuntimeError):
    """Raised when ``data_quality.strict`` is on and the panel is unusable."""


class PanelQualityReport:
    def __init__(self) -> None:
        self.critical: list[str] = []
        self.warnings: list[str] = []
        self.stats: dict[str, Any] = {}

    @property
    def ok(self) -> bool:
        return not self.critical

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "critical": list(self.critical),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
        }

    def log(self) -> None:
        if self.critical:
            logger.error("Data quality gate FAILED: %s", "; ".join(self.critical))
        for warning in self.warnings:
            logger.warning("Data quality: %s", warning)
        if self.ok and not self.warnings:
            logger.info("Data quality gate passed: %s", self.stats)


def _expected_interval(index: pd.DatetimeIndex, timeframe: str) -> Optional[pd.Timedelta]:
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    text = (timeframe or "").strip().lower()
    if not text:
        return None
    if text[-1] not in units:
        return None
    try:
        amount = int(text[:-1] or 1)
    except ValueError:
        return None
    return pd.Timedelta(**{units[text[-1]]: amount})


def _check_index(index, timeframe: str, report: PanelQualityReport, cfg: dict) -> None:
    if not isinstance(index, pd.DatetimeIndex):
        report.warnings.append("panel index is not a DatetimeIndex; skipping time checks")
        return

    report.stats["bars"] = int(len(index))
    if len(index) == 0:
        report.critical.append("panel has no rows")
        return

    report.stats["start"] = str(index.min().date())
    report.stats["end"] = str(index.max().date())

    duplicates = int(index.duplicated().sum())
    report.stats["duplicate_timestamps"] = duplicates
    if duplicates:
        ratio = duplicates / max(1, len(index))
        message = f"{duplicates} duplicate timestamp(s) in the panel"
        if ratio > float(cfg.get("max_duplicate_ratio", 0.0)):
            report.critical.append(message + f" ({ratio:.2%} of bars)")
        else:
            report.warnings.append(message)

    if not index.is_monotonic_increasing:
        report.critical.append("panel index is not sorted ascending")

    expected = _expected_interval(index, timeframe)
    if expected is None:
        return

    unique_sorted = index.drop_duplicates().sort_values()
    gaps = unique_sorted.to_series().diff().dropna()
    if gaps.empty:
        return
    median_gap = gaps.median()
    report.stats["median_interval"] = str(median_gap)
    report.stats["expected_interval"] = str(expected)
    if median_gap != expected:
        report.warnings.append(
            f"median bar interval {median_gap} differs from configured timeframe {expected}"
        )

    span = unique_sorted[-1] - unique_sorted[0]
    expected_bars = int(span / expected) + 1
    missing = max(0, expected_bars - len(unique_sorted))
    gap_ratio = missing / max(1, expected_bars)
    report.stats["expected_bars"] = expected_bars
    report.stats["missing_bars"] = missing
    report.stats["gap_ratio"] = round(gap_ratio, 4)
    if gap_ratio > float(cfg.get("max_gap_ratio", DEFAULT_QUALITY_CFG["max_gap_ratio"])):
        report.warnings.append(
            f"{missing} of {expected_bars} expected bars are missing ({gap_ratio:.1%})"
        )


def validate_panel(
    data: Any,
    returns: Any = None,
    timeframe: str = "1d",
    quality_cfg: Optional[dict] = None,
) -> PanelQualityReport:
    """Inspect the mining panel and its forward returns.

    Critical issues make the panel unusable (duplicate bars, unsorted index, no
    rows). Everything else — coverage, gaps, extreme returns — is a warning that
    is surfaced in the run log and stored on the client for the report.
    """
    cfg = {**DEFAULT_QUALITY_CFG, **(quality_cfg or {})}
    report = PanelQualityReport()

    if data is None:
        report.critical.append("mining panel is None")
        return report

    if isinstance(data, dict):
        frames = {key: value for key, value in data.items() if isinstance(value, (pd.DataFrame, pd.Series))}
        report.stats["fields"] = len(frames)
        if not frames:
            report.critical.append("mining panel contains no tabular fields")
            return report
        # Validate the widest field as the reference index.
        reference = max(frames.values(), key=lambda frame: len(frame))
        _check_index(reference.index, timeframe, report, cfg)
        total_cells = 0
        missing_cells = 0
        for name, frame in frames.items():
            total_cells += int(frame.size)
            missing_cells += int(frame.isna().sum().sum())
            if isinstance(frame, pd.DataFrame):
                empty_columns = [str(col) for col in frame.columns if frame[col].isna().all()]
                if empty_columns:
                    report.warnings.append(
                        f"field '{name}' has {len(empty_columns)} all-NaN column(s): "
                        f"{', '.join(empty_columns[:5])}"
                    )
        if total_cells:
            coverage = 1.0 - missing_cells / total_cells
            report.stats["coverage"] = round(coverage, 4)
            if coverage < float(cfg.get("min_coverage", DEFAULT_QUALITY_CFG["min_coverage"])):
                report.warnings.append(f"panel coverage is only {coverage:.1%}")
    elif isinstance(data, (pd.DataFrame, pd.Series)):
        _check_index(data.index, timeframe, report, cfg)
        total_cells = int(data.size)
        coverage = 1.0 - float(data.isna().sum().sum()) / max(1, total_cells)
        report.stats["coverage"] = round(coverage, 4)
        if coverage < float(cfg.get("min_coverage", DEFAULT_QUALITY_CFG["min_coverage"])):
            report.warnings.append(f"panel coverage is only {coverage:.1%}")
        if isinstance(data, pd.DataFrame):
            report.stats["assets"] = int(data.shape[1])
            empty_columns = [str(col) for col in data.columns if data[col].isna().all()]
            if empty_columns:
                report.warnings.append(
                    f"{len(empty_columns)} asset(s) are entirely empty: "
                    f"{', '.join(empty_columns[:5])}"
                )
            constant = [
                str(col) for col in data.columns
                if data[col].nunique(dropna=True) <= 1 and not data[col].isna().all()
            ]
            if constant:
                report.warnings.append(
                    f"{len(constant)} asset(s) are constant: {', '.join(constant[:5])}"
                )
    else:
        report.warnings.append(f"unrecognised panel type: {type(data).__name__}")

    if returns is not None and not (isinstance(returns, (pd.DataFrame, pd.Series)) and returns.empty):
        _check_returns(returns, report, cfg)

    return report


def _check_returns(returns: Any, report: PanelQualityReport, cfg: dict) -> None:
    values = returns.to_numpy(dtype=float, na_value=np.nan) if hasattr(returns, "to_numpy") else None
    if values is None:
        return
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        report.critical.append("forward returns contain no finite values")
        return
    report.stats["return_obs"] = int(finite.size)
    limit = float(cfg.get("max_abs_return", DEFAULT_QUALITY_CFG["max_abs_return"]))
    extreme = int((np.abs(finite) > limit).sum())
    report.stats["extreme_returns"] = extreme
    if extreme:
        report.warnings.append(
            f"{extreme} forward return(s) exceed {limit:.0%} in absolute value "
            "(split/bad print/instrument change?)"
        )
    if isinstance(returns, pd.DataFrame):
        all_nan = bool(returns.isna().all().all())
    else:
        all_nan = bool(returns.isna().all())
    if all_nan:
        report.critical.append("forward returns are entirely NaN")


def enforce(report: PanelQualityReport, quality_cfg: Optional[dict] = None) -> None:
    """Log the report and raise when strict mode rejects the panel."""
    cfg = {**DEFAULT_QUALITY_CFG, **(quality_cfg or {})}
    report.log()
    if report.critical and cfg.get("strict"):
        raise DataQualityError(
            "Refusing to mine on a broken panel: " + "; ".join(report.critical)
            + " (set data_quality.strict=false to downgrade this to a warning)"
        )
