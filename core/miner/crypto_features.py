"""Crypto-native feature template library.

Standard OHLCV operators (``ts_mean``, ``ts_zscore``, ...) are market-agnostic.
What makes a factor *crypto-native* is the **stream** it reads: perpetual
futures publish funding, open interest, basis and liquidation tapes that have no
equity analogue.  This module curates the economically meaningful ways to turn
those tapes into normalized, stationary features, expressed as ASTs over the
existing operator library — no new operators required.

Design notes
------------
* Every template is a pure ``window -> AST`` builder.  ASTs reference crypto
  streams by their canonical names (see
  :data:`core.data_feed.crypto_streams.CRYPTO_STREAM_NAMES`) and throwaway
  constants only.
* ``required`` lists the *crypto* streams a template needs.  OHLCV streams
  (``close``/``volume``) are assumed to exist whenever mining runs at all.
* ``build_crypto_feature_asts`` filters by what is actually present in the data
  panel, so enabling the library on a machine without funding/OI files simply
  yields fewer seeds instead of failing.

The templates encode standard crypto microstructure priors:

* **funding** — extreme positive funding means longs are crowded and paying to
  stay long (contrarian short signal); funding *changes* lead positioning.
* **open interest** — price up with OI up is new leverage (trend); price up
  with OI down is short covering (weak trend).
* **basis** — a rich perp premium is a carry/euphoria gauge.
* **liquidations** — forced flows mark local extremes; net imbalance is the
  cleanest single read.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple,
)

import pandas as pd

from core.data_feed.crypto_streams import CRYPTO_STREAM_NAMES

# Human-readable metadata used by docs, the LLM prompt and the catalog.
CRYPTO_STREAM_SPECS: Dict[str, Dict[str, str]] = {
    "funding_rate": {
        "label": "资金费率",
        "unit": "decimal per settlement (0.0001 = 1bp)",
        "native": "8h on Binance",
        "rationale": "Long/short crowding gauge; extremes mean-revert.",
    },
    "open_interest": {
        "label": "未平仓合约量",
        "unit": "quote notional (preferred) or base contracts",
        "native": "5m..1d grid",
        "rationale": "Leverage gauge; divergence from price separates new money from short covering.",
    },
    "basis": {
        "label": "基差",
        "unit": "(perp - spot) / spot",
        "native": "bar grid (derived)",
        "rationale": "Perp premium = carry and sentiment; rich basis precedes de-leveraging.",
    },
    "long_liquidations": {
        "label": "多头爆仓量",
        "unit": "quote notional",
        "native": "event / vendor supplied",
        "rationale": "Forced selling marks capitulation lows.",
    },
    "short_liquidations": {
        "label": "空头爆仓量",
        "unit": "quote notional",
        "native": "event / vendor supplied",
        "rationale": "Forced buying marks squeeze highs.",
    },
}

EPS = 1e-9


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #
def _un(op: str, left: Any, **params: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {"op": op, "left": left}
    node.update(params)
    return node


def _bin(op: str, left: Any, right: Any, **params: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {"op": op, "left": left, "right": right}
    node.update(params)
    return node


def _safe_denom(expr: Any) -> Dict[str, Any]:
    return _bin("add", _un("abs", expr), EPS)


def _liq_pressure_ast() -> Dict[str, Any]:
    """Net liquidation imbalance in [-1, 1]: longs minus shorts, normalized."""
    total = _bin("add", "long_liquidations", "short_liquidations")
    net = _bin("sub", "long_liquidations", "short_liquidations")
    return _bin("div", net, _bin("add", total, EPS))


# --------------------------------------------------------------------------- #
# Template model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CryptoFeatureTemplate:
    """One parameterized crypto-native feature.

    ``builder`` receives a window (in bars) and returns an AST dict that
    :func:`core.miner.operator_runtime.evaluate_ast` can run directly.
    """

    name: str
    category: str
    required: Tuple[str, ...]
    builder: Callable[[int], Dict[str, Any]]
    rationale: str = ""

    def build(self, window: int) -> Dict[str, Any]:
        return self.builder(int(window))

    def applicable(self, streams_present: Iterable[str]) -> bool:
        present = set(streams_present)
        return all(s in present for s in self.required)


def _tpl(
    name: str,
    category: str,
    required: Sequence[str],
    builder: Callable[[int], Dict[str, Any]],
    rationale: str,
) -> CryptoFeatureTemplate:
    return CryptoFeatureTemplate(
        name=name,
        category=category,
        required=tuple(dict.fromkeys(required)),
        builder=builder,
        rationale=rationale,
    )


# --------------------------------------------------------------------------- #
# The library
# --------------------------------------------------------------------------- #
CRYPTO_FEATURE_TEMPLATES: List[CryptoFeatureTemplate] = [
    # ---------------------------- funding ---------------------------------- #
    _tpl(
        "funding_zscore", "funding", ["funding_rate"],
        lambda w: _un("ts_zscore", "funding_rate", window=w),
        "Funding extremity relative to its own history (crowding).",
    ),
    _tpl(
        "funding_rank", "funding", ["funding_rate"],
        lambda w: _un("ts_rank", "funding_rate", window=w),
        "Percentile of current funding in the trailing window.",
    ),
    _tpl(
        "funding_delta", "funding", ["funding_rate"],
        lambda w: _un("ts_delta", "funding_rate", window=1),
        "One-settlement change in funding (positioning impulse).",
    ),
    _tpl(
        "funding_rel_change", "funding", ["funding_rate"],
        lambda w: _bin(
            "div",
            _un("ts_delta", "funding_rate", window=1),
            _safe_denom("funding_rate"),
        ),
        "Scale-free funding change.",
    ),
    _tpl(
        "funding_ema_gap", "funding", ["funding_rate"],
        lambda w: _bin("sub", "funding_rate", _un("ts_ema", "funding_rate", window=w)),
        "Deviation of funding from its smoothed level.",
    ),
    _tpl(
        "funding_sign", "funding", ["funding_rate"],
        lambda w: _un("sign", "funding_rate"),
        "Regime sign: +1 longs pay, -1 shorts pay.",
    ),
    _tpl(
        "funding_vol_adj", "funding", ["funding_rate"],
        lambda w: _bin(
            "div",
            _un("ts_zscore", "funding_rate", window=w),
            _safe_denom(_un("ts_realized_vol", "close", window=w)),
        ),
        "Funding extremity per unit of realized volatility.",
    ),
    # -------------------------- open interest ------------------------------ #
    _tpl(
        "oi_zscore", "open_interest", ["open_interest"],
        lambda w: _un("ts_zscore", "open_interest", window=w),
        "Open-interest extremity (leverage crowding).",
    ),
    _tpl(
        "oi_rel_change", "open_interest", ["open_interest"],
        lambda w: _bin(
            "div",
            _un("ts_delta", "open_interest", window=1),
            _safe_denom("open_interest"),
        ),
        "Scale-free one-bar change in open interest.",
    ),
    _tpl(
        "oi_momentum", "open_interest", ["open_interest"],
        lambda w: _un("ts_momentum", "open_interest", window=w),
        "Trailing open-interest growth rate.",
    ),
    _tpl(
        "oi_rank", "open_interest", ["open_interest"],
        lambda w: _un("ts_rank", "open_interest", window=w),
        "Percentile of current OI in the trailing window.",
    ),
    _tpl(
        "oi_volume_divergence", "open_interest", ["open_interest"],
        lambda w: _bin(
            "div",
            _un("ts_delta", "open_interest", window=w),
            _safe_denom(_un("ts_delta", "volume", window=w)),
        ),
        "OI build-up per unit of traded-volume change.",
    ),
    _tpl(
        "oi_price_divergence", "open_interest", ["open_interest"],
        lambda w: _bin(
            "div",
            _un("ts_momentum", "open_interest", window=w),
            _safe_denom(_un("ts_momentum", "close", window=w)),
        ),
        "OI growth per unit of price move: new money vs short covering.",
    ),
    _tpl(
        "oi_ema_gap", "open_interest", ["open_interest"],
        lambda w: _bin("sub", "open_interest", _un("ts_ema", "open_interest", window=w)),
        "Deviation of OI from its smoothed level.",
    ),
    # ------------------------------ basis ---------------------------------- #
    _tpl(
        "basis_zscore", "basis", ["basis"],
        lambda w: _un("ts_zscore", "basis", window=w),
        "Perp-premium extremity (carry / euphoria gauge).",
    ),
    _tpl(
        "basis_rank", "basis", ["basis"],
        lambda w: _un("ts_rank", "basis", window=w),
        "Percentile of current basis in the trailing window.",
    ),
    _tpl(
        "basis_delta_scaled", "basis", ["basis"],
        lambda w: _bin(
            "div",
            _un("ts_delta", "basis", window=w),
            _safe_denom("close"),
        ),
        "Basis change normalized by price level.",
    ),
    _tpl(
        "basis_vol_adj", "basis", ["basis"],
        lambda w: _bin(
            "div",
            _un("ts_zscore", "basis", window=w),
            _safe_denom(_un("ts_realized_vol", "close", window=w)),
        ),
        "Basis extremity per unit of realized volatility.",
    ),
    _tpl(
        "basis_funding_spread", "basis", ["basis", "funding_rate"],
        lambda w: _bin("sub", "basis", "funding_rate"),
        "Carry spread: perp premium minus funding.",
    ),
    _tpl(
        "basis_funding_gap", "basis", ["basis", "funding_rate"],
        lambda w: _bin(
            "sub",
            _un("ts_zscore", "basis", window=w),
            _un("ts_zscore", "funding_rate", window=w),
        ),
        "Disagreement between basis and funding z-scores.",
    ),
    # --------------------------- liquidations ------------------------------ #
    _tpl(
        "liq_pressure", "liquidation", ["long_liquidations", "short_liquidations"],
        lambda w: _liq_pressure_ast(),
        "Net liquidation imbalance in [-1, 1].",
    ),
    _tpl(
        "long_liq_zscore", "liquidation", ["long_liquidations"],
        lambda w: _un("ts_zscore", "long_liquidations", window=w),
        "Long-liquidation burst relative to history.",
    ),
    _tpl(
        "short_liq_zscore", "liquidation", ["short_liquidations"],
        lambda w: _un("ts_zscore", "short_liquidations", window=w),
        "Short-liquidation burst relative to history.",
    ),
    _tpl(
        "liq_volume_ratio", "liquidation", ["long_liquidations", "short_liquidations"],
        lambda w: _bin(
            "div",
            _bin("add", "long_liquidations", "short_liquidations"),
            _safe_denom("volume"),
        ),
        "Total liquidation notional per unit of traded volume.",
    ),
    _tpl(
        "net_liq_delta", "liquidation", ["long_liquidations", "short_liquidations"],
        lambda w: _bin(
            "sub",
            _un("ts_delta", "long_liquidations", window=1),
            _un("ts_delta", "short_liquidations", window=1),
        ),
        "Change in net forced flow.",
    ),
    # ---------------------------- interactions ----------------------------- #
    _tpl(
        "funding_oi_squeeze", "interaction", ["funding_rate", "open_interest"],
        lambda w: _bin(
            "mul",
            _un("ts_zscore", "funding_rate", window=w),
            _un("sign", _un("ts_delta", "open_interest", window=1)),
        ),
        "Crowded funding while leverage still builds (squeeze setup).",
    ),
    _tpl(
        "oi_liq_interaction", "interaction",
        ["open_interest", "long_liquidations", "short_liquidations"],
        lambda w: _bin(
            "mul",
            _un("ts_zscore", "open_interest", window=w),
            _un("ts_zscore", _liq_pressure_ast(), window=w),
        ),
        "Leverage level times net forced-flow imbalance.",
    ),
    _tpl(
        "basis_oi_carry", "interaction", ["basis", "open_interest"],
        lambda w: _bin(
            "mul",
            _un("ts_zscore", "basis", window=w),
            _un("sign", _un("ts_delta", "open_interest", window=1)),
        ),
        "Rich basis with rising OI (levered carry unwind risk).",
    ),
    _tpl(
        "funding_liq_reversal", "interaction",
        ["funding_rate", "long_liquidations", "short_liquidations"],
        lambda w: _bin(
            "mul",
            _un("ts_zscore", "funding_rate", window=w),
            _liq_pressure_ast(),
        ),
        "Funding crowding confirmed by forced-flow direction.",
    ),
]

TEMPLATES_BY_NAME: Dict[str, CryptoFeatureTemplate] = {
    t.name: t for t in CRYPTO_FEATURE_TEMPLATES
}

# Default window ladder (bars): 6/12/24/48 covers intraday-to-daily crypto
# cycles at 1h and stays sensible at 1d (roughly 1 week to 2 months).
DEFAULT_CRYPTO_WINDOWS: Tuple[int, ...] = (6, 12, 24, 48)


# --------------------------------------------------------------------------- #
# Introspection / filtering
# --------------------------------------------------------------------------- #
def crypto_streams_in(data: Any) -> Set[str]:
    """Return which crypto streams are present in a data panel.

    ``data`` may be the cross-asset dict (``{stream: DataFrame}``) or a
    sequential DataFrame (``stream`` columns).
    """
    if isinstance(data, Mapping):
        columns = set(data.keys())
    elif isinstance(data, pd.DataFrame):
        columns = set(data.columns)
    else:
        columns = set()
    return {s for s in CRYPTO_STREAM_NAMES if s in columns}


def applicable_templates(streams_present: Iterable[str]) -> List[CryptoFeatureTemplate]:
    present = set(streams_present)
    return [t for t in CRYPTO_FEATURE_TEMPLATES if t.applicable(present)]


def crypto_terminals(
    terminals: Iterable[str], data: Any
) -> List[str]:
    """Extend a terminal list with the crypto streams actually present."""
    out = list(terminals)
    for stream in CRYPTO_STREAM_NAMES:
        if stream in crypto_streams_in(data) and stream not in out:
            out.append(stream)
    return out


def iter_crypto_features(
    data: Any = None,
    windows: Sequence[int] = DEFAULT_CRYPTO_WINDOWS,
    streams_present: Optional[Iterable[str]] = None,
    names: Optional[Iterable[str]] = None,
) -> Iterator[Tuple[CryptoFeatureTemplate, int, Dict[str, Any]]]:
    """Yield ``(template, window, ast)`` for every applicable combination.

    Window-independent templates (sign, liquidation imbalance) would otherwise
    be emitted once per window; dedupe by canonical AST signature so a trial is
    never counted twice (which would bias multiple-testing corrections).
    """
    import json

    if streams_present is None:
        streams_present = crypto_streams_in(data) if data is not None else set(CRYPTO_STREAM_NAMES)
    present = set(streams_present)
    wanted = set(names) if names is not None else None

    seen: Set[str] = set()
    for template in CRYPTO_FEATURE_TEMPLATES:
        if wanted is not None and template.name not in wanted:
            continue
        if not template.applicable(present):
            continue
        for w in windows:
            w = int(w)
            if w <= 0:
                continue
            ast = template.build(w)
            signature = json.dumps(ast, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            yield template, w, ast


def build_crypto_feature_asts(
    data: Any = None,
    windows: Sequence[int] = DEFAULT_CRYPTO_WINDOWS,
    streams_present: Optional[Iterable[str]] = None,
    rng: Optional[random.Random] = None,
    limit: Optional[int] = None,
    names: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Build crypto-native feature ASTs filtered by available streams.

    Parameters
    ----------
    data:
        Optional data panel; when given, ``streams_present`` is inferred from it.
    windows:
        Window ladder applied to every applicable template.
    streams_present:
        Explicit stream set (overrides ``data`` inference).
    rng:
        Optional ``random.Random`` used to shuffle before truncating to
        ``limit``.  Deterministic when seeded.
    limit:
        Maximum number of ASTs to return (after shuffling).
    names:
        Restrict to specific template names.
    """
    asts = [
        ast for _, _, ast in iter_crypto_features(
            data=data, windows=windows, streams_present=streams_present, names=names
        )
    ]
    if rng is not None:
        rng.shuffle(asts)
    if limit is not None:
        asts = asts[: max(0, int(limit))]
    return asts


