"""Shared AST operator discovery, validation, generation and evaluation.

P0-2 upgrade: the built-in operator library supports *parameterized* windows
and a much richer set of time-series primitives (z-score, rank, delta,
momentum, min/max, correlation, covariance, cross-sectional rank, signed power,
decay-linear). Operators accept an optional ``window`` (or ``p``) argument with a
sane default, so GP/LLM can emit e.g. ``{"op": "ts_mean", "left": <node>,
"window": 5}`` and :func:`evaluate_ast` forwards those node-level keys as kwargs.

Crypto expansion: the library now also provides exponential/weighted smoothing,
lag (ref), higher-order moments (var/skew/kurt/median/mad), position-of-extreme
(argmax/argmin), count-of-up, rolling regression (beta), rolling sum/product,
realized volatility, RSI, cumulative sum, a full set of cross-sectional ops
(cs_zscore/cs_mean/cs_sum/cs_min/cs_max/cs_mad/cs_neutralize), pointwise
transforms (abs/sign/log/sqrt/exp/inv/sin/cos/tanh/sigmoid/clamp/scale) and
pairwise conditionals (max2/min2/greater/less). This lifts coverage from ~18 to
~58 operators, into AlphaGen/Alpha101 territory while staying crypto-relevant.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

from core.miner.registry import OperatorRegistry


class OperatorRuntimeError(ValueError):
    """Raised when an AST references an unavailable or invalid operator."""


def _safe_divide(left: Any, right: Any) -> Any:
    if hasattr(right, "replace"):
        right = right.replace(0, 1e-9)
    elif right == 0:
        right = 1e-9
    return left / right


def _min_periods(window: int) -> int:
    return max(2, int(window) // 2)


# --------------------------------------------------------------------------- #
# Time-series operator primitives (most accept an optional window / power param)
# --------------------------------------------------------------------------- #
def _rolling_mean(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).mean()


def _rolling_std(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).std()


def _ts_zscore(series: Any, window: int = 20) -> Any:
    w = int(window)
    m = series.rolling(w, min_periods=_min_periods(w)).mean()
    s = series.rolling(w, min_periods=_min_periods(w)).std()
    return (series - m) / (s + 1e-9)


def _ts_rank(series: Any, window: int = 20) -> Any:
    w = int(window)

    def _rank(x: np.ndarray) -> float:
        if len(x) < 2:
            return float("nan")
        last = x[-1]
        return float((x > last).sum()) / (len(x) - 1)

    return series.rolling(w, min_periods=_min_periods(w)).apply(_rank, raw=True)


def _ts_delta(series: Any, window: int = 20) -> Any:
    return series - series.shift(int(window))


def _ts_momentum(series: Any, window: int = 20) -> Any:
    w = int(window)
    base = series.shift(w)
    return series / (base + 1e-9) - 1.0


def _ts_min(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).min()


def _ts_max(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).max()


def _ts_corr(left: Any, right: Any, window: int = 20) -> Any:
    return left.rolling(int(window), min_periods=_min_periods(window)).corr(right)


def _ts_cov(left: Any, right: Any, window: int = 20) -> Any:
    return left.rolling(int(window), min_periods=_min_periods(window)).cov(right)


def _signed_power(series: Any, p: float = 2.0) -> Any:
    return np.sign(series) * np.power(np.abs(series) + 1e-12, float(p))


def _ts_decay_linear(series: Any, window: int = 20) -> Any:
    w = int(window)
    weights = np.arange(1, w + 1, dtype=float)

    def _wmean(x: np.ndarray) -> float:
        seg = weights[-len(x):]
        denom = np.nansum(seg)
        if denom == 0:
            return float("nan")
        return float(np.nansum(x * seg) / denom)

    return series.rolling(w, min_periods=_min_periods(w)).apply(_wmean, raw=True)


def _log1p_abs(series: Any) -> Any:
    return np.sign(series) * np.log1p(np.abs(series))


def _cs_rank(series: Any) -> Any:
    """Cross-sectional rank (percentile) across assets/columns."""
    if isinstance(series, pd.DataFrame):
        return series.rank(axis=1, pct=True)
    return series.rank(pct=True)


# --------------------------------------------------------------------------- #
# Crypto expansion: smoothing, lag, higher-order moments, shape, regression
# --------------------------------------------------------------------------- #
def _ts_ema(series: Any, window: int = 20) -> Any:
    return series.ewm(span=int(window), adjust=False).mean()


def _ts_ref(series: Any, n: int = 1) -> Any:
    """Lag operator: value n bars ago (n=1 is the classic delay)."""
    return series.shift(int(n))


def _ts_sum(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).sum()


def _ts_product(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).apply(np.nanprod, raw=True)


def _ts_ma_ratio(series: Any, window: int = 20) -> Any:
    return series / (_rolling_mean(series, window) + 1e-9)


def _ts_var(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).var()


def _ts_median(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).median()


def _ts_mad(series: Any, window: int = 20) -> Any:
    w = int(window)
    mean = series.rolling(w, min_periods=_min_periods(w)).mean()
    return (series - mean).abs().rolling(w, min_periods=_min_periods(w)).mean()


def _ts_skew(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).skew()


def _ts_kurt(series: Any, window: int = 20) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).kurt()


def _ts_quantile(series: Any, window: int = 20, q: float = 0.5) -> Any:
    return series.rolling(int(window), min_periods=_min_periods(window)).quantile(float(q))


def _ts_argmax(series: Any, window: int = 20) -> Any:
    w = int(window)

    def _f(x: np.ndarray) -> float:
        if len(x) < 2:
            return float("nan")
        pos = int(np.argmax(x))
        return float((len(x) - 1) - pos)  # bars since the window high

    return series.rolling(w, min_periods=_min_periods(w)).apply(_f, raw=True)


def _ts_argmin(series: Any, window: int = 20) -> Any:
    w = int(window)

    def _f(x: np.ndarray) -> float:
        if len(x) < 2:
            return float("nan")
        pos = int(np.argmin(x))
        return float((len(x) - 1) - pos)  # bars since the window low

    return series.rolling(w, min_periods=_min_periods(w)).apply(_f, raw=True)


def _ts_count_up(series: Any, window: int = 20) -> Any:
    up = (series.diff() > 0).astype(float)
    return up.rolling(int(window), min_periods=_min_periods(window)).sum()


def _ts_realized_vol(series: Any, window: int = 20) -> Any:
    """Rolling std of log returns — crypto's go-to volatility proxy."""
    ratio = series / series.shift(1)
    ratio = ratio.clip(lower=1e-9)  # guard against 0 / negative (e.g. zero-volume bars)
    lr = np.log(ratio)
    return lr.rolling(int(window), min_periods=_min_periods(window)).std()


