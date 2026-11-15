"""Canonical local market-data naming utilities.

All V4 data files use ``{safe_symbol}-{timeframe}-{trade_type}.feather``.
``safe_symbol`` is derived directly from the CCXT symbol, for example
``BTC/USDT:USDT`` becomes ``BTC_USDT_USDT``.
"""

import os
from pathlib import Path
from typing import Optional, Union


DEFAULT_DATA_ROOT = "data"


def resolve_data_root(
    value: Optional[Union[str, Path]] = None, *, cwd: Optional[Union[str, Path]] = None
) -> Path:
    """Resolve a data root to an absolute path.

    One definition for the whole system, so the directory the preflight
    validates is the directory the miner actually reads.  A relative value
    (including the default ``"data"``) is anchored at ``cwd`` -- the project
    root for a CLI run -- and an empty/``None`` value falls back to the
    default.
    """
    base = Path(cwd) if cwd is not None else Path(os.getcwd())
    raw = str(value).strip() if value is not None else ""
    if not raw:
        raw = DEFAULT_DATA_ROOT
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def safe_symbol(symbol: str) -> str:
    """Return the filesystem-safe form of a CCXT market symbol."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty CCXT symbol string")
    return symbol.strip().replace("/", "_").replace(":", "_")


def data_filename(symbol: str, timeframe: str, trade_type: str) -> str:
    """Build the only supported V4 local data filename."""
    if not timeframe or not trade_type:
        raise ValueError("timeframe and trade_type are required")
    if trade_type == "futures" and ":" not in symbol:
        raise ValueError("futures symbols must use CCXT settlement notation, e.g. BTC/USDT:USDT")
    return f"{safe_symbol(symbol)}-{timeframe}-{trade_type}.feather"


def data_path(
    data_root: Union[str, Path],
    exchange: str,
    symbol: str,
    timeframe: str,
    trade_type: str,
) -> Path:
    """Build the canonical local data path without probing legacy filenames."""
    if not exchange:
        raise ValueError("exchange is required")
    return Path(data_root) / exchange / trade_type / data_filename(symbol, timeframe, trade_type)


def parse_data_filename(filename: str) -> tuple[str, str, str]:
    """Parse a canonical V4 filename into safe symbol, timeframe and trade type."""
    path = Path(filename)
    if path.suffix != ".feather":
        raise ValueError(f"unsupported data file extension: {filename}")

    parts = path.stem.rsplit("-", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"not a canonical V4 data filename: {filename}")
    safe_name, timeframe, trade_type = parts
    if trade_type == "futures":
        symbol_parts = safe_name.split("_")
        if len(symbol_parts) < 3 or symbol_parts[-1] != symbol_parts[-2]:
            raise ValueError(f"futures filename lacks a CCXT settlement suffix: {filename}")
    return safe_name, timeframe, trade_type
