import os
import tempfile
import unittest
from unittest.mock import MagicMock

import pandas as pd

from core.evaluation.significance import apply_promotion_gates
from core.miner.director import FactorMinerDirector
from core.miner.expressions import FactorExpressionAST


class ApplyPromotionGatesTest(unittest.TestCase):
    def test_all_green_inspected(self):
        metrics = [{
            "hac_t": 3.5, "p_value": 0.001, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
        }]
        out = apply_promotion_gates(metrics)
        self.assertEqual(out[0]["status"], "INSPECTED")
        self.assertTrue(out[0]["gate"]["pass"])

    def test_weak_p_adj_unverified(self):
        metrics = [{
            "hac_t": 3.5, "p_value": 0.4, "dsr": 0.97,
            "oos_consistent": True, "cost_adj_return": 0.05,
        }]
        out = apply_promotion_gates(metrics)
        self.assertEqual(out[0]["status"], "DISCOVERED_UNVERIFIED")

    def test_oos_inconsistent_unverified(self):
        metrics = [{
            "hac_t": 3.5, "p_value": 0.001, "dsr": 0.97,
            "oos_consistent": False, "cost_adj_return": 0.05,
        }]
        out = apply_promotion_gates(metrics)
        self.assertEqual(out[0]["status"], "DISCOVERED_UNVERIFIED")

    def test_fdr_corrects_lucky_p_among_many(self):
        # One tiny raw p among many nulls should be corrected upward by BH-FDR.
        metrics = [{"hac_t": 4.0, "p_value": 0.0001, "dsr": 0.99,
                    "oos_consistent": True, "cost_adj_return": 0.05}]
        for _ in range(40):
            metrics.append({"hac_t": 0.2, "p_value": 0.6, "dsr": 0.1,
                            "oos_consistent": True, "cost_adj_return": -0.01})
        out = apply_promotion_gates(metrics)
        # the lucky one's adjusted p should exceed its raw p (conservative)
        self.assertGreater(out[0]["p_adj"], 0.0001)

    def test_dsr_deflated_by_cumulative_trials(self):
        # Same factor, same Sharpe: the more ideas were tried, the harder the
        # DSR bar. This is what makes num_trials a real multiple-testing
        # denominator instead of a decorative counter.
        metrics = [{
            "hac_t": 3.5, "p_value": 0.001, "dsr": 0.99,
            "ir": 0.10, "n_obs": 240,
            "oos_consistent": True, "cost_adj_return": 0.05,
        }]
        single = apply_promotion_gates(metrics, n_trials=1)
        searched = apply_promotion_gates(metrics, n_trials=5000)
        self.assertLess(searched[0]["dsr"], single[0]["dsr"])
        self.assertEqual(single[0]["status"], "INSPECTED")
        self.assertEqual(searched[0]["status"], "DISCOVERED_UNVERIFIED")
        self.assertFalse(searched[0]["gate"]["dsr_pass"])

    def test_precomputed_dsr_used_when_trials_unknown(self):
        metrics = [{
            "hac_t": 3.5, "p_value": 0.001, "dsr": 0.97,
            "ir": 0.01, "n_obs": 240,
            "oos_consistent": True, "cost_adj_return": 0.05,
        }]
        out = apply_promotion_gates(metrics)
        self.assertAlmostEqual(out[0]["dsr"], 0.97)
        self.assertEqual(out[0]["status"], "INSPECTED")


class _TestFactor(FactorExpressionAST):
    def __init__(self, metrics):
        super().__init__(ast_dict={"op": "close"})
        self.metrics = metrics

    def compute(self, data):
        return data

    def get_source(self):
        return {"op": "close"}


class _Data:
    def get_data(self):
        return pd.DataFrame({"A": [1.0, 2.0, 3.0]})
    def get_returns(self):
        return pd.DataFrame({"A": [0.01, -0.02, 0.03]})
    def get_test_data(self):
        return None
    def get_test_returns(self):
        return None