def _ts_rsi(series: Any, window: int = 20) -> Any:
    """Relative Strength Index, range (0, 100)."""
    w = int(window)
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    mp = _min_periods(w)
    avg_gain = gain.rolling(w, min_periods=mp).mean()
    avg_loss = loss.rolling(w, min_periods=mp).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100.0 - 100.0 / (1.0 + rs)


def _cumsum(series: Any) -> Any:
    return series.cumsum()


def _ts_regression(left: Any, right: Any, window: int = 20) -> Any:
    """Slope of OLS regressing ``left`` on ``right`` over a rolling window.

    Equivalently the rolling beta of ``left`` to ``right``; computed from the
    correlation / std ratio to stay fully vectorized.
    """
    w = int(window)
    mp = _min_periods(w)
    corr = left.rolling(w, min_periods=mp).corr(right)
    sy = left.rolling(w, min_periods=mp).std()
    sx = right.rolling(w, min_periods=mp).std()
    return corr * sy / (sx + 1e-9)


# --------------------------------------------------------------------------- #
# Pairwise / conditional
# --------------------------------------------------------------------------- #
def _ts_max2(left: Any, right: Any) -> Any:
    return np.maximum(left, right)


def _ts_min2(left: Any, right: Any) -> Any:
    return np.minimum(left, right)


