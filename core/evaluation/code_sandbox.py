"""Fail-closed execution boundary for LLM-generated factor code."""

from __future__ import annotations

import ast
import multiprocessing
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

import numpy as np
import pandas as pd


class SecurityError(Exception):
    """Generated code violates the static execution policy."""


class SandboxExecutionError(Exception):
    """Generated code failed inside the isolated worker."""


class SandboxTimeoutError(SandboxExecutionError):
    """Generated code exceeded its wall-clock budget."""


class FactorOutputError(ValueError):
    """Generated code returned an object that violates the factor contract."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 5.0
    cpu_seconds: int = 3
    memory_mb: int = 1024


_SAFE_BUILTINS = {
    "abs",
    "min",
    "max",
    "len",
    "range",
    "list",
    "dict",
    "float",
    "int",
}

_SAFE_ATTRIBUTES = {
    # pandas Series/DataFrame transforms used by factor formulas.
    "abs",
    "add",
    "clip",
    "corr",
    "cummax",
    "cummin",
    "cumprod",
    "cumsum",
    "diff",
    "div",
    "divide",
    "dropna",
    "ewm",
    "expanding",
    "fillna",
    "index",
    "isna",
    "log",
    "max",
    "mean",
    "median",
    "min",
    "mul",
    "multiply",
    "notna",
    "pct_change",
    "pow",
    "quantile",
    "rank",
    "replace",
    "rolling",
    "shift",
    "sign",
    "std",
    "sub",
    "subtract",
    "sum",
    "var",
    "where",
    "winsorize",
    # Explicit NumPy/Pandas entry points.
    "Series",
    "DataFrame",
    "array",
    "exp",
    "log1p",
    "maximum",
    "minimum",
    "nan",
    "sqrt",
}

# Windows 上没有 resource 模块，RLIMIT_AS / RLIMIT_CPU 会静默失效，
# 沙箱只剩墙钟超时。必须同时限制常量规模，否则
# "x = list(range(10**9))" 这类合法语法能在超时前耗尽进程内存。
_MAX_NUMERIC_CONSTANT = 1_000_000
_MAX_POW_EXPONENT = 6

_ALLOWED_NODE_TYPES = (
    ast.Module,
    ast.Assign,
    ast.Expr,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.Subscript,
    ast.Slice,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Call,
    ast.Attribute,
    ast.keyword,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Invert,
    ast.And,
    ast.Or,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


class FactorCodePolicy(ast.NodeVisitor):
    """Small AST allowlist for vectorized pandas/numpy factor expressions."""

    def __init__(self) -> None:
        self.assigned_names: set[str] = set()
        self.factor_assigned = False

    def validate(self, code_str: str) -> ast.Module:
        try:
            tree = ast.parse(code_str, mode="exec")
        except SyntaxError as exc:
            raise SecurityError(f"Generated factor code has invalid syntax: {exc.msg}") from exc
        self.visit(tree)
        if not self.factor_assigned:
            raise FactorOutputError(
                "The generated code must assign the result to the variable 'factor'"
            )
        return tree

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise SecurityError(
                f"Generated factor code uses forbidden syntax: {type(node).__name__}"
            )
        super().generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if not isinstance(target, ast.Name):
                raise SecurityError("Assignments may target local variable names only")
            if target.id in {"df", "np", "pd"} or target.id in _SAFE_BUILTINS:
                raise SecurityError(f"Assignment to protected name '{target.id}' is forbidden")
            if target.id.startswith("_"):
                raise SecurityError("Private or dunder variable names are forbidden")
            self.assigned_names.add(target.id)
            if target.id == "factor":
                self.factor_assigned = True
        self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise SecurityError("Private or dunder names are forbidden")
        if isinstance(node.ctx, ast.Load):
            allowed = {"df", "np", "pd"} | _SAFE_BUILTINS | self.assigned_names
            if node.id not in allowed:
                raise SecurityError(f"Use of name '{node.id}' is not allowed")

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(value) > _MAX_NUMERIC_CONSTANT:
                raise SecurityError(
                    f"Numeric constant {value!r} exceeds the allowed magnitude "
                    f"({_MAX_NUMERIC_CONSTANT})"
                )
        self.generic_visit(node)

    @classmethod
    def _literal_value(cls, node: ast.AST) -> Optional[float]:
        """Fold a literal-only arithmetic subtree, or return None.

        E-06: checking each constant individually is not enough —
        ``999999 * 999999 * 999999`` passes ``visit_Constant`` (each <= 1e6)
        and used to allocate an unbounded range/list on Windows, where
        RLIMIT_AS/RLIMIT_CPU are unavailable.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = cls._literal_value(node.operand)
            if inner is None:
                return None
            return -inner if isinstance(node.op, ast.USub) else inner
        if isinstance(node, ast.BinOp):
            left = cls._literal_value(node.left)
            right = cls._literal_value(node.right)
            if left is None or right is None:
                return None
            try:
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Pow):
                    return left ** right
            except (OverflowError, ValueError, ZeroDivisionError):
                return float("inf")
        return None

    def visit_BinOp(self, node: ast.BinOp) -> None:
        folded = self._literal_value(node)
        if folded is not None and abs(folded) > _MAX_NUMERIC_CONSTANT:
            raise SecurityError(
                "Literal arithmetic exceeds the allowed magnitude "
                f"({_MAX_NUMERIC_CONSTANT}); large precomputed constants can "
                "exhaust memory before the wall-clock timeout fires."
            )
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant):
            exponent = node.right.value
            if (
                isinstance(exponent, (int, float))
                and not isinstance(exponent, bool)
                and exponent > _MAX_POW_EXPONENT
            ):
                raise SecurityError(
                    f"Exponent {exponent!r} exceeds the allowed maximum "
                    f"({_MAX_POW_EXPONENT})"
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr not in _SAFE_ATTRIBUTES:
            raise SecurityError(f"Attribute '{node.attr}' is not allowed")
        self.visit(node.value)

    @staticmethod
    def _is_negative_number(node: ast.AST) -> bool:
        """True for ``-1`` / ``-0.5`` / any unary-minus expression."""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value < 0
        return False

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id not in _SAFE_BUILTINS:
                raise SecurityError(f"Call to '{node.func.id}' is not allowed")
            # E-06: no RLIMIT_AS on Windows, so cap allocation-heavy builtins.
            if node.func.id in ("range", "list", "tuple") and node.args:
                bound = self._literal_value(node.args[0])
                if bound is not None and abs(bound) > _MAX_NUMERIC_CONSTANT:
                    raise SecurityError(
                        f"{node.func.id}() with {bound:.0f} elements exceeds the "
                        f"allowed allocation size ({_MAX_NUMERIC_CONSTANT})."
                    )
        elif not isinstance(node.func, ast.Attribute):
            raise SecurityError("Only approved builtin and attribute calls are allowed")

        # A2: a negative lag turns ``close.shift(-1)/close-1`` into a perfect
        # copy of the forward-return label. The whitelist used to check only
        # *parameter names*, never their values or signs.
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("bfill", "backfill"):
                raise SecurityError(
                    "Backward filling uses future bars and is forbidden"
                )
            if attr in ("shift", "diff", "pct_change", "rolling", "ewm", "expanding"):
                lag_args = list(node.args) + [
                    kw.value for kw in node.keywords
                    if kw.arg in ("periods", "n", "window", "lag", "span")
                ]
                for arg in lag_args:
                    if self._is_negative_number(arg):
                        raise SecurityError(
                            "Negative time displacement is forbidden (look-ahead)"
                        )
            if attr == "fillna":
                for kw in node.keywords:
                    if kw.arg == "method" and isinstance(kw.value, ast.Constant) \
                            and str(kw.value.value).lower() in ("bfill", "backfill"):
                        raise SecurityError(
                            "fillna(method='bfill') uses future bars and is forbidden"
                        )
        self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg.startswith("_"):
                raise SecurityError("Dynamic or private keyword arguments are forbidden")
            self.visit(keyword.value)


def _validate_finite_numeric(value: Union[pd.Series, pd.DataFrame]) -> None:
    dtypes = [value.dtype] if isinstance(value, pd.Series) else list(value.dtypes)
    if any(not pd.api.types.is_numeric_dtype(dtype) for dtype in dtypes):
        raise FactorOutputError("LLM factor output must contain numeric values only")
    if int(value.notna().to_numpy().sum()) == 0:
        raise FactorOutputError("LLM factor output must contain at least one finite value")
    numeric = value.to_numpy(dtype=float, na_value=float("nan"))
    import numpy as np

    if bool((~np.isnan(numeric) & ~np.isfinite(numeric)).any()):
        raise FactorOutputError("LLM factor output must not contain infinite values")


def validate_llm_factor_output(
    value: Any,
    data: Any,
) -> Union[pd.Series, pd.DataFrame]:
    """Enforce the code-expression contract before returning across the boundary."""
    if isinstance(data, pd.DataFrame):
        if not isinstance(value, pd.Series):
            raise FactorOutputError(
                "Sequential LLM factor code must return pandas.Series, "
                f"received {type(value).__name__}"
            )
        if len(value) != len(data.index):
            raise FactorOutputError(
                f"LLM factor length {len(value)} does not match input length "
                f"{len(data.index)}"
            )
        if not value.index.equals(data.index):
            raise FactorOutputError("LLM factor index must exactly match the input df index")
        if value.index.has_duplicates:
            raise FactorOutputError("LLM factor index must not contain duplicates")
        _validate_finite_numeric(value)
        return value

    if not isinstance(data, dict) or not data:
        raise FactorOutputError(
            "LLM factor input must be a DataFrame or a non-empty feature mapping"
        )
    feature_frames = list(data.values())
    if any(not isinstance(frame, pd.DataFrame) for frame in feature_frames):
        raise FactorOutputError(
            "Cross-asset LLM inputs must map every feature to pandas.DataFrame"
        )
    reference = feature_frames[0]
    for frame in feature_frames[1:]:
        if (
            not frame.index.equals(reference.index)
            or not frame.columns.equals(reference.columns)
        ):
            raise FactorOutputError(
                "Cross-asset LLM feature frames must have identical indexes and columns"
            )
    if not isinstance(value, pd.DataFrame):
        raise FactorOutputError(
            "Cross-asset LLM factor code must return pandas.DataFrame, "
            f"received {type(value).__name__}"
        )
    if (
        not value.index.equals(reference.index)
        or not value.columns.equals(reference.columns)
    ):
        raise FactorOutputError(
            "Cross-asset LLM factor index and columns must exactly match the inputs"
        )
    if value.index.has_duplicates or value.columns.has_duplicates:
        raise FactorOutputError(
            "Cross-asset LLM factor axes must not contain duplicates"
        )
    _validate_finite_numeric(value)
    return value


def causality_probe(
    code: Any,
    globals_: Dict[str, Any],
    data: Any,
    output: Union[pd.Series, pd.DataFrame],
    tail: int = 5,
) -> None:
    """A2: recompute on a tail-truncated tape and demand identical early rows.

    Static AST checks cannot see ``n = -1; df.close.shift(n)``, a full-sample
    ``rank(pct=True)``, or any other indirectly constructed look-ahead. A
    causal factor's value at time t may only depend on rows <= t, so removing
    the last ``tail`` rows must not change ANY of the earlier values.
    """
    try:
        n_rows = len(data.index) if isinstance(data, pd.DataFrame) else len(
            next(iter(data.values())).index
        )
    except Exception:
        return  # cannot determine a timeline; static checks still apply
    cut = n_rows - tail
    if cut < max(20, tail * 2):
        return  # too short for a meaningful comparison

    if isinstance(data, pd.DataFrame):
        truncated = data.iloc[:cut]
    else:
        truncated = {k: v.iloc[:cut] for k, v in data.items()}

    local_vars = {"df": truncated}
    try:
        exec(code, dict(globals_), local_vars)
    except Exception as exc:
        raise SecurityError(f"Causality probe recomputation failed: {exc}") from exc
    replay = local_vars.get("factor")
    if replay is None or not hasattr(replay, "to_numpy"):
        raise SecurityError("Causality probe produced no factor output")

    try:
        expected = np.asarray(output.iloc[:cut], dtype=float)
        actual = np.asarray(replay, dtype=float)
    except Exception as exc:
        raise SecurityError(f"Causality probe could not compare outputs: {exc}") from exc
    if expected.shape != actual.shape:
        raise SecurityError(
            f"Causality probe shape mismatch: {actual.shape} vs {expected.shape}"
        )
    if not np.allclose(actual, expected, rtol=1e-8, atol=1e-10, equal_nan=True):
        raise SecurityError(
            "Factor is not point-in-time causal: truncating the last "
            f"{tail} bars changed earlier factor values"
        )


def _apply_resource_limits(limits: SandboxLimits) -> None:
    try:
        import resource

        cpu = max(1, int(limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        memory = max(128, int(limits.memory_mb)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        if hasattr(resource, "RLIMIT_NOFILE"):
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (ImportError, OSError, ValueError):
        # Wall-clock timeout and process termination remain active on platforms
        # where one or more POSIX resource limits are unavailable.
        pass


def _sandbox_worker(connection, code_str: str, data: Any, limits: SandboxLimits) -> None:
    try:
        _apply_resource_limits(limits)
        import builtins
        import numpy as np

        safe_globals = {
            "__builtins__": {
                name: getattr(builtins, name)
                for name in _SAFE_BUILTINS
            },
            "np": np,
            "pd": pd,
        }
        local_vars = {"df": data}
        compiled = compile(code_str, "<llm-factor>", "exec")
        exec(compiled, safe_globals, local_vars)
        output = validate_llm_factor_output(local_vars.get("factor"), data)
        # A2: dynamic point-in-time check. Static AST rules cannot see an
        # indirectly constructed negative lag or a full-sample rank.
        causality_probe(compiled, safe_globals, data, output)
        connection.send({"status": "success", "output": output})
    except Exception as exc:
        connection.send(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        connection.close()


class RestrictedSandbox:
    """Validate statically, then execute in a bounded short-lived process."""

    def __init__(
        self,
        timeout_seconds: float = 5.0,
        cpu_seconds: int = 3,
        memory_mb: int = 1024,
    ) -> None:
        self.limits = SandboxLimits(
            timeout_seconds=float(timeout_seconds),
            cpu_seconds=int(cpu_seconds),
            memory_mb=int(memory_mb),
        )

    def validate_code(self, code_str: str) -> None:
        FactorCodePolicy().validate(code_str)

    def execute_factor_code(
        self,
        code_str: str,
        data: Any,
    ) -> Union[pd.Series, pd.DataFrame]:
        self.validate_code(code_str)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_sandbox_worker,
            args=(child, code_str, data, self.limits),
            daemon=True,
        )
        process.start()
        child.close()
        try:
            if not parent.poll(self.limits.timeout_seconds):
                process.terminate()
                process.join(timeout=1)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=1)
                raise SandboxTimeoutError(
                    f"LLM factor execution exceeded {self.limits.timeout_seconds:.2f}s"
                )
            result = parent.recv()
        except EOFError as exc:
            raise SandboxExecutionError(
                f"LLM factor worker exited without a result (exit code {process.exitcode})"
            ) from exc
        finally:
            parent.close()
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)

        if result["status"] == "success":
            return result["output"]
        error_type = result.get("error_type", "SandboxExecutionError")
        message = result.get("message", "LLM factor execution failed")
        if error_type == "FactorOutputError":
            raise FactorOutputError(message)
        raise SandboxExecutionError(f"{error_type}: {message}")
