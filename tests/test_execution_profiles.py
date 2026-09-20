from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_profiles import (
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
)
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import (
    install_execution_profile_definitions,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutionProfileTests(unittest.TestCase):
    def test_seed_contains_repository_and_orchestration_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = SQLiteStateStore(Path(raw) / "state.sqlite3")
            service = install_execution_profile_definitions(
                DefinitionRegistryService(DefinitionRegistryStore(store))
            )

            catalog = service.catalog(project_id="home")
            profile, reference = service.resolve(
                "orchestration-only",
                project_id="home",
            )

        self.assertIsInstance(catalog, ExecutionProfileCatalogDefinition)
        self.assertEqual(catalog.default_profile_id, "repository-write")
        self.assertEqual(profile.workspace_mode, "scratch")
        self.assertEqual(profile.repository_access, "none")
        self.assertEqual(profile.required_worker_capabilities, ("command_execution",))
        self.assertIn("work_item.handoff", profile.control_plane_operations)
        self.assertEqual(reference.kind, "execution-profile-catalog")

    def test_structural_coordination_role_uses_orchestration_profile(self) -> None:
        catalog = ExecutionRoleCatalogDefinition.model_validate(
            execution_role_catalog_seed_payload()
        )
        orchestrator = catalog.role_map["orchestrator"]
        implementation = catalog.role_map["james"]

        self.assertEqual(orchestrator.execution_profile_id, "orchestration-only")
        self.assertEqual(implementation.execution_profile_id, "repository-write")

    def test_scratch_profile_cannot_smuggle_git_or_network_authority(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionProfileContract(
                id="bad-git",
                name="Bad git",
                description="invalid",
                workspace_mode="scratch",
                repository_access="none",
                required_worker_capabilities=("git", "command_execution"),
            )
        with self.assertRaises(ValueError):
            ExecutionProfileContract(
                id="bad-network",
                name="Bad network",
                description="invalid",
                workspace_mode="scratch",
                repository_access="none",
                required_worker_capabilities=("command_execution", "network"),
            )


if __name__ == "__main__":
    unittest.main()