def _greater(left: Any, right: Any) -> Any:
    return (left > right).astype(float)


def _less(left: Any, right: Any) -> Any:
    return (left < right).astype(float)


# --------------------------------------------------------------------------- #
# Pointwise transforms (unary, elementwise, sign-preserving where needed)
# --------------------------------------------------------------------------- #
def _abs(series: Any) -> Any:
    return np.abs(series)


def _sign(series: Any) -> Any:
    return np.sign(series)


def _log(series: Any) -> Any:
    return np.sign(series) * np.log1p(np.abs(series))


def _sqrt(series: Any) -> Any:
    return np.sign(series) * np.sqrt(np.abs(series))


def _exp(series: Any) -> Any:
    return np.exp(series.clip(-30, 30))


def _inv(series: Any) -> Any:
    return np.sign(series) / (np.abs(series) + 1e-9)


def _sin(series: Any) -> Any:
    return np.sin(series)


def _cos(series: Any) -> Any:
    return np.cos(series)


def _tanh(series: Any) -> Any:
    return np.tanh(series)


def _sigmoid(series: Any) -> Any:
    return 1.0 / (1.0 + np.exp(-series.clip(-30, 30)))


def _clamp(series: Any, lo: float = -3.0, hi: float = 3.0) -> Any:
    return series.clip(float(lo), float(hi))


def _scale(series: Any) -> Any:
    """Alpha101 scale: normalize so |values| sum to 1 per cross-section."""
    if isinstance(series, pd.DataFrame):
        denom = series.abs().sum(axis=1).replace(0, np.nan)
        return series.div(denom, axis=0)
    return series / (series.abs().sum() + 1e-9)


# --------------------------------------------------------------------------- #
# Cross-sectional ops (across assets/columns; identity for a single series)
# --------------------------------------------------------------------------- #
def _broadcast_const(frame: pd.DataFrame, series: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        np.broadcast_to(series.values[:, None], (len(series), frame.shape[1])),
        index=frame.index,
        columns=frame.columns,
    )


def _cs_mean(series: Any) -> Any:
    if isinstance(series, pd.DataFrame):
        return _broadcast_const(series, series.mean(axis=1))
    return series


def _cs_sum(series: Any) -> Any:
    if isinstance(series, pd.DataFrame):
        return _broadcast_const(series, series.sum(axis=1))
    return series


def _cs_min(series: Any) -> Any:
    if isinstance(series, pd.DataFrame):
        return _broadcast_const(series, series.min(axis=1))
    return series


def _cs_max(series: Any) -> Any:
    if isinstance(series, pd.DataFrame):
        return _broadcast_const(series, series.max(axis=1))
    return series


def _cs_zscore(series: Any) -> Any:
    if isinstance(series, pd.DataFrame):
        return series.sub(series.mean(axis=1), axis=0).div(series.std(axis=1) + 1e-9, axis=0)
    return series


def _cs_mad(series: Any) -> Any:
    if isinstance(series, pd.DataFrame):
        return series.sub(series.median(axis=1), axis=0)
    return series


def _cs_neutralize(series: Any) -> Any:
    """Cross-sectional (market) neutralization: remove the per-time mean."""
    if isinstance(series, pd.DataFrame):
        return series.sub(series.mean(axis=1), axis=0)
    return series


