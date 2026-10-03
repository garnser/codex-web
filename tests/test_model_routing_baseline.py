from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.model_routing_baseline import install_model_routing_baseline
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ModelRoutingBaselineTests(unittest.TestCase):
    def test_dated_concrete_matrix_is_canonical_replaceable_definition_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = DefinitionRegistryService(
                DefinitionRegistryStore(SQLiteStateStore(Path(directory) / "state.db"))
            )
            service = install_model_routing_baseline(registry)

            baseline, reference = service.resolve(
                organization_id="org-a",
                workspace_id="workspace-a",
            )

        self.assertEqual(
            baseline.evaluation_revision,
            "initial-functional-matrix-2026-09-26",
        )
        self.assertEqual(baseline.evaluated_at, 1790380800)
        self.assertTrue(baseline.replaceable)
        self.assertGreaterEqual(len(baseline.entries), 39)
        functions = {item.function_id: item for item in baseline.entries}
        self.assertEqual(
            functions["architecture.adr"].primary_models,
            ("Claude Fable 5.1",),
        )
        self.assertEqual(
            functions["architecture.adr"].critic_models,
            ("GPT-6 Astra",),
        )
        self.assertTrue(functions["kpi.calculate"].deterministic)
        self.assertEqual(functions["kpi.calculate"].primary_models, ())
        self.assertEqual(reference.revision, 1)
