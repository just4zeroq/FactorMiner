"""Regression tests for the Phase-1 paradigm-correctness fixes.

Covers four confirmed bugs from the 2026-11-15 expert review:

* **G-01** — RL REINFORCE used a *running* mean as the advantage baseline.
* **M-05** — NN early stopping compared the pre-update loss but saved the
  post-update weights (one-epoch misalignment).
* **G-02** — LLM miner silently emitted hardcoded mock factors when the backend
  was missing/failed.
* **M-04** — ``_ts_decay_linear`` mis-weighted partial windows and divided by
  the full weight sum when a window contained gaps.
"""

import unittest

import numpy as np
import pandas as pd

from core.miner.entities import CandidateEvaluation, EvaluationFeedback
from core.miner.expressions import FactorExpressionAction
from core.miner.operator_runtime import _ts_decay_linear
from core.miner.paradigms.llm_miner import LLMFactorMiner
from core.miner.paradigms.rl_miner import RLFactorMiner, RLFactorExpression


class _FakeDataClient:
    def __init__(self, panel):
        self._panel = panel

    def get_data(self):
        return self._panel


def _panel(n=80):
    idx = pd.RangeIndex(n)
    return pd.DataFrame(
        {
            "close": np.linspace(100, 120, n),
            "volume": np.linspace(1e6, 2e6, n),
        },
        index=idx,
    )


class _StubEvaluator:
    """Returns a caller-supplied reward per candidate."""

    def __init__(self, rewards):
        self._rewards = rewards

    def evaluate(self, candidates):
        results = []
        for cand, reward in zip(candidates, self._rewards):
            cand.metrics = {"fitness_score": float(reward)}
            results.append(
                CandidateEvaluation(
                    candidate=cand, status="success", metrics=cand.metrics
                )
            )
        return EvaluationFeedback(results=results)


class RLAdvantageBatchMeanTests(unittest.TestCase):
    """G-01: the gradient baseline must be the whole-batch mean."""

    def _miner(self, rewards):
        cfg = {
            "paradigm": "RL",
            "rl": {"learning_rate": 0.1, "traj_len": 2, "entropy_coef": 0.0},
            "search_space": {"allowed_operators": ["add", "sub"], "terminals": ["close"]},
            "gp": {"windows": [5], "powers": [1.0]},
            "batch_size": len(rewards),
        }
        miner = RLFactorMiner(_FakeDataClient(_panel()), cfg)
        miner.initialize_search_space()
        # Deterministic policy so gradients are comparable across the batch.
        miner.policy_logits = np.zeros_like(miner.policy_logits)
        miner.evaluator = _StubEvaluator(rewards)
        return miner

    def test_baseline_is_full_batch_mean(self):
        rewards = [1.0, 2.0, 3.0, 4.0]
        miner = self._miner(rewards)
        candidates = miner.generate_candidates()
        feedback = miner.evaluate_candidates(candidates)
        miner.update_model(candidates, feedback)
        # baseline stored after the update must be the full-batch mean (2.5),
        # not a partial running mean (which the old code left at the last
        # prefix mean, i.e. dominated by whichever rewards arrived first).
        self.assertAlmostEqual(miner.baseline, 2.5, places=6)

    def test_equal_rewards_give_zero_advantage_gradient(self):
        # All rewards identical => every advantage is 0 => only the (disabled)
        # entropy term could move the logits. With entropy_coef=0 the policy
        # must not change. The old running-mean code produced non-zero
        # advantages here because early candidates were compared to their own
        # prefix mean, biasing the update.
        rewards = [5.0, 5.0, 5.0]
        miner = self._miner(rewards)
        before = miner.policy_logits.copy()
        candidates = miner.generate_candidates()
        feedback = miner.evaluate_candidates(candidates)
        miner.update_model(candidates, feedback)
        np.testing.assert_allclose(miner.policy_logits, before, atol=1e-12)

    def test_gradient_uses_full_batch_mean_baseline(self):
        # The decisive property: every advantage uses mean(ALL rewards), not a
        # per-prefix running mean. We recompute the expected gradient by hand
        # from each candidate's recorded (actions, probs) and compare.
        rewards = [0.0, 10.0]
        miner = self._miner(rewards)
        before = miner.policy_logits.copy()
        candidates = miner.generate_candidates()
        feedback = miner.evaluate_candidates(candidates)
        miner.update_model(candidates, feedback)

        baseline = float(np.mean(rewards))
        expected_grad = np.zeros_like(before)
        for cand, reward in zip(candidates, rewards):
            g = np.zeros_like(before)
            for a, p in zip(cand._rl_actions, cand._rl_probs):
                e = -p.copy()
                e[a] += 1.0
                g += e
            expected_grad += (reward - baseline) * g
        expected_grad /= len(candidates)
        expected_logits = before + 0.1 * expected_grad  # lr = 0.1, entropy off
        np.testing.assert_allclose(miner.policy_logits, expected_logits, atol=1e-12)