BUILTIN_OPERATORS: Dict[str, Dict[str, Any]] = {
    # --- scalar binary arithmetic ---
    "add": {"func": lambda left, right: left + right, "arity": 2},
    "sub": {"func": lambda left, right: left - right, "arity": 2},
    "mul": {"func": lambda left, right: left * right, "arity": 2},
    "div": {"func": _safe_divide, "arity": 2},
    # --- rolling statistics (parameterized window) ---
    "ts_mean": {"func": _rolling_mean, "arity": 1},
    "ts_std": {"func": _rolling_std, "arity": 1},
    "ts_zscore": {"func": _ts_zscore, "arity": 1},
    "ts_rank": {"func": _ts_rank, "arity": 1},
    "ts_delta": {"func": _ts_delta, "arity": 1},
    "ts_momentum": {"func": _ts_momentum, "arity": 1},
    "ts_min": {"func": _ts_min, "arity": 1},
    "ts_max": {"func": _ts_max, "arity": 1},
    "ts_decay_linear": {"func": _ts_decay_linear, "arity": 1},
    "signed_power": {"func": _signed_power, "arity": 1},
    "log1p_abs": {"func": _log1p_abs, "arity": 1},
    "cs_rank": {"func": _cs_rank, "arity": 1},
    # --- rolling bivariate (parameterized window) ---
    "ts_corr": {"func": _ts_corr, "arity": 2},
    "ts_cov": {"func": _ts_cov, "arity": 2},
    # --- crypto expansion: smoothing / lag / shape / regression ---
    "ts_ema": {"func": _ts_ema, "arity": 1},
    "ts_ref": {"func": _ts_ref, "arity": 1},
    "ts_sum": {"func": _ts_sum, "arity": 1},
    "ts_product": {"func": _ts_product, "arity": 1},
    "ts_ma_ratio": {"func": _ts_ma_ratio, "arity": 1},
    "ts_var": {"func": _ts_var, "arity": 1},
    "ts_median": {"func": _ts_median, "arity": 1},
    "ts_mad": {"func": _ts_mad, "arity": 1},
    "ts_skew": {"func": _ts_skew, "arity": 1},
    "ts_kurt": {"func": _ts_kurt, "arity": 1},
    "ts_quantile": {"func": _ts_quantile, "arity": 1},
    "ts_argmax": {"func": _ts_argmax, "arity": 1},
    "ts_argmin": {"func": _ts_argmin, "arity": 1},
    "ts_count_up": {"func": _ts_count_up, "arity": 1},
    "ts_realized_vol": {"func": _ts_realized_vol, "arity": 1},
    "ts_rsi": {"func": _ts_rsi, "arity": 1},
    "cumsum": {"func": _cumsum, "arity": 1},
    "ts_regression": {"func": _ts_regression, "arity": 2},
    # --- pairwise / conditional ---
    "ts_max2": {"func": _ts_max2, "arity": 2},
    "ts_min2": {"func": _ts_min2, "arity": 2},
    "greater": {"func": _greater, "arity": 2},
    "less": {"func": _less, "arity": 2},
    # --- pointwise transforms ---
    "abs": {"func": _abs, "arity": 1},
    "sign": {"func": _sign, "arity": 1},
    "log": {"func": _log, "arity": 1},
    "sqrt": {"func": _sqrt, "arity": 1},
    "exp": {"func": _exp, "arity": 1},
    "inv": {"func": _inv, "arity": 1},
    "sin": {"func": _sin, "arity": 1},
    "cos": {"func": _cos, "arity": 1},
    "tanh": {"func": _tanh, "arity": 1},
    "sigmoid": {"func": _sigmoid, "arity": 1},
    "clamp": {"func": _clamp, "arity": 1},
    "scale": {"func": _scale, "arity": 1},
    # --- cross-sectional ops ---
    "cs_zscore": {"func": _cs_zscore, "arity": 1},
    "cs_mean": {"func": _cs_mean, "arity": 1},
    "cs_sum": {"func": _cs_sum, "arity": 1},
    "cs_min": {"func": _cs_min, "arity": 1},
    "cs_max": {"func": _cs_max, "arity": 1},
    "cs_mad": {"func": _cs_mad, "arity": 1},
    "cs_neutralize": {"func": _cs_neutralize, "arity": 1},
}

