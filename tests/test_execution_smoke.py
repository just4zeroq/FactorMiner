"""Smoke coverage for ``core.execution`` (review finding T8).

``core/execution/compiler.py`` and ``core/execution/inspector.py`` had no direct
import test, so a break there would only surface through unrelated failures.
This keeps them in the import smoke net.
"""

from __future__ import annotations

import importlib
import unittest


class TestExecutionImportSmoke(unittest.TestCase):
    def test_compiler_and_inspector_import(self):
        compiler = importlib.import_module("core.execution.compiler")
        inspector = importlib.import_module("core.execution.inspector")
        self.assertTrue(hasattr(compiler, "FactorCompiler"))
        self.assertTrue(hasattr(compiler, "FactorExpressionAST"))
        self.assertTrue(hasattr(compiler, "RestrictedSandbox"))
        self.assertTrue(hasattr(inspector, "FactorInspector"))

    def test_analysis_module_imports(self):
        # Already exercised by test_factor_analytics, but keep it in the import net.
        mod = importlib.import_module("core.analysis.factor_analytics")
        self.assertTrue(hasattr(mod, "SnapshotAnalysisError"))


if __name__ == "__main__":
    unittest.main()