class DirectorGatingTest(unittest.TestCase):
    def setUp(self):
        self.config = {"paradigm": "GP", "evaluation": {}}
        self.director = FactorMinerDirector(self.config, _Data())
        # Isolate persistence + trace.
        self.director.storage_client = MagicMock()
        self.director.storage_client.save_factor_snapshot.return_value = True
        self.director.trace = MagicMock()
        self.director.trace.num_trials = 0
        self.saved_meta = []
        def _save(src, meta):
            self.saved_meta.append(meta)
            return None
        self.director.storage_client.save_gp_factor.side_effect = _save

    def test_lifecycle_status_assigned(self):
        strong = _TestFactor({"hac_t": 3.5, "p_value": 0.001, "dsr": 0.97,
                              "oos_consistent": True, "cost_adj_return": 0.05,
                              "RankIC": 0.06, "RankIC_oos": 0.05,
                              "oos_enabled": True})
        weak = _TestFactor({"hac_t": 0.3, "p_value": 0.6, "dsr": 0.1,
                            "oos_consistent": True, "cost_adj_return": -0.01,
                            "RankIC": 0.01, "RankIC_oos": 0.005,
                            "oos_enabled": True})
        self.director._save_excellent_factors([strong, weak])
        statuses = [m.lifecycle_status for m in self.saved_meta]
        self.assertEqual(statuses, ["INSPECTED", "DISCOVERED_UNVERIFIED"])

    def test_p_adj_written_and_trace_called(self):
        strong = _TestFactor({"hac_t": 3.5, "p_value": 0.001, "dsr": 0.97,
                              "oos_consistent": True, "cost_adj_return": 0.05,
                              "RankIC_oos": 0.05, "oos_enabled": True})
        weak = _TestFactor({"hac_t": 0.3, "p_value": 0.6, "dsr": 0.1,
                            "oos_consistent": True, "cost_adj_return": -0.01,
                            "RankIC_oos": 0.005, "oos_enabled": True})
        self.director._save_excellent_factors([strong, weak])
        self.assertIn("p_adj", strong.metrics)
        # cumulative trial count is persisted with the factor for auditability
        self.assertEqual(strong.metrics["n_trials"], 2)
        self.assertTrue(self.director.trace.record.called)

    def test_gate_thresholds_honored_from_config(self):
        # evaluation.gates.oos_rankic_min must actually reach the promotion
        # gate (before this wiring, config thresholds were silently ignored).
        cfg = {"paradigm": "GP",
               "evaluation": {"gates": {"oos_rankic_min": 0.10}}}
        d = FactorMinerDirector(cfg, _Data())
        d.storage_client = MagicMock()
        d.storage_client.save_factor_snapshot.return_value = True
        d.trace = MagicMock()
        d.trace.num_trials = 0
        saved = []
        d.storage_client.save_gp_factor.side_effect = lambda src, meta: saved.append(meta)
        f = _TestFactor({"hac_t": 3.5, "p_value": 0.001, "dsr": 0.97,
                         "oos_consistent": True, "cost_adj_return": 0.05,
                         "RankIC_oos": 0.05, "oos_enabled": True})
        d._save_excellent_factors([f])
        self.assertEqual(saved[0].lifecycle_status, "DISCOVERED_UNVERIFIED")

    def test_storage_root_honored_from_config(self):
        # A configured storage.db_root must actually redirect the Director's
        # storage client (regression: get_global_storage ignored it before).
        cfg = {"paradigm": "GP", "evaluation": {},
               "storage": {"db_root": "user_workspace/_cfg_root_smoke"}}
        d = FactorMinerDirector(cfg, _Data())
        self.assertTrue(
            d.storage_client.meta_dir.replace("\\", "/").endswith(
                "user_workspace/_cfg_root_smoke/metadata"
            )
        )


if __name__ == "__main__":
    unittest.main()