# Operator names whose first positional argument is a time window; GP samples
# integer windows from this set when it emits such operators.
WINDOWED_OPERATORS = {
    "ts_mean", "ts_std", "ts_zscore", "ts_rank", "ts_delta", "ts_momentum",
    "ts_min", "ts_max", "ts_decay_linear", "ts_corr", "ts_cov",
    "ts_ema", "ts_sum", "ts_product", "ts_ma_ratio", "ts_var", "ts_median",
    "ts_mad", "ts_skew", "ts_kurt", "ts_quantile", "ts_argmax", "ts_argmin",
    "ts_count_up", "ts_realized_vol", "ts_rsi", "ts_regression",
}
POWERED_OPERATORS = {"signed_power"}


# Parameter sampling specs for GP/LLM: op -> list of (name, lo, hi, kind).
# kind is "int" or "float". Operators not listed take no sampled parameters.
OPERATOR_PARAM_SPECS: Dict[str, List[Tuple[str, float, float, str]]] = {
    "ts_mean": [("window", 3, 60, "int")],
    "ts_std": [("window", 3, 60, "int")],
    "ts_zscore": [("window", 3, 60, "int")],
    "ts_rank": [("window", 3, 60, "int")],
    "ts_delta": [("window", 1, 60, "int")],
    "ts_momentum": [("window", 1, 60, "int")],
    "ts_min": [("window", 3, 60, "int")],
    "ts_max": [("window", 3, 60, "int")],
    "ts_decay_linear": [("window", 3, 60, "int")],
    "ts_corr": [("window", 3, 60, "int")],
    "ts_cov": [("window", 3, 60, "int")],
    "ts_ema": [("window", 3, 60, "int")],
    "ts_ref": [("n", 1, 30, "int")],
    "ts_sum": [("window", 3, 60, "int")],
    "ts_product": [("window", 3, 60, "int")],
    "ts_ma_ratio": [("window", 3, 60, "int")],
    "ts_var": [("window", 3, 60, "int")],
    "ts_median": [("window", 3, 60, "int")],
    "ts_mad": [("window", 3, 60, "int")],
    "ts_skew": [("window", 3, 60, "int")],
    "ts_kurt": [("window", 3, 60, "int")],
    "ts_quantile": [("window", 3, 60, "int"), ("q", 0.1, 0.9, "float")],
    "ts_argmax": [("window", 3, 60, "int")],
    "ts_argmin": [("window", 3, 60, "int")],
    "ts_count_up": [("window", 3, 60, "int")],
    "ts_realized_vol": [("window", 3, 60, "int")],
    "ts_rsi": [("window", 3, 60, "int")],
    "ts_regression": [("window", 3, 60, "int")],
    "signed_power": [("p", 0.5, 3.0, "float")],
    "clamp": [("lo", -5.0, -0.5, "float"), ("hi", 0.5, 5.0, "float")],
}

# Keep the legacy derived sets in sync with the canonical spec table.
WINDOWED_OPERATORS = {op for op, specs in OPERATOR_PARAM_SPECS.items()
                      if any(p[0] == "window" for p in specs)}
POWERED_OPERATORS = {op for op, specs in OPERATOR_PARAM_SPECS.items()
                     if any(p[0] == "p" for p in specs)}

PARAM_KEYS = ("window", "p", "n", "q", "lo", "hi")


def sample_operator_params(op: str) -> Dict[str, Any]:
    """Randomly sample parameter values for an operator (used by GP mutation)."""
    import random as _random

    specs = OPERATOR_PARAM_SPECS.get(op, [])
    out: Dict[str, Any] = {}
    for name, lo, hi, kind in specs:
        if kind == "int":
            out[name] = int(_random.randint(int(lo), int(hi)))
        else:
            out[name] = round(_random.uniform(lo, hi), 4)
    return out


def default_operator_params(op: str) -> Dict[str, Any]:
    """Return safe default parameters for an operator (used by LLM generation)."""
    specs = OPERATOR_PARAM_SPECS.get(op, [])
    out: Dict[str, Any] = {}
    for name, lo, hi, kind in specs:
        if kind == "int":
            out[name] = int((lo + hi) / 2)
        else:
            out[name] = round((lo + hi) / 2, 4)
    return out


