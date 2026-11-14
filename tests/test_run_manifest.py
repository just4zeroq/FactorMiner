"""P1: run manifest and registry -- reproducible identity of a mining run."""

from __future__ import annotations

import json
import random
import unittest

import numpy as np

from core.experiment import manifest as mf
from core.experiment import registry as reg


BASE_CONFIG = {
    "paradigm": "GP",
    "data_feeds": {
        "exchange": "binance",
        "instrument_type": "futures",
        "timeframe": "1d",
        "pairs": ["BTC/USDT:USDT"],
        "mine_period": [["2023-04-01", "2024-12-31"]],
        "test_period": [["2025-01-01", "2025-06-30"]],
    },
}


class TestHashing(unittest.TestCase):
    def test_hash_json_is_key_order_independent(self):
        self.assertEqual(mf.hash_json({"a": 1, "b": 2}), mf.hash_json({"b": 2, "a": 1}))

    def test_operator_set_hash_is_stable_and_short(self):
        h = mf.operator_set_hash()
        self.assertEqual(len(h), 12)
        self.assertEqual(h, mf.operator_set_hash())
        int(h, 16)

    def test_git_sha_is_string(self):
        self.assertIsInstance(mf.git_sha(), str)

    def test_new_run_id_unique(self):
        self.assertNotEqual(mf.new_run_id(), mf.new_run_id())


class TestSeed(unittest.TestCase):
    def test_seed_everything_makes_gp_and_rl_sampling_reproducible(self):
        mf.seed_everything(1234)
        a = (random.random(), np.random.choice(5, p=[0.2] * 5), random.choice("abcdef"))
        mf.seed_everything(1234)
        b = (random.random(), np.random.choice(5, p=[0.2] * 5), random.choice("abcdef"))
        self.assertEqual(a, b)

    def test_none_leaves_rng_alone(self):
        self.assertIsNone(mf.seed_everything(None))


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.m = mf.build_manifest(BASE_CONFIG, config_path="cfg.json", n_trials_at_start=787)

    def test_required_fields(self):
        for key in ("schema_version", "run_id", "created_at", "config_hash", "git_sha",
                    "operator_set_hash", "data_snapshot", "windows", "universe",
                    "n_trials_at_start", "environment"):
            self.assertIn(key, self.m, key)
        self.assertEqual(self.m["n_trials_at_start"], 787)
        self.assertEqual(self.m["windows"]["test"], BASE_CONFIG["data_feeds"]["test_period"])

    def test_data_snapshot_has_content_hash(self):
        snap = self.m["data_snapshot"]
        self.assertIn("hash", snap)
        self.assertGreaterEqual(snap["n_files"], 0)
        self.assertLessEqual(len(snap["files"]), 8)

    def test_summary_is_compact(self):
        s = mf.manifest_summary(self.m)
        self.assertEqual(set(s), {"run_id", "config_hash", "git_sha",
                                  "operator_set_hash", "data_snapshot_hash", "seed"})
        self.assertEqual(mf.manifest_summary(None), {})

    def test_verify_detects_config_drift(self):
        changed = dict(BASE_CONFIG, population_size=999)
        self.assertFalse(mf.verify_manifest(self.m, changed)["reproducible"])

    def test_verify_detects_operator_set_change(self):
        stale = dict(self.m, operator_set_hash="deadbeefdead")
        result = mf.verify_manifest(stale, BASE_CONFIG)
        self.assertIn("operator_set_hash", result["mismatches"])

    def test_verify_accepts_identical_config(self):
        self.assertTrue(mf.verify_manifest(self.m, BASE_CONFIG)["reproducible"])

    def test_extra_fields_and_llm_note(self):
        m = mf.build_manifest(dict(BASE_CONFIG, paradigm="LLM"), extra={"tag": "x"})
        self.assertEqual(m["tag"], "x")
        self.assertTrue(m["notes"]["llm_nondeterministic"])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.m = mf.build_manifest(BASE_CONFIG)
        self.rid = self.m["run_id"]
        self.path = reg.create_run(self.m, BASE_CONFIG, config_path="cfg.json", root=self.root)
        self.addCleanup(self._tmp.cleanup)

    def test_creates_manifest_config_status(self):
        for name in ("manifest.json", "config.json", "status.json"):
            self.assertTrue((self.path / name).exists(), name)
        status = reg.read_status(self.rid, root=self.root)
        self.assertEqual(status["status"], "created")
        self.assertEqual(status["completed_stages"], ["created"])

    def test_update_status_is_idempotent(self):
        reg.update_status(self.rid, "mining", root=self.root)
        reg.update_status(self.rid, "mining", artifacts={"factors": "4"}, root=self.root)
        s = reg.read_status(self.rid, root=self.root)
        self.assertEqual(s["completed_stages"], ["created", "mining"])
        self.assertEqual(s["artifacts"]["factors"], "4")

    def test_completed_stage_sets_terminal(self):
        reg.update_status(self.rid, "completed", root=self.root)
        self.assertEqual(reg.read_status(self.rid, root=self.root)["status"], "completed")

    def test_find_resumable_matches_config_hash(self):
        self.assertEqual(reg.find_resumable(self.m["config_hash"], root=self.root), self.rid)
        reg.update_status(self.rid, "completed", root=self.root)
        self.assertIsNone(reg.find_resumable(self.m["config_hash"], root=self.root))
        self.assertIsNone(reg.find_resumable("nope", root=self.root))

    def test_list_runs_and_artifact(self):
        rows = reg.list_runs(root=self.root)
        self.assertEqual(rows[0]["run_id"], self.rid)
        p = reg.write_artifact(self.rid, "metrics.json", {"sharpe": 1.2}, root=self.root)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["sharpe"], 1.2)


if __name__ == "__main__":
    unittest.main()
