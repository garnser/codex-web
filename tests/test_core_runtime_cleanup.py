from __future__ import annotations

import ast
import unittest
from pathlib import Path

from codex_web import application
from codex_web.runtime import core
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.autonomy import AutonomyService


class CoreRuntimeCleanupTests(unittest.TestCase):
    def test_application_composes_extracted_runtime_owners(self) -> None:
        self.assertIs(core.codex, application.codex_runtime)
        self.assertIs(application.app.state.codex_runtime, application.codex_runtime)
        self.assertIsInstance(core.codex, CodexRuntime)

        self.assertIs(application.app.state.autonomy_service, application.autonomy_service)
        self.assertIsInstance(application.autonomy_service, AutonomyService)
        self.assertIs(
            core._run_owner_work_watchdog_cycle.__self__,
            application.autonomy_service,
        )
        self.assertIs(
            core._run_work_item_sla_cycle.__self__,
            application.autonomy_service,
        )

    def test_core_no_longer_defines_extracted_implementations(self) -> None:
        source = Path(core.__file__).read_text()
        tree = ast.parse(source)
        definitions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        removed = {
            "CodexAppServer",
            "_codex_request_timeout",
            "_run_owner_work_watchdog_cycle",
            "_run_release_gate_watchdog_cycle",
            "_run_work_item_sla_cycle",
            "_run_orchestrator_watchdog_cycle",
            "_run_split_brain_watchdog_cycle",
        }

        self.assertTrue(removed.isdisjoint(definitions), removed & definitions)


if __name__ == "__main__":
    unittest.main()