def get_operator_spec(name: str) -> Dict[str, Any]:
    if name in BUILTIN_OPERATORS:
        return BUILTIN_OPERATORS[name]
    if name in OperatorRegistry._registry:
        return OperatorRegistry._registry[name]

    available = sorted({*BUILTIN_OPERATORS, *OperatorRegistry._registry})
    raise OperatorRuntimeError(
        f"Unknown operator '{name}'. Register it under user_workspace/custom_operators "
        f"or choose one of: {', '.join(available)}."
    )


def resolve_operator_specs(operator_names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(operator_names, (list, tuple)):
        raise OperatorRuntimeError("search_space.allowed_operators must be a non-empty list of operator names.")
    names = list(operator_names)
    if not names:
        raise OperatorRuntimeError("At least one operator must be configured in search_space.allowed_operators.")

    specs: Dict[str, Dict[str, Any]] = {}
    for name in names:
        if not isinstance(name, str) or not name:
            raise OperatorRuntimeError("Every configured operator name must be a non-empty string.")
        spec = get_operator_spec(name)
        arity = spec.get("arity")
        func = spec.get("func")
        if arity not in (1, 2) or not callable(func):
            raise OperatorRuntimeError(
                f"Operator '{name}' has invalid registration metadata; expected callable func and arity 1 or 2."
            )
        specs[name] = spec
    return specs


def configured_operator_names(config: Dict[str, Any], default: Iterable[str] = ("add", "sub", "mul", "div")) -> list[str]:
    return list(config.get("search_space", {}).get("allowed_operators", list(default)))


def _zero_like_data(data: Any) -> Any:
    if isinstance(data, dict):
        if not data:
            return pd.DataFrame()
        sample = next(iter(data.values()))
        return pd.DataFrame(0.0, index=sample.index, columns=sample.columns)
    if isinstance(data, pd.DataFrame):
        return pd.Series(0.0, index=data.index)
    return pd.Series(dtype=float)


def _terminal_value(node: Any, data: Any) -> Any:
    if isinstance(node, str):
        if isinstance(data, dict):
            if node in data:
                return data[node]
            return _zero_like_data(data)
        if isinstance(data, pd.DataFrame) and node in data.columns:
            return data[node]
        return _zero_like_data(data)
    if isinstance(node, (int, float)):
        # Ephemeral constant terminal (e.g. randomly sampled by GP).
        zero = _zero_like_data(data)
        return zero + node
    raise OperatorRuntimeError(f"Invalid AST terminal {node!r}; expected a stream name or numeric constant.")


def evaluate_ast(node: Any, data: Any) -> Any:
    """Evaluate a unary/binary AST against sequential or cross-asset data.

    Any key in the AST node other than ``op``/``left``/``right`` (e.g. ``window``
    or ``p``) is forwarded to the operator function as a keyword argument.
    """
    if not isinstance(node, dict) or "op" not in node:
        return _terminal_value(node, data)

    op_name = node["op"]
    spec = get_operator_spec(op_name)
    left_node = node.get("left")
    if left_node is None:
        raise OperatorRuntimeError(f"Operator '{op_name}' is missing its left operand.")
    left = evaluate_ast(left_node, data)

    # Extra node-level parameters (window, power, ...) -> kwargs.
    params = {k: v for k, v in node.items() if k not in ("op", "left", "right")}

    if spec["arity"] == 1:
        args = (left,)
    else:
        if "right" not in node or node.get("right") is None:
            raise OperatorRuntimeError(f"Binary operator '{op_name}' is missing its right operand.")
        args = (left, evaluate_ast(node["right"], data))

    try:
        result = spec["func"](*args, **params)
    except Exception as exc:
        raise OperatorRuntimeError(f"Operator '{op_name}' failed during AST evaluation: {exc}") from exc

    if not isinstance(result, (pd.Series, pd.DataFrame)):
        raise OperatorRuntimeError(
            f"Operator '{op_name}' returned {type(result).__name__}; operators must return pandas.Series or pandas.DataFrame."
        )
    return result
