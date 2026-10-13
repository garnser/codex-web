from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.models import WorkItemState
from codex_web.services.work_item_contracts import WorkItemContractService
from codex_web.services.execution_role_definitions import ExecutionRoleDefinitionService
from codex_web.execution_role_models import validate_execution_role_catalog
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_profiles import (
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
)
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService
from codex_web.services.execution_profile_definitions import (
    install_execution_profile_definitions,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutionProfileTests(unittest.TestCase):
    @staticmethod
    def administrative_payload():
        payload = execution_profile_catalog_seed_payload()
        payload["profiles"].append({
            "id": "orchestration-admin", "name": "Orchestrator administrative escalation",
            "description": "Project-scoped governed provider administration",
            "workspace_mode": "scratch", "repository_access": "none",
            "required_worker_capabilities": ["command_execution"],
            "control_plane_operations": ["repository.issue.create"],
            "administrative_repository_ids": ["resource-connector"],
        })
        return payload

    def test_legacy_profile_snapshots_do_not_gain_administrative_fields(self):
        payload = execution_profile_catalog_seed_payload()
        self.assertEqual(ExecutionProfileCatalogDefinition.model_validate(payload)
                         .model_dump(mode="json"), payload)
        self.assertFalse(any("administrative_repository_ids" in row
                             for row in payload["profiles"]))

    def test_administrative_targets_cannot_be_repository_execution_or_implicit_operations(self):
        original = self.administrative_payload()["profiles"][-1]
        for changes in (
            {"workspace_mode": "repository", "repository_access": "mutable",
             "required_worker_capabilities": ["git", "command_execution"]},
            {"control_plane_operations": []},
            {"administrative_repository_ids": ["resource-connector", "resource-connector"]},
            {"administrative_repository_ids": ["/home/private"]},
            {"administrative_repository_ids": [f"resource-{n}" for n in range(33)]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ExecutionProfileContract.model_validate(original | changes)

    def test_administrative_definition_requires_new_schema_and_exact_project(self):
        with tempfile.TemporaryDirectory() as raw:
            registry = DefinitionRegistryService(DefinitionRegistryStore(
                SQLiteStateStore(Path(raw) / "state.sqlite3")))
            profiles = install_execution_profile_definitions(registry)
            payload = self.administrative_payload()
            with self.assertRaises(ValueError):
                registry.create_draft(DefinitionDraftCreate(
                    kind="execution-profile-catalog", definition_id="execution.profiles.default",
                    definition_schema_version="1.0", payload=payload, actor="fixture"))
            global_record = registry.create_draft(DefinitionDraftCreate(
                kind="execution-profile-catalog", definition_id="execution.profiles.default",
                definition_schema_version="1.1", payload=payload, actor="fixture"))
            registry.publish(global_record.record_id, DefinitionPublishRequest(actor="fixture"))
            with self.assertRaisesRegex(ValueError, "exact Project-scoped"):
                profiles.resolve("orchestration-admin", project_id="project-a")
            project_record = registry.create_draft(DefinitionDraftCreate(
                kind="execution-profile-catalog", definition_id="execution.profiles.default",
                definition_schema_version="1.1", payload=payload, actor="fixture",
                scope_type="project", scope_id="project-a"))
            registry.publish(project_record.record_id, DefinitionPublishRequest(actor="fixture"))
            profile, reference = profiles.resolve("orchestration-admin", project_id="project-a")
            self.assertEqual(profile.administrative_repository_ids, ("resource-connector",))
            self.assertEqual(reference.record_id, project_record.record_id)
            self.assertEqual(profile.public()["administrativeRepositoryIds"], ["resource-connector"])
            with self.assertRaisesRegex(ValueError, "exact Project-scoped"):
                profiles.resolve("orchestration-admin", project_id="project-b")

    def test_administrative_envelope_rechecks_lease_roles_target_and_definition(self):
        with tempfile.TemporaryDirectory() as raw:
            registry = DefinitionRegistryService(DefinitionRegistryStore(
                SQLiteStateStore(Path(raw) / "state.sqlite3")))
            service = install_execution_profile_definitions(registry)
            payload = self.administrative_payload()
            record = registry.create_draft(DefinitionDraftCreate(
                kind="execution-profile-catalog", definition_id="execution.profiles.default",
                definition_schema_version="1.1", payload=payload, actor="fixture",
                scope_type="project", scope_id="project-a"))
            registry.publish(record.record_id, DefinitionPublishRequest(actor="fixture"))
            profile, reference = service.resolve("orchestration-admin", project_id="project-a")
            assignment = SimpleNamespace(
                organization_id="org", workspace_id="workspace", project_id="project-a",
                status="running", lease=SimpleNamespace(expires_at=200, fence=3, worker_id="worker"),
                fence=3, assigned_worker_id="worker", deadline_at=200,
                execution_profile_id="orchestration-admin", execution_profile_definition=reference,
                agent_profile=SimpleNamespace(authority_role_id="escalation", execution_profile_id="orchestration-admin",
                    profile_id="orchestrator", profile_revision=8, profile_record_id="profile-r8", role_id="orchestrator"),
            )
            current_profile = SimpleNamespace(lifecycle="active", revision=8, record_id="profile-r8",
                authority_role_id="escalation", execution_profile_id="orchestration-admin", role_id="orchestrator")
            service.agent_profiles = SimpleNamespace(get=Mock(return_value=current_profile))
            actor = SimpleNamespace(organization_id="org", workspace_id="workspace")
            authority = SimpleNamespace(role_ids_for_actor=Mock(return_value=("escalation",)))
            def authorize(**changes):
                return service.authorize_administrative_target(
                    assignment, operation_id=changes.get("operation", "repository.issue.create"),
                    repository_id=changes.get("target", "resource-connector"),
                    actor=actor, authority=authority, now=changes.get("now", 100),
                )
            self.assertEqual(authorize(), profile)
            for changes in ({"target": "resource-foreign"}, {"operation": "repository.branch.publish"}, {"now": 201}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    authorize(**changes)
            current_profile.lifecycle = "disabled"
            with self.assertRaisesRegex(ValueError, "disabled or its binding changed"):
                authorize()
            current_profile.lifecycle = "active"
            current_profile.revision = 9
            with self.assertRaisesRegex(ValueError, "disabled or its binding changed"):
                authorize()
            current_profile.revision = 8
            assignment.lease.fence = 2
            with self.assertRaisesRegex(ValueError, "live fenced"):
                authorize()
            assignment.lease.fence = 3
            authority.role_ids_for_actor.return_value = ()
            with self.assertRaisesRegex(ValueError, "live authority"):
                authorize()
            authority.role_ids_for_actor.return_value = ("escalation",)
            actor.workspace_id = "foreign"
            with self.assertRaisesRegex(ValueError, "live fenced"):
                authorize()
            actor.workspace_id = "workspace"
            replacement = registry.create_draft(DefinitionDraftCreate(
                kind="execution-profile-catalog", definition_id="execution.profiles.default",
                definition_schema_version="1.1", payload=payload, actor="fixture",
                scope_type="project", scope_id="project-a"))
            registry.publish(replacement.record_id, DefinitionPublishRequest(actor="fixture"))
            with self.assertRaisesRegex(ValueError, "no longer effective"):
                authorize()

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
            repository_profile, _ = service.resolve(
                "repository-write",
                project_id="home",
            )

        self.assertIsInstance(catalog, ExecutionProfileCatalogDefinition)
        self.assertEqual(catalog.default_profile_id, "repository-write")
        self.assertEqual(profile.workspace_mode, "scratch")
        self.assertEqual(profile.repository_access, "none")
        self.assertEqual(profile.required_worker_capabilities, ("command_execution",))
        self.assertIn("work_item.handoff", profile.control_plane_operations)
        self.assertIn(
            "work_item.progress",
            repository_profile.control_plane_operations,
        )
        self.assertEqual(reference.kind, "execution-profile-catalog")

    def test_work_item_pins_its_workspace_role_and_profile_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            registry = DefinitionRegistryService(DefinitionRegistryStore(
                SQLiteStateStore(Path(raw) / "state.sqlite3")))
            profiles = install_execution_profile_definitions(registry)
            registry.register_schema(DefinitionKindSchema(
                kind="execution-role-catalog", schema_version="1.0",
                validate=validate_execution_role_catalog,
            ))
            roles = ExecutionRoleDefinitionService(registry)
            roles.bootstrap()
            expected = {}
            for tenant in ("a", "b"):
                records = []
                for kind, definition_id, payload in [
                    ("execution-role-catalog", "execution-roles.default", execution_role_catalog_seed_payload()),
                    ("execution-profile-catalog", "execution.profiles.default", execution_profile_catalog_seed_payload()),
                ]:
                    record = registry.create_draft(DefinitionDraftCreate(
                        kind=kind, definition_id=definition_id, definition_schema_version="1.0",
                        scope_type="workspace", scope_id=f"workspace-{tenant}",
                        payload=payload, actor="fixture",
                    ))
                    registry.publish(record.record_id, DefinitionPublishRequest(actor="fixture"))
                    records.append(record.record_id)
                expected[tenant] = records
            service = WorkItemContractService(None, lambda state: state.ref, roles, profiles)
            for tenant in ("a", "b"):
                state = WorkItemState(
                    ref=f"group/app#{tenant}", organization_id=f"org-{tenant}",
                    workspace_id=f"workspace-{tenant}", project_id=f"project-{tenant}",
                    current_owner="james", current_stage="implementation_active",
                    last_meaningful_update_at=1, updated_at=1, created_at=1,
                )
                contract = service.contract_for_state(state)
                self.assertEqual([ref.record_id for ref in contract.definition_refs], expected[tenant])

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
