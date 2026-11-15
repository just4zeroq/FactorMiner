import inspect
from typing import Callable, Dict

import numpy as np
import pandas as pd


class ExtensionRegistrationError(ValueError):
    """Raised when a user extension cannot satisfy its registry contract."""


def _validate_callable_arity(func: Callable, expected_arity: int, extension_type: str) -> None:
    if not callable(func):
        raise ExtensionRegistrationError(f"{extension_type} must be callable.")
    try:
        inspect.signature(func).bind(*([object()] * expected_arity))
    except TypeError as exc:
        name = getattr(func, "__name__", repr(func))
        raise ExtensionRegistrationError(
            f"{extension_type} '{name}' must accept at least {expected_arity} positional argument(s): {exc}"
        ) from exc

def _validate_operator_causality(func: Callable, arity: int, name: str) -> None:
    """M-08/A2: a custom operator must be point-in-time causal.

    The AST sandbox only governs LLM *code*; a plugin registered with
    ``lambda s: s.shift(-1)`` bypassed it entirely. Recompute the operator on
    a tail-truncated tape: a causal operator's early rows cannot change.
    """
    try:
        idx = pd.RangeIndex(60)
        base = pd.Series(np.arange(60, dtype=float), index=idx)
        args = [base] if arity == 1 else [base, base * 0.5 + 1.0]
        full = np.asarray(func(*args), dtype=float)
        cut_args = [a.iloc[:-5] for a in args]
        cut = np.asarray(func(*cut_args), dtype=float)
        if full.ndim != cut.ndim or full.shape[0] < 6:
            return  # non-series output: nothing comparable
        if not np.allclose(full[:-5], cut[:full.shape[0] - 5],
                           rtol=1e-8, atol=1e-10, equal_nan=True):
            raise ExtensionRegistrationError(
                f"Custom operator '{name}' is not point-in-time causal: "
                "truncating the last 5 bars changed earlier values "
                "(negative shift / full-sample rank / bfill are forbidden)"
            )
    except ExtensionRegistrationError:
        raise
    except Exception:
        # A plugin that cannot be probed on synthetic data is governed by the
        # runtime probe / static AST checks instead of blocking registration.
        return


class OperatorRegistry:
    _registry = {}
    
    @classmethod
    def register(cls, arity: int = 1):
        if arity not in (1, 2):
            raise ExtensionRegistrationError(
                f"Custom operator arity must be 1 or 2, received {arity}."
            )

        def decorator(func: Callable):
            _validate_callable_arity(func, arity, "Custom operator")
            _validate_operator_causality(func, arity, func.__name__)
            cls._registry[func.__name__] = {"func": func, "arity": arity}
            return func
        return decorator

class EvaluatorRegistry:
    _registry = {}
    
    @classmethod
    def register_fitness_hook(cls, hook_name: str):
        if not isinstance(hook_name, str) or not hook_name.strip():
            raise ExtensionRegistrationError("Fitness Hook name must be a non-empty string.")

        def decorator(func: Callable):
            _validate_callable_arity(func, 3, "Fitness Hook")
            cls._registry[hook_name] = func
            return func
        return decorator

class MinerRegistry:
    _registry = {}
    
    @classmethod
    def register(cls, paradigm_name: str):
        if not isinstance(paradigm_name, str) or not paradigm_name.strip():
            raise ExtensionRegistrationError("Miner name must be a non-empty string.")

        def decorator(miner_cls: type):
            from core.miner.paradigms.base import BaseFactorMiner

            if not inspect.isclass(miner_cls) or not issubclass(miner_cls, BaseFactorMiner):
                raise ExtensionRegistrationError(
                    f"Miner '{paradigm_name}' must inherit from BaseFactorMiner."
                )
            cls._registry[paradigm_name] = miner_cls
            return miner_cls
        return decorator
