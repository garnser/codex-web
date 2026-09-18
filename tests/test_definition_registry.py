from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.definitions import build_definitions_router
from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublishRequest,
    DefinitionRollbackRequest,
)
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_contracts import (
    execution_role,
    route_execution_role,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import WorkItemState
from codex_web.services.work_item_contracts import WorkItemContractService
from codex_web.execution_role_models import (
    EXECUTION_ROLE_CATALOG_ID,
    EXECUTION_ROLE_CATALOG_KIND,
    EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
    ExecutionRoleCatalogDefinition,
    validate_execution_role_catalog,
)
from codex_web.services.definitions import (
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionKindSchema,
    DefinitionNotFoundError,
    DefinitionRegistryService,
)
from codex_web.services.execution_role_definitions import ExecutionRoleDefinitionService
from codex_web.services.projects import ProjectNotFoundError
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class DefinitionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.tempdir.name) / "state.db")
        self.store = DefinitionRegistryStore(self.state)
        self.events: list[dict] = []
        self.service = DefinitionRegistryService(
            self.store,
            notifier=self.events.append,
        )
        self.service.register_schema(
            DefinitionKindSchema(
                kind=EXECUTION_ROLE_CATALOG_KIND,
                schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_execution_role_catalog,
            )
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _draft(self, payload: dict | None = None, *, actor: str = "admin"):
        return self.service.create_draft(
            DefinitionDraftCreate(
                definition_id=EXECUTION_ROLE_CATALOG_ID,
                kind=EXECUTION_ROLE_CATALOG_KIND,
                definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                payload=payload or execution_role_catalog_seed_payload(),
                actor=actor,
            )
        )

    def test_validate_publish_supersede_and_rollback_are_versioned(self) -> None:
        first = self._draft()
        validated = self.service.validate(first.record_id, actor="reviewer")
        self.assertEqual(validated.lifecycle, DefinitionLifecycle.VALIDATED)
        published = self.service.publish(
            first.record_id,
            DefinitionPublishRequest(actor="publisher"),
        )
        self.assertEqual(published.lifecycle, DefinitionLifecycle.PUBLISHED)

        changed = execution_role_catalog_seed_payload()
        changed["roles"][2]["description"] = "Database-published James description."
        second = self._draft(changed)
        second_published = self.service.publish(
            second.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=published.revision,
            ),
        )
        history = self.service.list_records(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
        )
        old = next(item for item in history if item.record_id == published.record_id)
        self.assertEqual(old.lifecycle, DefinitionLifecycle.SUPERSEDED)
        self.assertEqual(old.superseded_by_record_id, second_published.record_id)

        rollback = self.service.rollback(
            DefinitionRollbackRequest(
                definition_id=EXECUTION_ROLE_CATALOG_ID,
                kind=EXECUTION_ROLE_CATALOG_KIND,
                target_revision=published.revision,
                actor="publisher",
                expected_active_revision=second_published.revision,
            )
        )
        self.assertGreater(rollback.revision, second_published.revision)
        self.assertEqual(rollback.rollback_of_record_id, published.record_id)
        resolved = self.service.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
        )
        self.assertEqual(resolved.record_id, rollback.record_id)
        self.assertGreaterEqual(len(self.events), 6)

    def test_publish_uses_optimistic_active_revision(self) -> None:
        first = self._draft()
        active = self.service.publish(
            first.record_id,
            DefinitionPublishRequest(actor="publisher"),
        )
        second = self._draft()
        with self.assertRaises(DefinitionConflictError):
            self.service.publish(
                second.record_id,
                DefinitionPublishRequest(
                    actor="publisher",
                    expected_active_revision=active.revision + 50,
                ),
            )

    def test_scope_resolution_prefers_project_over_global(self) -> None:
        global_record = self.service.publish(
            self._draft().record_id,
            DefinitionPublishRequest(actor="publisher"),
        )
        project_payload = execution_role_catalog_seed_payload()
        project_payload["roles"][2]["description"] = "Project-specific James."
        project_draft = self.service.create_draft(
            DefinitionDraftCreate(
                definition_id=EXECUTION_ROLE_CATALOG_ID,
                kind=EXECUTION_ROLE_CATALOG_KIND,
                definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type="project",
                scope_id="project-a",
                payload=project_payload,
                actor="project-admin",
            )
        )
        project_record = self.service.publish(
            project_draft.record_id,
            DefinitionPublishRequest(actor="project-admin"),
        )
        selected = self.service.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
            context=DefinitionContext(project_id="project-a"),
        )
        other = self.service.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
            context=DefinitionContext(project_id="project-b"),
        )
        self.assertEqual(selected.record_id, project_record.record_id)
        self.assertEqual(other.record_id, global_record.record_id)

    def test_missing_quarantined_or_incompatible_definitions_fail_closed(self) -> None:
        with self.assertRaises(DefinitionNotFoundError):
            self.service.resolve(
                definition_id=EXECUTION_ROLE_CATALOG_ID,
                kind=EXECUTION_ROLE_CATALOG_KIND,
            )

        active = self.service.publish(
            self._draft().record_id,
            DefinitionPublishRequest(actor="publisher"),
        )
        self.service.quarantine(
            active.record_id,
            actor="security",
            reason="checksum investigation",
        )
        with self.assertRaises(DefinitionNotFoundError):
            self.service.resolve(
                definition_id=EXECUTION_ROLE_CATALOG_ID,
                kind=EXECUTION_ROLE_CATALOG_KIND,
            )

        incompatible = DefinitionRegistryService(
            self.store,
            engine_version="0.5",
        )
        incompatible.register_schema(
            DefinitionKindSchema(
                kind=EXECUTION_ROLE_CATALOG_KIND,
                schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_execution_role_catalog,
            )
        )
        draft = incompatible.create_draft(
            DefinitionDraftCreate(
                definition_id="execution-roles.future",
                kind=EXECUTION_ROLE_CATALOG_KIND,
                definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                payload=execution_role_catalog_seed_payload(),
                actor="admin",
                min_engine_version="1.0",
            )
        )
        with self.assertRaises(DefinitionCompatibilityError):
            incompatible.publish(
                draft.record_id,
                DefinitionPublishRequest(actor="admin"),
            )

    def test_store_detects_payload_checksum_tampering(self) -> None:
        active = self.service.publish(
            self._draft().record_id,
            DefinitionPublishRequest(actor="publisher"),
        )
        raw = self.state.get(self.store.namespace)
        raw["records"][0]["payload"]["roles"][0]["description"] = "tampered"
        self.state.put(self.store.namespace, raw)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.store.load()
        self.assertTrue(active.checksum)

    def test_published_database_revision_drives_work_item_contract_and_is_pinned(self) -> None:
        execution = ExecutionRoleDefinitionService(self.service)
        execution.bootstrap()

        initial = self.service.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
        )
        changed = execution_role_catalog_seed_payload()
        james = next(role for role in changed["roles"] if role["id"] == "james")
        james["required_artifacts"] = [
            *james["required_artifacts"],
            "database-defined proof artifact",
        ]
        draft = self._draft(changed)
        published = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=initial.revision,
            ),
        )

        saved: list[WorkItemState] = []
        events: list = []

        class Host:
            @staticmethod
            def _work_item_split_brain_findings(state):
                return []

            @staticmethod
            def _save_work_item_state(state):
                saved.append(state.model_copy(deep=True))
                return state

            @staticmethod
            def _append_work_item_event(event):
                events.append(event)

        state = WorkItemState(
            ref="group/app#42",
            project_id="app",
            project_path="group/app",
            current_owner="james",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        contract_service = WorkItemContractService(
            Host(),
            lambda item: f"WORK {item.ref}",
            execution,
        )
        text = contract_service.dispatch_text(state)
        contract = contract_service.contract_for_state(state)

        self.assertIn("database-defined proof artifact", contract.expected_outputs)
        self.assertEqual(contract.definition_refs[0].record_id, published.record_id)
        self.assertEqual(contract.definition_refs[0].revision, published.revision)
        self.assertEqual(state.execution.definition_refs[0].record_id, published.record_id)
        self.assertEqual(saved[-1].execution.definition_refs[0].record_id, published.record_id)
        self.assertEqual(events[-1].event_type, "execution_definition_pinned")
        self.assertIn(
            f"{EXECUTION_ROLE_CATALOG_ID}@{published.revision}",
            text,
        )

    def test_export_import_and_bootstrap_are_idempotent(self) -> None:
        execution = ExecutionRoleDefinitionService(self.service)
        execution.bootstrap()
        first_count = len(self.store.load())
        execution.bootstrap()
        self.assertEqual(len(self.store.load()), first_count)

        exported = self.service.export(kind=EXECUTION_ROLE_CATALOG_KIND)

        with tempfile.TemporaryDirectory() as second_dir:
            state = SQLiteStateStore(Path(second_dir) / "state.db")
            second = DefinitionRegistryService(DefinitionRegistryStore(state))
            second.register_schema(
                DefinitionKindSchema(
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    validate=validate_execution_role_catalog,
                )
            )
            imported = second.import_records(exported, actor="importer")
            self.assertEqual(len(imported), first_count)
            self.assertTrue(all(item.lifecycle == DefinitionLifecycle.DRAFT for item in imported))


class ExecutionRoleDefinitionTests(unittest.TestCase):
    def test_seed_preserves_routing_behavior_without_remaining_runtime_authority(self) -> None:
        catalog = ExecutionRoleCatalogDefinition.model_validate(
            execution_role_catalog_seed_payload()
        )
        self.assertEqual(
            route_execution_role("Implement the backend API", catalog=catalog).id,
            "james",
        )
        self.assertEqual(
            route_execution_role("Review and merge the MR", catalog=catalog).id,
            "quinn",
        )
        self.assertEqual(
            route_execution_role("Deploy the production release", catalog=catalog).id,
            "release-manager",
        )
        self.assertEqual(execution_role("james", catalog=catalog).lane, "implementation")


class DefinitionRegistryApiTests(unittest.TestCase):
    class _Projects:
        @staticmethod
        def get(project_id, scope):
            if (
                project_id == "project-a"
                and scope.organization_id == "org-a"
                and scope.workspace_id == "ws-a"
            ):
                return object()
            raise ProjectNotFoundError("Project not found")

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.tempdir.name) / "state.db")
        self.service = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        self.service.register_schema(
            DefinitionKindSchema(
                kind=EXECUTION_ROLE_CATALOG_KIND,
                schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_execution_role_catalog,
            )
        )
        self.actor = AuthenticationActor(
            identity_id="authenticated-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_definitions_router(self.service, self._Projects()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _draft_payload(self, **overrides):
        payload = {
            "definition_id": EXECUTION_ROLE_CATALOG_ID,
            "kind": EXECUTION_ROLE_CATALOG_KIND,
            "definition_schema_version": EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
            "payload": execution_role_catalog_seed_payload(),
            "actor": "spoofed-client-actor",
        }
        payload.update(overrides)
        return payload

    def test_authenticated_actor_replaces_payload_actor_and_runtime_resolves_same_record(self) -> None:
        draft = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(),
        )
        self.assertEqual(draft.status_code, 200)
        self.assertEqual(draft.json()["record"]["created_by"], self.actor.identity_id)
        record_id = draft.json()["record"]["record_id"]

        validated = self.client.post(
            f"/api/definitions/{record_id}/validate",
            json={"actor": "spoofed-reviewer"},
        )
        self.assertEqual(validated.status_code, 200)
        self.assertEqual(
            validated.json()["record"]["validated_by"],
            self.actor.identity_id,
        )

        published = self.client.post(
            f"/api/definitions/{record_id}/publish",
            json={
                "actor": "spoofed-publisher",
                "approval_metadata": {"ticket": "A-1"},
            },
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(
            published.json()["record"]["published_by"],
            self.actor.identity_id,
        )

        resolved_api = self.client.post(
            "/api/definitions/resolve",
            json={
                "definition_id": EXECUTION_ROLE_CATALOG_ID,
                "kind": EXECUTION_ROLE_CATALOG_KIND,
            },
        )
        self.assertEqual(resolved_api.status_code, 200)

        resolved_runtime = self.service.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
        )
        self.assertEqual(
            resolved_api.json()["record"]["record_id"],
            resolved_runtime.record_id,
        )
        self.assertEqual(resolved_runtime.approval_metadata["ticket"], "A-1")

    def test_low_assurance_human_cannot_mutate_workspace_definition(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.PRIMARY}
        )

        response = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(
                scope_type="workspace",
                scope_id="ws-a",
            ),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())

    def test_cross_tenant_scopes_are_denied_or_filtered(self) -> None:
        denied = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(
                scope_type="organization",
                scope_id="org-b",
            ),
        )
        self.assertEqual(denied.status_code, 403)

        own = self.service.create_draft(
            DefinitionDraftCreate(
                definition_id="execution-roles.own",
                kind=EXECUTION_ROLE_CATALOG_KIND,
                definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type="organization",
                scope_id="org-a",
                payload=execution_role_catalog_seed_payload(),
                actor="internal-bootstrap",
            )
        )
        other = self.service.create_draft(
            DefinitionDraftCreate(
                definition_id="execution-roles.other",
                kind=EXECUTION_ROLE_CATALOG_KIND,
                definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type="organization",
                scope_id="org-b",
                payload=execution_role_catalog_seed_payload(),
                actor="internal-bootstrap",
            )
        )

        rows = self.client.get("/api/definitions/records")
        self.assertEqual(rows.status_code, 200)
        ids = {item["record_id"] for item in rows.json()["items"]}
        self.assertIn(own.record_id, ids)
        self.assertNotIn(other.record_id, ids)

        hidden = self.client.get(f"/api/definitions/{other.record_id}/usage")
        self.assertEqual(hidden.status_code, 404)

    def test_project_scope_is_fenced_by_canonical_project_service(self) -> None:
        allowed = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(
                definition_id="execution-roles.project-a",
                scope_type="project",
                scope_id="project-a",
            ),
        )
        self.assertEqual(allowed.status_code, 200)

        denied = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(
                definition_id="execution-roles.project-b",
                scope_type="project",
                scope_id="project-b",
            ),
        )
        self.assertEqual(denied.status_code, 403)

        resolved_denied = self.client.post(
            "/api/definitions/resolve",
            json={
                "definition_id": EXECUTION_ROLE_CATALOG_ID,
                "kind": EXECUTION_ROLE_CATALOG_KIND,
                "context": {"project_id": "project-b"},
            },
        )
        self.assertEqual(resolved_denied.status_code, 403)

    def test_service_definition_admin_scope_can_mutate_workspace_but_not_global(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="definition-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("definitions:admin",),
        )

        allowed = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(
                scope_type="workspace",
                scope_id="ws-a",
            ),
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.json()["record"]["created_by"],
            "definition-service",
        )

        global_denied = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(definition_id="execution-roles.global-service"),
        )
        self.assertEqual(global_denied.status_code, 403)
        self.assertIn("definitions:global-admin", global_denied.json()["detail"])

    def test_global_mutation_requires_local_trusted_human_context(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )

        response = self.client.post(
            "/api/definitions/drafts",
            json=self._draft_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("local-trusted", response.json()["detail"])

    def test_resolve_defaults_to_authenticated_tenant_context(self) -> None:
        global_record = self.service.publish(
            self.service.create_draft(
                DefinitionDraftCreate(
                    definition_id="execution-roles.scoped",
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=execution_role_catalog_seed_payload(),
                    actor="bootstrap",
                )
            ).record_id,
            DefinitionPublishRequest(actor="bootstrap"),
        )
        workspace_payload = execution_role_catalog_seed_payload()
        workspace_payload["roles"][0]["description"] = "Workspace-specific definition."
        workspace_record = self.service.publish(
            self.service.create_draft(
                DefinitionDraftCreate(
                    definition_id="execution-roles.scoped",
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    scope_type="workspace",
                    scope_id="ws-a",
                    payload=workspace_payload,
                    actor="bootstrap",
                )
            ).record_id,
            DefinitionPublishRequest(actor="bootstrap"),
        )

        response = self.client.post(
            "/api/definitions/resolve",
            json={
                "definition_id": "execution-roles.scoped",
                "kind": EXECUTION_ROLE_CATALOG_KIND,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(global_record.record_id, workspace_record.record_id)
        self.assertEqual(
            response.json()["record"]["record_id"],
            workspace_record.record_id,
        )

    def test_usage_filters_cross_tenant_and_unscoped_references(self) -> None:
        record = self.service.publish(
            self.service.create_draft(
                DefinitionDraftCreate(
                    definition_id="execution-roles.shared",
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=execution_role_catalog_seed_payload(),
                    actor="bootstrap",
                )
            ).record_id,
            DefinitionPublishRequest(actor="bootstrap"),
        )
        self.service.register_usage_provider(
            lambda reference: [
                {
                    "object_type": "work_item",
                    "object_id": "own",
                    "project_id": "project-a",
                },
                {
                    "object_type": "work_item",
                    "object_id": "other",
                    "project_id": "project-b",
                },
                {
                    "object_type": "unknown",
                    "object_id": "unscoped",
                },
            ]
        )

        response = self.client.get(
            f"/api/definitions/{record.record_id}/usage"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["items"][0]["object_id"], "own")


if __name__ == "__main__":
    unittest.main()
