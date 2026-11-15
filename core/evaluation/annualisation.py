"""Data-driven annualisation for Sharpe / information ratios.

Why this module exists
----------------------
The evaluation stack inherited ``252`` (US equity trading days) from the
original implementation. FactorMiner trades crypto, which is 7x24: a daily bar
is 365 periods per year, an hourly bar 8760. Keeping 252 mislabels every
annualised Sharpe by ``sqrt(365/252) = 1.203``, i.e. ~20% too low, and makes
our reports incomparable with crypto-native frameworks (``crypto-factor-lab``
uses ``24*365 = 8760`` for 1h bars).

Scope note (verified against the source before this change): ``annualisation``
only scales *displayed* annualised Sharpes. The promotion gates use a
per-period Sharpe (``cost_adj_return / std_return``), HAC t-statistics, BH-FDR
p-values and a DSR benchmark on the ``1/sqrt(n_obs-1)`` scale. None of those
consume ``annualisation``. Changing this value therefore cannot change a
factor's pass/fail verdict — see ``tests/test_annualisation.py`` for the
regression assertion that pins this invariant.

Resolution order (highest priority first)
----------------------------------------
1. ``config["fitness"]["annualisation"]``  — explicit override
2. ``config["evaluation"]["annualisation"]`` / ``["gates"]["annualisation"]``
3. ``config["data_feeds"]["timeframe"]`` (or an explicit ``timeframe`` arg)
4. median spacing of a DatetimeIndex
5. ``DEFAULT_ANNUALISATION`` = 365 (crypto daily bars)

All calendar conventions use 365 days/year and 7x24 trading. Equity-style 252
is still available by setting the override explicitly, which is how historical
published numbers (e.g. the 2.35 DSR bar in ``p1_gate_upgrades.md``) stay
reproducible.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

# 365 * 24 * 60 = 525_600 minutes per year on a 7x24 market.
MINUTES_PER_YEAR = 365 * 24 * 60
DEFAULT_ANNUALISATION = 365.0

_TIMEFRAME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$")
_UNIT_MINUTES = {
    "s": 1 / 60, "sec": 1 / 60, "second": 1 / 60, "seconds": 1 / 60,
    "m": 1.0, "min": 1.0, "minute": 1.0, "minutes": 1.0,
    "h": 60.0, "hr": 60.0, "hour": 60.0, "hours": 60.0,
    "d": 1440.0, "day": 1440.0, "days": 1440.0,
    "w": 10080.0, "wk": 10080.0, "week": 10080.0, "weeks": 10080.0,
}

# CCXT / pandas use case to distinguish minutes from months: "1m" is one
# minute, "1M" is one MONTH. Lower-casing everything made ``1M`` resolve to
# 525600 periods/year instead of ~12, inflating annualised Sharpe by ~209x.
_CASE_SENSITIVE_UNIT_MINUTES = {
    "M": MINUTES_PER_YEAR / 12.0,   # 43800 minutes -> 12 periods/year
    "Y": float(MINUTES_PER_YEAR),
    "y": float(MINUTES_PER_YEAR),
}


def timeframe_minutes(timeframe: Any) -> Optional[float]:
    """Minutes in one bar for strings like ``5m``, ``1h``, ``4H``, ``1d``.

    Case matters: ``1m`` is one minute, ``1M`` is one month (CCXT/pandas
    convention). Returns ``None`` for anything unparseable so callers can fall
    back to the index spacing instead of silently guessing.
    """
    if timeframe is None:
        return None
    if isinstance(timeframe, (int, float)):
        # Bare numbers are interpreted as minutes (pandas-style offsets).
        val = float(timeframe)
        return val if val > 0 else None
    match = _TIMEFRAME_RE.match(str(timeframe))
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    minutes = _CASE_SENSITIVE_UNIT_MINUTES.get(unit)
    if minutes is None:
        minutes = _UNIT_MINUTES.get(unit.lower())
    if minutes is None or value <= 0:
        return None
    return value * minutes


def index_minutes(index: Any) -> Optional[float]:
    """Median spacing (in minutes) of a DatetimeIndex, robust to gaps/duplicates."""
    if index is None or not isinstance(index, pd.DatetimeIndex) or index.size < 3:
        return None
    try:
        diffs = index.to_series().diff().dropna().dt.total_seconds() / 60.0
        diffs = diffs[diffs > 0]
        if diffs.empty:
            return None
        return float(diffs.median())
    except (TypeError, AttributeError, ValueError):
        return None


def infer_annualisation(
    timeframe: Any = None,
    index: Any = None,
) -> float:
    """Periods per year implied by a timeframe string or a DatetimeIndex.

    ``1d -> 365``, ``4h -> 2190``, ``1h -> 8760``, ``5m -> 105120``.
    Falls back to ``DEFAULT_ANNUALISATION`` when neither source is usable.
    """
    minutes = timeframe_minutes(timeframe)
    if minutes is None:
        minutes = index_minutes(index)
    if not minutes or minutes <= 0:
        return DEFAULT_ANNUALISATION
    return float(MINUTES_PER_YEAR / minutes)


def _explicit(config: dict) -> Optional[float]:
    for container in (
        config.get("fitness") or {},
        config.get("evaluation") or {},
        (config.get("evaluation") or {}).get("gates") or {},
        config.get("data_feeds") or {},
    ):
        if not isinstance(container, dict):
            continue
        raw = container.get("annualisation")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def resolve_annualisation(
    config: Optional[dict] = None,
    timeframe: Any = None,
    index: Any = None,
) -> float:
    """Resolve periods/year from config + runtime hints.

    ``config`` is the full run config (the same dict the evaluator/director
    hold), so ``data_feeds.timeframe`` participates automatically. An explicit
    ``timeframe`` argument wins over the config's timeframe but *not* over an
    explicit ``annualisation`` override.
    """
    config = config or {}
    explicit = _explicit(config)
    if explicit is not None:
        return explicit
    tf = timeframe
    if tf is None:
        tf = (config.get("data_feeds") or {}).get("timeframe")
    return infer_annualisation(tf, index)
