"""Regression tests for T-05 / T-08 (research scripts must not lie).

The three report scripts used to embed the numbers of the 2025 run directly in
their prose and always exited 0. These tests pin:
* conclusions are generated from the passed-in data, so changing the data
  changes every figure in the text;
* no historical figure is left hard-coded in the script sources;
* missing/incomplete input makes the script fail with a non-zero exit;
* the re-audit report's sensitivity table and conclusions are data-driven.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

OOS_REPORT = "user_workspace/experiment_tools/summarize_oos_validation.py"
REAUDIT = "scripts/reaudit_factor_db.py"
GATES = "user_workspace/experiment_tools/validate_gates.py"


def _load(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_runs(*, zero_oos=-7.37, zero_is=-10.41, cost_oos=-11.18,
              leaked=-6.59, sr_oos=-1.14, sr_is=-0.55) -> pd.DataFrame:
    return pd.DataFrame([
        {"scenario": "IS 2024, with costs", "total_return_pct": -17.87, "sharpe": sr_is},
        {"scenario": "IS 2024, zero cost", "total_return_pct": zero_is, "sharpe": sr_is},
        {"scenario": "OOS 2025H1 (leaked direction), with costs",
         "total_return_pct": leaked, "sharpe": sr_oos},
        {"scenario": "STRICT OOS 2025H1, with costs",
         "total_return_pct": cost_oos, "sharpe": sr_oos},
        {"scenario": "STRICT OOS 2025H1, zero cost",
         "total_return_pct": zero_oos, "sharpe": sr_oos},
    ])


def make_ics(n: int = 6, flips: int = 2) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "factor_id": f"fac_{i}",
            "IS_RankIC": 0.05 + i * 0.001,
            "OOS_RankIC": 0.02 + i * 0.001,
            "direction_flipped": i < flips,
        }
        for i in range(n)
    ])


class TestOosConclusions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("oos_report_under_test", OOS_REPORT)

    def test_every_number_follows_the_data(self):
        baseline = "\n".join(self.mod.build_conclusions(make_runs(), make_ics()))
        self.assertIn("-7.37%", baseline)
        self.assertIn("-11.18%", baseline)
        self.assertIn("-1.14", baseline)
        self.assertIn("2/6 个因子方向翻转", baseline)

        changed = "\n".join(self.mod.build_conclusions(
            make_runs(zero_oos=-1.23, cost_oos=-2.34, leaked=3.45,
                      sr_oos=0.77, sr_is=-0.55),
            make_ics(),
        ))
        self.assertIn("-1.23%", changed)
        self.assertIn("-2.34%", changed)
        self.assertIn("3.45%", changed)
        self.assertIn("0.77", changed)
        self.assertNotIn("-7.37%", changed)
        self.assertNotIn("-11.18%", changed)
        # The qualitative verdict flips when the strict OOS Sharpe is better
        # than the in-sample one; it must not stay hard-coded as "更差".
        self.assertIn("样本外表现不劣于样本内", changed)
        self.assertNotIn("样本外表现劣于样本内", changed)

    def test_factor_count_follows_the_data(self):
        text = "\n".join(self.mod.build_conclusions(make_runs(), make_ics(n=3, flips=3)))
        self.assertIn("3/3 个因子方向翻转", text)
        self.assertIn("这 3 个因子", text)

    def test_missing_scenario_is_an_error(self):
        runs = make_runs().drop(index=4)  # strict OOS zero-cost row
        with self.assertRaises(ValueError):
            self.mod.build_conclusions(runs, make_ics())

    def test_empty_input_makes_main_fail(self):
        with patch.object(self.mod, "collect_runs", return_value=pd.DataFrame()), \
             patch.object(self.mod, "collect_factor_ic", return_value=make_ics()):
            self.assertEqual(self.mod.main(), 1)

    def test_bad_data_makes_main_fail(self):
        with patch.object(self.mod, "collect_runs", return_value=make_runs().drop(index=4)), \
             patch.object(self.mod, "collect_factor_ic", return_value=make_ics()):
            self.assertEqual(self.mod.main(), 1)


class TestReauditConclusions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("reaudit_under_test", REAUDIT)

    @staticmethod
    def _rows(n=3):
        return [
            {
                "factor_id": f"fac_{i}",
                "old": "INSPECTED",
                "new": "INSPECTED" if i == 0 else "DISCOVERED_UNVERIFIED",
                "mode": "panel",
                "rankic_is": 0.05,
                "rankic_oos": 0.02,
                "hac_t": 2.1,
                "p_adj": 0.03,
                "dsr": 0.6,
                "net": -0.001 * i,
                "ls_t": 1.0,
                "fail": "dsr_pass,cost_adj_positive" if i else "-",
                "_gate_input": {},
                "_gate_cfg": {},
            }
            for i in range(n)
        ]

    def test_report_numbers_come_from_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.md"
            with patch.object(self.mod, "apply_promotion_gates",
                              return_value=[{"status": "DISCOVERED_UNVERIFIED"}]):
                sensitivity = self.mod._write_report(self._rows(), n_trials=42, path=str(out))
            text = out.read_text(encoding="utf-8")
        self.assertIn("**1** / 3", text)
        self.assertIn("当前 3 个因子高度相关", text)
        self.assertIn("`dsr_pass` 为主", text)
        self.assertIn("3 个因子成本后净收益 ≤ 0", text)
        self.assertNotIn("84 个因子", text)
        self.assertNotIn("通过数也只有 1 个 ——", text)
        self.assertEqual(sensitivity[1], 0)
        self.assertEqual(set(sensitivity), {42, 20, 6, 1})

    def test_sensitivity_pass_count_is_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.md"
            # A4: the sensitivity sweep now batches the whole library in one
            # call, so the mock must return one verdict per input.
            def _all_pass(inputs, **kwargs):
                return [{"status": "INSPECTED", "p_adj": 0.0, "dsr": 1.0}
                        for _ in inputs]

            with patch.object(self.mod, "apply_promotion_gates", side_effect=_all_pass):
                sensitivity = self.mod._write_report(self._rows(), n_trials=10, path=str(out))
            text = out.read_text(encoding="utf-8")
        self.assertEqual(sensitivity[1], 3)
        self.assertIn("**3** / 3", text)


class TestScriptHygiene(unittest.TestCase):
    FORBIDDEN = (
        "-7.37%", "-10.41%", "-11.18%", "-6.59%", "11.18%",
        "t≈4.2", "-6.4%", "84 个因子", "6/6 拦截",
        'reaudited_at"] = "2026-11-14"',
    )

    def test_no_historical_numbers_are_baked_into_sources(self):
        for rel in (OOS_REPORT, REAUDIT, GATES):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for token in self.FORBIDDEN:
                self.assertNotIn(token, text, f"{rel} 仍写死历史数字 {token}")

    def test_scripts_have_a_nonzero_exit_path(self):
        for rel in (OOS_REPORT, REAUDIT, GATES):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertTrue(
                "raise SystemExit" in text or "return 1" in text,
                f"{rel} 没有失败退出路径",
            )


if __name__ == "__main__":
    unittest.main()
