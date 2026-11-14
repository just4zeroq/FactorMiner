import logging
import random
from typing import Any, Dict, List

import numpy as np

from core.miner.paradigms.base import BaseFactorMiner
from core.miner.expressions import FactorExpression, FactorExpressionAction
from core.miner.entities import EvaluationFeedback
from core.miner.operator_runtime import (
    configured_operator_names,
    resolve_operator_specs,
    WINDOWED_OPERATORS,
    POWERED_OPERATORS,
)

logger = logging.getLogger(__name__)

TERMINALS_DEFAULT = ["close", "open", "high", "low", "volume"]


class RLFactorExpression(FactorExpressionAction):
    """An RL-produced factor: the sampled action sequence is recorded for lineage,
    but the actual signal is the folded AST, which we can evaluate directly."""

    def __init__(self, action_sequence: List[str], ast_dict: Dict[str, Any], parent_ids=None):
        super().__init__(action_sequence)
        self.action_sequence = action_sequence
        self.ast_dict = ast_dict

    def compute(self, data):
        from core.miner.operator_runtime import evaluate_ast
        return evaluate_ast(self.ast_dict, data)

    def get_source(self):
        # Store the concrete AST (more reproducible than the action sequence).
        return self.ast_dict

    def get_complexity(self):
        def _depth(node):
            if not isinstance(node, dict) or "op" not in node:
                return 1
            return 1 + max(_depth(node.get("left")), _depth(node.get("right")))
        return f"Depth: {_depth(self.ast_dict)}"


