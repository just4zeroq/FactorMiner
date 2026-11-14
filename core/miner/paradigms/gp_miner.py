import copy
import logging
import random
from typing import Any, Dict, List

from core.miner.paradigms.base import BaseFactorMiner
from core.miner.expressions import FactorExpression, FactorExpressionAST
from core.miner.entities import EvaluationFeedback
from core.miner.operator_runtime import (
    configured_operator_names,
    resolve_operator_specs,
    sample_operator_params,
    PARAM_KEYS,
)
from core.miner.crypto_features import (
    DEFAULT_CRYPTO_WINDOWS,
    build_crypto_feature_asts,
    crypto_terminals,
)

logger = logging.getLogger(__name__)

TERMINALS_DEFAULT = ["close", "open", "high", "low", "volume"]


class GPFactorMiner(BaseFactorMiner):
    """
    遗传规划(GP)因子挖掘器 (P0 upgrade: 真·遗传算子)

    相比原来的"外层再包一个算子"伪交叉 + 仅留 top-half，本实现具备：
      * 随机初始树（含 ephemeral 常数终端 + 参数化窗口）
      * 子树交叉 (subtree crossover)
      * 锦标赛选择 (tournament selection)
      * 变异 (mutation：子树重生成 / 算子或窗口扰动)
      * 精英保留 (elitism)
      * 简约度惩罚 (parsimony：深度越深 fitness 打折，抑制 bloat)
    """

    def initialize_search_space(self) -> None:
        logger.info("Initializing GP search space...")
        self.allowed_operators = configured_operator_names(self.config)
        self.operator_specs = resolve_operator_specs(self.allowed_operators)
        self.population_size = int(self.config.get("population_size", 20))

        gp = self.config.get("gp", {})
        self.terminals = list(self.config.get("search_space", {}).get("terminals", TERMINALS_DEFAULT))
        if not self.terminals:
            self.terminals = list(TERMINALS_DEFAULT)
        # Crypto-native streams present in the loaded panel become terminals
        # automatically, so GP can mutate/crossover into crypto-native trees.
        panel = self._data_panel()
        self.terminals = crypto_terminals(self.terminals, panel)

        # Seed the initial population with curated crypto-native features
        # (funding / OI / basis / liquidation templates).  Inactive when no
        # crypto stream is present, so OHLCV-only runs are unchanged.
        self.crypto_seed_ratio = float(gp.get("crypto_seed_ratio", 0.3))
        self.crypto_windows = [
            int(w) for w in gp.get("crypto_windows", list(DEFAULT_CRYPTO_WINDOWS))
        ]
        self._crypto_asts = (
            build_crypto_feature_asts(data=panel, windows=self.crypto_windows)
            if panel is not None and self.crypto_seed_ratio > 0
            else []
        )
        if self._crypto_asts:
            logger.info(
                "GP: %d crypto-native seed ASTs available (terminals: %s)",
                len(self._crypto_asts), ", ".join(
                    t for t in self.terminals if t not in TERMINALS_DEFAULT
                ) or "none",
            )

        self.max_depth = int(gp.get("max_depth", 4))
        self.const_prob = float(gp.get("const_prob", 0.2))
        self.windows = [int(w) for w in gp.get("windows", [5, 10, 20, 60])]
        self.powers = [float(p) for p in gp.get("powers", [1.0, 2.0, 3.0])]
        self.tournament_k = int(gp.get("tournament_k", 3))
        self.crossover_prob = float(gp.get("crossover_prob", 0.8))
        self.mutation_prob = float(gp.get("mutation_prob", 0.3))
        self.parsimony = float(gp.get("parsimony", 0.05))
        self.elite_count = int(gp.get("elite_count", max(1, self.population_size // 10)))
        self.survival_count = int(self.config.get("survival_count", self.population_size // 2))

    def _data_panel(self):
        """Best-effort access to the panel the miners evaluate on."""
        try:
            if hasattr(self.data, "get_data"):
                return self.data.get_data()
            return self.data
        except Exception as exc:
            logger.warning("GP: unable to inspect data panel for crypto streams: %s", exc)
            return None

    def _initial_population(self) -> List[FactorExpression]:
        """First generation: crypto-native seeds first, random ASTs to fill."""
        n = self.population_size
        seeds: List[FactorExpression] = []
        if self._crypto_asts and self.crypto_seed_ratio > 0:
            n_seed = min(len(self._crypto_asts), int(round(n * self.crypto_seed_ratio)))
            if n_seed > 0:
                picks = random.sample(self._crypto_asts, n_seed)
                seeds = [FactorExpressionAST(ast_dict=a) for a in picks]
        n_random = max(0, n - len(seeds))
        randoms = [
            FactorExpressionAST(ast_dict=self._random_ast()) for _ in range(n_random)
        ]
        if seeds:
            logger.info(
                "GP: initial population seeded with %d crypto-native features", len(seeds)
            )
        return seeds + randoms

    # ------------------------------------------------------------------ #
    # AST construction
    # ------------------------------------------------------------------ #
    def _random_terminal(self) -> Any:
        if random.random() < self.const_prob:
            # Ephemeral random constant terminal.
            return round(random.uniform(-2.0, 2.0), 3)
        return random.choice(self.terminals)

    def _random_ast(self, depth: int = 0) -> Any:
        if depth >= self.max_depth:
            return self._random_terminal()
        op = random.choice(self.allowed_operators)
        spec = self.operator_specs[op]
        node: Dict[str, Any] = {"op": op}
        node.update(sample_operator_params(op))
        node["left"] = self._random_ast(depth + 1)
        if spec["arity"] == 2:
            node["right"] = self._random_ast(depth + 1)
        return node

    @staticmethod
    def _depth(node: Any) -> int:
        if not isinstance(node, dict) or "op" not in node:
            return 1
        left = GPFactorMiner._depth(node.get("left"))
        right = GPFactorMiner._depth(node.get("right"))
        return 1 + max(left, right)

    def _adjusted_fitness(self, expr: FactorExpression) -> float:
        base = float(expr.metrics.get("fitness_score", -1e9))
        depth = self._depth(expr.get_source())
        return base / (1.0 + self.parsimony * depth)

    # ------------------------------------------------------------------ #
    # Genetic operators
    # ------------------------------------------------------------------ #
    @staticmethod
    def _all_subtrees(node: Any) -> List[Dict]:
        refs: List[Dict] = []
        if isinstance(node, dict) and "op" in node:
            refs.append(node)
            for key in ("left", "right"):
                child = node.get(key)
                if isinstance(child, dict):
                    refs.extend(GPFactorMiner._all_subtrees(child))
        return refs

    def _tournament(self) -> FactorExpressionAST:
        contenders = random.sample(
            self.state.population, min(self.tournament_k, len(self.state.population))
        )
        return max(contenders, key=self._adjusted_fitness)

    def _crossover(self, p1: Dict, p2: Dict) -> Dict:
        child = copy.deepcopy(p1)
        if not isinstance(p2, dict):
            return child
        p2_sub = random.choice(self._all_subtrees(p2))
        targets = self._all_subtrees(child)
        if not targets:
            return child
        tgt = random.choice(targets)
        tgt.clear()
        tgt.update(copy.deepcopy(p2_sub))
        return child

    def _mutate(self, node: Dict) -> Dict:
        node = copy.deepcopy(node)
        subs = self._all_subtrees(node)
        if not subs:
            return self._random_ast()
        tgt = random.choice(subs)
        if random.random() < 0.5:
            # Regenerate the whole subtree.
            new_sub = self._random_ast()
            tgt.clear()
            if isinstance(new_sub, dict):
                tgt.update(new_sub)
            else:
                tgt.clear()
                tgt.update(self._random_ast())
        else:
            op = tgt.get("op")
            # Re-sample this operator's parameters (window / power / lag / ...).
            tgt.update(sample_operator_params(op))
            # Occasionally swap to another operator of the same arity.
            if random.random() < 0.3:
                new_op = random.choice(self.allowed_operators)
                if self.operator_specs[new_op]["arity"] == self.operator_specs[op]["arity"]:
                    tgt["op"] = new_op
                    for k in PARAM_KEYS:
                        tgt.pop(k, None)
                    tgt.update(sample_operator_params(new_op))
        return node

    # ------------------------------------------------------------------ #
    # Search loop hooks
    # ------------------------------------------------------------------ #
    def generate_candidates(self) -> List[FactorExpression]:
        logger.info("GP: Generating candidates (ASTs)...")
        if not self.state.population:
            return self._initial_population()

        # Offspring only; elites are carried over inside update_model (and exempt
        # from the dedup filter via the preserved-set mechanism in BaseFactorMiner).
        n_offspring = max(1, self.population_size - self.elite_count)
        candidates: List[FactorExpression] = []
        for _ in range(n_offspring):
            p1 = self._tournament()
            p2 = self._tournament()
            child_src = p1.get_source()
            if random.random() < self.crossover_prob:
                child_src = self._crossover(p1.get_source(), p2.get_source())
            if random.random() < self.mutation_prob:
                child_src = self._mutate(child_src)
            candidates.append(
                FactorExpressionAST(
                    ast_dict=child_src,
                    parent_ids=[
                        p1.get_lineage_parents(),
                        p2.get_lineage_parents(),
                    ],
                )
            )
        return candidates

    def evaluate_candidates(self, candidates: List[FactorExpression]) -> EvaluationFeedback:
        if self.evaluator:
            return self.evaluator.evaluate(candidates)
        return EvaluationFeedback()

    def update_model(self, candidates: List[FactorExpression], feedback: EvaluationFeedback) -> None:
        logger.info("GP: Selecting next generation (elitism + tournament survivors)...")
        scored = []
        for expr in candidates:
            result = feedback.for_candidate(expr)
            if result and result.succeeded:
                scored.append((expr.metrics.get("fitness_score", -1e9), expr))

        # Carry over elites from the previous population (their metrics persist).
        prev = self.state.population or []
        elites = sorted(prev, key=lambda e: e.metrics.get("fitness_score", -1e9), reverse=True)
        elites = elites[: self.elite_count]

        pool = [(e.metrics.get("fitness_score", -1e9), e) for e in elites] + scored
        pool.sort(key=lambda x: x[0], reverse=True)
        self.state.population = [e for _, e in pool[: self.population_size]]
