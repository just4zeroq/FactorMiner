from abc import ABC, abstractmethod
import numpy as np
import pandas as pd
from typing import List, Dict, Any, Union, Optional

from core.miner.ridge_weights import fit_scale_aware_ridge

class FactorExpression(ABC):
    """
    统一的因子中间体表达式
    不论生成器是 GP、RL、LLM 还是 NN，最终吐出的都是该类的子类实例。
    """
    
    def __init__(self):
        self.metrics: Dict[str, float] = {}
        self.logic_hash: str = ""
    @abstractmethod
    def compute(self, data: pd.DataFrame) -> Union[pd.Series, pd.DataFrame, Any]:
        """
        统一计算接口。
        - AST派生类: 在这里遍历解析树并计算
        - Code派生类: 在这里使用 exec/eval 计算
        - DL派生类: 在这里执行 model.forward(data)
        """
        pass
        
    @abstractmethod
    def get_source(self) -> Any:
        """
        获取底层真正的逻辑表达（比如 ast dict，code 字符串，或 nn.Module）
        """
        pass
        
    @abstractmethod
    def get_complexity(self) -> str:
        """
        获取因子的复杂度标签，如 "Depth: 5", "LoC: 10", "Params: 100"
        """
        pass
        
    @abstractmethod
    def to_display_string(self, max_length: int = 50) -> str:
        """
        格式化为用于终端或 UI 表格中展示的精简字符串
        """
        pass

class LineageTrackableMixIn:
    """可追溯谱系的能力标签"""
    def __init__(self, parent_ids: List[str] = None):
        self._parent_ids = parent_ids or []
        
    def get_lineage_parents(self) -> List[str]:
        return self._parent_ids

class LLMReflectableMixIn:
    """具有大模型反思记忆的能力标签"""
    def __init__(self, reflection_history: str = ""):
        self._reflection_history = reflection_history
        
    def get_reflection_history(self) -> str:
        return self._reflection_history

class FactorExpressionAST(FactorExpression, LineageTrackableMixIn):
    """供遗传规划 (GP) 专用的 AST 表达式"""
    def __init__(self, ast_dict: Dict, parent_ids: List[str] = None):
        FactorExpression.__init__(self)
        LineageTrackableMixIn.__init__(self, parent_ids)
        self.ast_dict = ast_dict
        
    def compute(self, data: pd.DataFrame):
        from core.miner.operator_runtime import evaluate_ast

        return evaluate_ast(self.ast_dict, data)
        
    def get_source(self) -> Dict:
        return self.ast_dict
        
    def get_complexity(self) -> str:
        # 简单计算深度
        def get_depth(node):
            if not isinstance(node, dict) or 'op' not in node:
                return 1
            left_depth = get_depth(node.get('left', None))
            right_depth = get_depth(node.get('right', None))
            return 1 + max(left_depth, right_depth)
        return f"Depth: {get_depth(self.ast_dict)}"
        
    def to_display_string(self, max_length: int = None) -> str:
        def to_str(node):
            if not isinstance(node, dict) or 'op' not in node:
                return str(node)
            left = to_str(node.get('left'))
            if node.get('right') is None:
                return f"{node['op']}({left})"
            return f"{node['op']}({left}, {to_str(node.get('right'))})"
        s = to_str(self.ast_dict)
        if max_length is not None and len(s) > max_length:
            return s[:max_length-3] + "..."
        return s

class FactorExpressionCode(FactorExpression, LineageTrackableMixIn, LLMReflectableMixIn):
    """供大语言模型 (LLM) 生成纯源码使用的表达式"""
    def __init__(
        self,
        code_str: str,
        sandbox: Any = None,
        parent_ids: List[str] = None,
        reflection_history: str = "",
        provenance: Optional[Dict[str, Any]] = None,
    ):
        FactorExpression.__init__(self)
        LineageTrackableMixIn.__init__(self, parent_ids)
        LLMReflectableMixIn.__init__(self, reflection_history)
        self.code_str = code_str
        self.sandbox = sandbox
        self.provenance = provenance or {}
        
    def compute(self, data: pd.DataFrame):
        if not self.sandbox:
            raise RuntimeError(
                "FactorExpressionCode requires an explicit sandbox executor; "
                "unsafe exec fallback is disabled"
            )
        return self.sandbox.execute_factor_code(self.code_str, data)

    def set_reflection_history(self, reflection_history: str) -> None:
        self._reflection_history = reflection_history

    def get_provenance(self) -> Dict[str, Any]:
        return dict(self.provenance)

    def update_provenance(self, **values: Any) -> None:
        self.provenance.update(values)
        
    def get_source(self) -> str:
        return self.code_str
        
    def get_complexity(self) -> str:
        loc = len([line for line in self.code_str.split('\n') if line.strip()])
        return f"LoC: {loc}"
        
    def to_display_string(self, max_length: int = None) -> str:
        s = self.code_str.replace('\n', ' ').strip()
        if max_length is not None and len(s) > max_length:
            return s[:max_length-3] + "..."
        return s