def crypto_feature_catalog(
    streams_present: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Metadata for every template, optionally flagging applicability."""
    present = None if streams_present is None else set(streams_present)
    out: List[Dict[str, Any]] = []
    for t in CRYPTO_FEATURE_TEMPLATES:
        out.append({
            "name": t.name,
            "category": t.category,
            "required": list(t.required),
            "rationale": t.rationale,
            "applicable": True if present is None else t.applicable(present),
        })
    return out


def streams_required_by_library() -> Dict[str, List[str]]:
    """Which streams each category needs — used to decide what to download."""
    needed: Dict[str, Set[str]] = {}
    for t in CRYPTO_FEATURE_TEMPLATES:
        needed.setdefault(t.category, set()).update(t.required)
    return {k: sorted(v) for k, v in needed.items()}


def describe_available_streams(streams_present: Iterable[str]) -> str:
    """One-line description for prompts / logs."""
    present = [s for s in CRYPTO_STREAM_NAMES if s in set(streams_present)]
    if not present:
        return "No crypto-native streams present (OHLCV only)."
    parts = []
    for s in present:
        spec = CRYPTO_STREAM_SPECS.get(s, {})
        parts.append(f"{s} ({spec.get('label', s)})")
    return "Crypto-native streams available: " + ", ".join(parts)
