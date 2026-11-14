"""FactorMiner backtest component.

Integrates stored factor value snapshots with the ml4t-backtest engine so a
cross-sectional long/short portfolio can be backtested against local market
data without re-implementing an event loop.

Public API:
  - run_backtest(...)            orchestrate a full backtest run
  - load_factor_signals(...)     build the ensemble signal from factor_db
  - load_prices(...)             load local V4 feather market data
  - CrossSectionalFactorStrategy the trading logic used by the engine
"""

from core.backtest.backtest_engine import (
    CrossSectionalFactorStrategy,
    build_backtest_config,
    build_funding,
    infer_orientation,
    load_factor_signals,
    load_prices,
    run_backtest,
)

__all__ = [
    "CrossSectionalFactorStrategy",
    "build_backtest_config",
    "build_funding",
    "infer_orientation",
    "load_factor_signals",
    "load_prices",
    "run_backtest",
]