class RLFactorMiner(BaseFactorMiner):
    """
    强化学习(RL)因子挖掘器 (P0 upgrade: 真正的策略梯度, 不再是随机游走)

    策略: 一个可学习的 softmax 策略在算子集合上。每轮从策略中采样一条算子
    轨迹 (trajectory)，折叠成一个合法 AST 作为因子；用评价器给出的 fitness
    作为 reward，以 REINFORCE 梯度上升更新策略 logits。advantage 用批次均值
    作基线，从而对"能泛化 (OOS) 的算子组合"给出正反馈。
    """

    def initialize_search_space(self) -> None:
        logger.info("Initializing RL policy network (REINFORCE) and environment...")
        self.action_space = list(
            self.config.get("search_space", {}).get(
                "allowed_operators", configured_operator_names(self.config)
            )
        )
        self.operator_specs = resolve_operator_specs(self.action_space)
        self.terminals = list(
            self.config.get("search_space", {}).get("terminals", TERMINALS_DEFAULT)
        )
        if not self.terminals:
            self.terminals = list(TERMINALS_DEFAULT)
        # P4: GP already promoted crypto-native streams to first-class terminals.
        # RL used to stay on the 5 OHLCV columns, so its action space could not
        # express funding / basis / OI factors at all. Best-effort: when no
        # crypto stream is loaded the list is unchanged.
        from core.miner.crypto_features import crypto_terminals
        try:
            panel = self.data.get_data() if hasattr(self.data, "get_data") else self.data
        except Exception:
            panel = None
        self.terminals = crypto_terminals(self.terminals, panel)
        gp = self.config.get("gp", {})
        self.windows = [int(w) for w in gp.get("windows", [5, 10, 20, 60])]
        self.powers = [float(p) for p in gp.get("powers", [1.0, 2.0, 3.0])]

        # Learnable policy logits (initialized to uniform).
        self.policy_logits = np.zeros(len(self.action_space), dtype=float)
        self.baseline = 0.0
        self.learning_rate = float(self.config.get("rl", {}).get("learning_rate", 0.05))
        self.traj_len = int(self.config.get("rl", {}).get("traj_len", 4))
        self.entropy_coef = float(self.config.get("rl", {}).get("entropy_coef", 0.01))
        self.best_expr = None
        self.best_reward = -1e9

    # ------------------------------------------------------------------ #
    # Policy sampling helpers
    # ------------------------------------------------------------------ #
    def _policy_probs(self) -> np.ndarray:
        z = self.policy_logits - np.max(self.policy_logits)
        e = np.exp(z)
        return e / e.sum()

    def _sample_trajectory(self):
        probs = self._policy_probs()
        actions_idx = []
        actions_probs = []
        for _ in range(self.traj_len):
            a = int(np.random.choice(len(self.action_space), p=probs))
            actions_idx.append(a)
            actions_probs.append(probs.copy())
        return actions_idx, actions_probs

    def _build_ast(self, actions_idx):
        # Fold operators onto an accumulator to produce a valid nested AST.
        acc = random.choice(self.terminals)
        for a in actions_idx:
            op = self.action_space[a]
            spec = self.operator_specs[op]
            node: Dict[str, Any] = {"op": op}
            if op in WINDOWED_OPERATORS:
                node["window"] = random.choice(self.windows)
            if op in POWERED_OPERATORS:
                node["p"] = random.choice(self.powers)
            if spec["arity"] == 1:
                node["left"] = acc
            else:
                node["left"] = acc
                node["right"] = random.choice(self.terminals)
            acc = node
        return acc

    # ------------------------------------------------------------------ #
    # Search loop hooks
    # ------------------------------------------------------------------ #
    def generate_candidates(self) -> List[FactorExpression]:
        logger.info("RL: Sampling action trajectories from policy...")
        candidates = []
        for _ in range(self.config.get("batch_size", 10)):
            actions_idx, actions_probs = self._sample_trajectory()
            ast = self._build_ast(actions_idx)
            seq = [self.action_space[i] for i in actions_idx]
            expr = RLFactorExpression(
                action_sequence=seq,
                ast_dict=ast,
                parent_ids=["rl_policy"],
            )
            # Stash sampling info for the gradient update.
            expr._rl_actions = actions_idx
            expr._rl_probs = actions_probs
            candidates.append(expr)
        return candidates

    def evaluate_candidates(self, candidates: List[FactorExpression]) -> EvaluationFeedback:
        if self.evaluator:
            return self.evaluator.evaluate(candidates)
        return EvaluationFeedback()

    def update_model(self, candidates: List[FactorExpression], feedback: EvaluationFeedback) -> None:
        logger.info("RL: REINFORCE update of policy logits...")
        grad = np.zeros_like(self.policy_logits)
        rewards = []
        for expr in candidates:
            result = feedback.for_candidate(expr)
            if not result or not result.succeeded:
                continue
            reward = float(expr.metrics.get("fitness_score", 0.0))
            rewards.append(reward)
            actions_idx = getattr(expr, "_rl_actions", None)
            actions_probs = getattr(expr, "_rl_probs", None)
            if actions_idx is None or actions_probs is None:
                continue
            if reward > self.best_reward:
                self.best_reward = reward
                self.best_expr = expr
            advantages = reward - (np.mean(rewards) if rewards else 0.0)
            for a, p in zip(actions_idx, actions_probs):
                # grad_log p_a = e_a - p  (REINFORCE)
                g = -p.copy()
                g[a] += 1.0
                grad += advantages * g
            # Entropy bonus to encourage exploration early on.
            probs = self._policy_probs()
            entropy_grad = (np.log(probs + 1e-12) + 1.0) * probs
            grad -= self.entropy_coef * entropy_grad

        if rewards:
            self.baseline = float(np.mean(rewards))
        grad /= max(1, len(rewards))
        self.policy_logits += self.learning_rate * grad
        # Keep logits bounded for numerical stability.
        self.policy_logits = np.clip(self.policy_logits, -20.0, 20.0)
        logger.info(
            "RL policy (top ops): %s",
            ", ".join(
                f"{self.action_space[i]}={self._policy_probs()[i]:.2f}"
                for i in np.argsort(-self.policy_logits)[:5]
            ),
        )