class FactorExpressionTensor(FactorExpression):
    """供 NN 范式使用的网络计算表达式。"""
    def __init__(self, model_version_id: str, channel_idx: int, model_instance: Any = None):
        FactorExpression.__init__(self)
        self.model_version_id = model_version_id
        self.channel_idx = channel_idx
        self.model_instance = model_instance # PyTorch 模型实例
        
    def compute(self, data: pd.DataFrame):
        # NN 训练阶段不在这里截断计算，需要保留梯度图
        if self.model_instance:
            # 假设 data 转为 tensor 传入
            # return self.model_instance(data)[:, self.channel_idx]
            pass
        return None
        
    def get_source(self) -> Dict:
        return {"model_version": self.model_version_id, "channel": self.channel_idx}

    def export_model_artifact(self):
        """Return a portable artifact when the custom model supports the NN contract."""
        exporter = getattr(self.model_instance, "export_artifact", None)
        return exporter() if callable(exporter) else None
        
    def get_complexity(self) -> str:
        # 简单统计模型参数
        params = 0
        if self.model_instance and hasattr(self.model_instance, 'get_parameter_count'):
            params = int(self.model_instance.get_parameter_count())
        elif self.model_instance and hasattr(self.model_instance, 'parameters'):
            params = sum(p.numel() for p in self.model_instance.parameters() if p.requires_grad)
        elif self.model_instance and hasattr(self.model_instance, 'W'):
            # Supports the NumPy-based reference miner as well as torch-like
            # models above.
            params = getattr(self.model_instance.W, 'size', 0)
        return f"Params: {params}"
        
    def to_display_string(self, max_length: int = None) -> str:
        s = f"NNModel(v={self.model_version_id}) [Ch: {self.channel_idx}]"
        if max_length is not None and len(s) > max_length:
            return s[:max_length-3] + "..."
        return s

class FactorExpressionAction(FactorExpression):
    """供强化学习 (RL) 使用的动作轨迹序列表达式"""
    def __init__(self, action_sequence: List[str]):
        FactorExpression.__init__(self)
        self.action_sequence = action_sequence
        
    def compute(self, data: pd.DataFrame):
        # 根据动作序列累加计算
        return pd.Series(index=data.index, data=0)
        
    def get_source(self) -> List[str]:
        return self.action_sequence
        
    def get_complexity(self) -> str:
        return f"Depth: {len(self.action_sequence)}"
        
    def to_display_string(self, max_length: int = None) -> str:
        s = " -> ".join(self.action_sequence)
        if max_length is not None and len(s) > max_length:
            return s[:max_length-3] + "..."
        return s