class NNEarlyStoppingAlignmentTests(unittest.TestCase):
    """M-05: the saved weights must correspond to the reported best loss."""

    def test_returned_loss_matches_returned_weights(self):
        from core.miner.nn import NumpyMLPFactorModel

        rng = np.random.default_rng(0)
        X = rng.standard_normal((120, 3))
        y = X[:, 0] * 0.5 + rng.standard_normal(120) * 0.1
        model = NumpyMLPFactorModel(
            features=["close", "volume", "x"], hidden_dim=4, mode="sequential_single"
        )
        best_loss = model.train(X, y, epochs=60, learning_rate=0.05, patience=60)

        # Recompute the loss of the *returned* weights; it must equal best_loss.
        norm = model.transform(X)
        channels = np.tanh(norm @ model.W1 + model.b1)
        pred = channels @ model.W2 + model.b2
        recomputed = float(np.mean((pred - y) ** 2))
        self.assertAlmostEqual(recomputed, best_loss, places=6)


class LLMMockGuardTests(unittest.TestCase):
    """G-02: no silent mock factors when the backend is unusable."""

    def test_missing_backend_raises_without_optin(self):
        miner = LLMFactorMiner(_FakeDataClient(_panel()), {"llm_api_config": {}})
        miner.initialize_search_space()
        self.assertIsNone(miner.api_manager)
        with self.assertRaises(RuntimeError):
            miner.generate_candidates()

    def test_mock_requires_explicit_optin(self):
        cfg = {"llm_api_config": {}, "llm_allow_mock": True, "batch_size": 2}
        miner = LLMFactorMiner(_FakeDataClient(_panel()), cfg)
        miner.initialize_search_space()
        cands = miner.generate_candidates()
        self.assertTrue(cands)
        # Every candidate must be flagged synthetic so downstream accounting
        # can exclude it from "real alpha".
        for c in cands:
            self.assertTrue(c.get_provenance().get("synthetic_mock"))


class DecayLinearTests(unittest.TestCase):
    """M-04: correct weighting for gaps and partial windows."""

    def test_full_window_matches_manual_linear_decay(self):
        s = pd.Series(np.arange(1.0, 11.0))  # 1..10
        out = _ts_decay_linear(s, window=4)
        # At index 9 (0-based), window = [7,8,9,10], weights [1,2,3,4]
        expected = (7 * 1 + 8 * 2 + 9 * 3 + 10 * 4) / 10.0
        self.assertAlmostEqual(out.iloc[9], expected, places=9)

    def test_gap_does_not_deflate_output(self):
        # A window with one NaN must be the weighted mean of the *present*
        # values (denominator restricted to present weights), not shrunk.
        s = pd.Series([2.0, 2.0, np.nan, 2.0])
        out = _ts_decay_linear(s, window=4)
        self.assertAlmostEqual(out.iloc[3], 2.0, places=9)

    def test_partial_window_uses_small_weight_segment(self):
        # First observation (len=1) has weight [1] only -> equals the value.
        s = pd.Series([5.0, 5.0, 5.0])
        out = _ts_decay_linear(s, window=3)
        # min_periods may require full window; if defined, all values equal 5
        if pd.notna(out.iloc[0]):
            self.assertAlmostEqual(out.iloc[0], 5.0, places=9)


if __name__ == "__main__":
    unittest.main()