class LinearCombinedFactor(FactorExpression):
    """末层线性回归系数拟合 (AlphaGen / Alpha101 风格) 的线性组合因子。

    持有若干子表达式 (通常是 GP / RL 挖掘出的多条 alpha) 与一组**固定的**线性
    权重 ``w``。``compute()`` 在每个时间点对子因子做加权求和，得到最终因子序列。

    权重只在 in-sample (mine) 数据上通过岭回归拟合，之后固定；因此可直接用于
    OOS / 回测，OOS 的 RankIC 能真实反映泛化能力，而不会被 in-sample 拟合所高估。
    """

    def __init__(self, sub_expressions, weights=None, parent_ids=None):
        FactorExpression.__init__(self)
        self.sub_expressions = list(sub_expressions)
        if weights is None:
            weights = [1.0 / len(self.sub_expressions)] * len(self.sub_expressions)
        self.weights = [float(w) for w in weights]
        self._parent_ids = list(parent_ids or [])

    def compute(self, data):
        combined = None
        for sub, w in zip(self.sub_expressions, self.weights):
            f = sub.compute(data)
            if f is None:
                continue
            term = f * w
            combined = term if combined is None else combined + term
        return combined

    def get_source(self):
        return self.to_dict()

    def to_dict(self) -> Dict:
        subs = []
        for s in self.sub_expressions:
            subs.append(s.get_source() if hasattr(s, "get_source") else s)
        return {
            "type": "linear_combination",
            "weights": [float(w) for w in self.weights],
            "sub_asts": subs,
        }

    @classmethod
    def from_dict(cls, d: Dict):
        subs = []
        for a in d.get("sub_asts", []):
            if isinstance(a, dict) and "op" in a:
                subs.append(FactorExpressionAST(ast_dict=a))
            else:
                subs.append(a)
        return cls(sub_expressions=subs, weights=d.get("weights"))

    def get_complexity(self) -> str:
        return f"LinearCombo(K={len(self.sub_expressions)})"

    def to_display_string(self, max_length: int = None) -> str:
        pairs = sorted(
            zip(self.weights, self.sub_expressions),
            key=lambda x: abs(x[0]), reverse=True,
        )
        parts = []
        for w, s in pairs:
            label = s.to_display_string(30) if hasattr(s, "to_display_string") else str(s)
            parts.append(f"{w:+.3f}*{label}")
        s = " + ".join(parts)
        if max_length is not None and len(s) > max_length:
            return s[:max_length - 3] + "..."
        return s


def _flatten_factor(obj) -> np.ndarray:
    """Flatten a factor (Series / DataFrame) to a 1-D numpy array in a stable order.

    DataFrames are flattened row-major (time-major) so every sub-factor and the
    target returns share the same (time, asset) observation ordering.
    """
    if isinstance(obj, pd.DataFrame):
        return obj.to_numpy(dtype=float, na_value=float("nan")).reshape(-1, order="C")
    if isinstance(obj, pd.Series):
        return obj.to_numpy(dtype=float, na_value=float("nan"))
    return np.asarray(obj, dtype=float).reshape(-1)


def _safe_corr(a, b):
    try:
        aa = _flatten_factor(a)
        bb = _flatten_factor(b)
        if aa.shape != bb.shape:
            return None
        m = np.isfinite(aa) & np.isfinite(bb)
        if m.sum() < 30:
            return None
        return float(np.corrcoef(aa[m], bb[m])[0, 1])
    except Exception:
        return None


def fit_linear_combination(
    sub_expressions,
    data,
    returns,
    reg_lambda: float = 1.0,
    min_obs: int = 60,
) -> "LinearCombinedFactor":
    """在 in-sample 数据上用岭回归拟合子因子的线性组合权重。

    只使用传入的 ``data`` / ``returns`` (mine 切分)，因此学到的固定权重可安全
    应用于样本外。返回 ``LinearCombinedFactor``，其 ``compute`` 会在任意数据上重算
    子因子并套用学到的权重。

    不足两个可用子因子、样本过少或矩阵奇异时退化为等权组合。
    """
    if not sub_expressions:
        raise ValueError("fit_linear_combination requires at least one sub-expression")

    cols = []
    usable = []
    for s in sub_expressions:
        f = s.compute(data)
        if f is None:
            continue
        fv = _flatten_factor(f)
        if fv.size == 0:
            continue
        cols.append(fv)
        usable.append(s)

    equal = (
        [1.0 / len(usable)] * len(usable)
        if usable else
        [1.0 / len(sub_expressions)] * len(sub_expressions)
    )
    if len(cols) < 2:
        return LinearCombinedFactor(
            sub_expressions=usable or list(sub_expressions), weights=equal
        )

    y = _flatten_factor(returns)
    n = cols[0].size
    if y.size != n or not all(c.size == n for c in cols):
        # Sub-factor 与目标 returns 的布局不一致 -> 无法安全对齐，退化为等权。
        return LinearCombinedFactor(sub_expressions=usable, weights=equal)

    X = np.column_stack(cols)
    yv = np.asarray(y, dtype=float)

    # M-02: fit on masked, per-column z-scores with an unpenalised intercept,
    # then map the slopes back to raw factor space so compute()/backtest keep
    # evaluating sum(w_j * raw_factor_j) exactly as before.
    fit = fit_scale_aware_ridge(
        X, yv, reg_lambda=reg_lambda, min_obs=min_obs, equal_weights=equal
    )
    return LinearCombinedFactor(sub_expressions=usable, weights=fit.weights)
