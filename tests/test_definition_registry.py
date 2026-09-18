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


class _DefinitionProjectScope:
    _scopes = {
        "project-a": ("org-a", "ws-a"),
        "project-b": ("org-b", "ws-b"),
    }

    def get(self, project_id, scope):
        expected = self._scopes.get(project_id)
        if expected != (scope.organization_id, scope.workspace_id):
            raise ProjectNotFoundError("Project not found")
        return object()


class DefinitionRegistryApiTests(unittest.TestCase):
    def _service(self, root: Path) -> DefinitionRegistryService:
        service = DefinitionRegistryService(
            DefinitionRegistryStore(SQLiteStateStore(root / "state.db"))
        )
        service.register_schema(
            DefinitionKindSchema(
                kind=EXECUTION_ROLE_CATALOG_KIND,
                schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_execution_role_catalog,
            )
        )
        return service

    @staticmethod
    def _human(
        *,
        identity_id: str = "admin",
        organization_id: str = "org-a",
        workspace_id: str = "ws-a",
        assurance: AuthenticationAssurance = AuthenticationAssurance.MFA,
    ) -> AuthenticationActor:
        return AuthenticationActor(
            identity_id=identity_id,
            principal_kind=PrincipalKind.HUMAN,
            organization_id=organization_id,
            workspace_id=workspace_id,
            roles=(MembershipRole.ADMIN,),
            assurance=assurance,
        )

    def _client(self, service, actor_box, *, projects=None) -> TestClient:
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            actor = actor_box["actor"]
            request.state.identity_actor = actor
            request.state.tenant_scope = actor.tenant
            return await call_next(request)

        app.include_router(build_definitions_router(service, projects))
        return TestClient(app)

    def test_api_and_runtime_resolve_the_same_published_record(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            service = self._service(Path(tempdir))
            actor_box = {
                "actor": self._human(
                    assurance=AuthenticationAssurance.LOCAL_TRUSTED,
                )
            }
            payload = execution_role_catalog_seed_payload()

            with self._client(service, actor_box) as client:
                draft = client.post(
                    "/api/definitions/drafts",
                    json={
                        "definition_id": EXECUTION_ROLE_CATALOG_ID,
                        "kind": EXECUTION_ROLE_CATALOG_KIND,
                        "definition_schema_version": EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                        "payload": payload,
                        "actor": "forged-admin",
                    },
                )
                self.assertEqual(draft.status_code, 200)
                self.assertEqual(
                    draft.json()["record"]["created_by"],
                    actor_box["actor"].identity_id,
                )
                record_id = draft.json()["record"]["record_id"]

                validated = client.post(
                    f"/api/definitions/{record_id}/validate",
                    json={"actor": "forged-reviewer"},
                )
                self.assertEqual(validated.status_code, 200)
                self.assertEqual(
                    validated.json()["record"]["validated_by"],
                    actor_box["actor"].identity_id,
                )

                published = client.post(
                    f"/api/definitions/{record_id}/publish",
                    json={
                        "actor": "forged-publisher",
                        "approval_metadata": {"ticket": "A-1"},
                    },
                )
                self.assertEqual(published.status_code, 200)
                self.assertEqual(
                    published.json()["record"]["published_by"],
                    actor_box["actor"].identity_id,
                )

                resolved_api = client.post(
                    "/api/definitions/resolve",
                    json={
                        "definition_id": EXECUTION_ROLE_CATALOG_ID,
                        "kind": EXECUTION_ROLE_CATALOG_KIND,
                    },
                )
                self.assertEqual(resolved_api.status_code, 200)

            resolved_runtime = service.resolve(
                definition_id=EXECUTION_ROLE_CATALOG_ID,
                kind=EXECUTION_ROLE_CATALOG_KIND,
            )
            self.assertEqual(
                resolved_api.json()["record"]["record_id"],
                resolved_runtime.record_id,
            )
            self.assertEqual(resolved_runtime.approval_metadata["ticket"], "A-1")

    def test_low_assurance_and_cross_tenant_definition_mutations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            service = self._service(Path(tempdir))
            actor_box = {
                "actor": self._human(
                    assurance=AuthenticationAssurance.PRIMARY,
                )
            }
            payload = execution_role_catalog_seed_payload()

            with self._client(
                service,
                actor_box,
                projects=_DefinitionProjectScope(),
            ) as client:
                low_assurance = client.post(
                    "/api/definitions/drafts",
                    json={
                        "definition_id": "roles.org-a",
                        "kind": EXECUTION_ROLE_CATALOG_KIND,
                        "definition_schema_version": EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                        "scope_type": "organization",
                        "scope_id": "org-a",
                        "payload": payload,
                    },
                )
                self.assertEqual(low_assurance.status_code, 403)
                self.assertIn("mfa", low_assurance.json()["detail"].lower())

                actor_box["actor"] = self._human()
                for scope_type, scope_id in (
                    ("organization", "org-b"),
                    ("workspace", "ws-b"),
                    ("project", "project-b"),
                ):
                    denied = client.post(
                        "/api/definitions/drafts",
                        json={
                            "definition_id": f"roles.{scope_id}",
                            "kind": EXECUTION_ROLE_CATALOG_KIND,
                            "definition_schema_version": EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                            "scope_type": scope_type,
                            "scope_id": scope_id,
                            "payload": payload,
                        },
                    )
                    self.assertEqual(denied.status_code, 403)

                global_denied = client.post(
                    "/api/definitions/drafts",
                    json={
                        "definition_id": "roles.global",
                        "kind": EXECUTION_ROLE_CATALOG_KIND,
                        "definition_schema_version": EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                        "payload": payload,
                    },
                )
                self.assertEqual(global_denied.status_code, 403)
                self.assertIn("platform-global", global_denied.json()["detail"])

    def test_visible_history_and_resolve_context_are_tenant_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            service = self._service(Path(tempdir))
            payload = execution_role_catalog_seed_payload()
            service.create_draft(
                DefinitionDraftCreate(
                    definition_id="roles.global",
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=payload,
                    actor="bootstrap",
                )
            )
            service.create_draft(
                DefinitionDraftCreate(
                    definition_id="roles.org-b",
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    scope_type="organization",
                    scope_id="org-b",
                    payload=payload,
                    actor="other",
                )
            )
            service.create_draft(
                DefinitionDraftCreate(
                    definition_id="roles.project-a",
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    scope_type="project",
                    scope_id="project-a",
                    payload=payload,
                    actor="admin",
                )
            )
            actor_box = {"actor": self._human()}

            with self._client(
                service,
                actor_box,
                projects=_DefinitionProjectScope(),
            ) as client:
                listed = client.get("/api/definitions/records")
                self.assertEqual(listed.status_code, 200)
                ids = {item["definition_id"] for item in listed.json()["items"]}
                self.assertIn("roles.global", ids)
                self.assertIn("roles.project-a", ids)
                self.assertNotIn("roles.org-b", ids)

                denied_filter = client.get(
                    "/api/definitions/records",
                    params={"scope_type": "organization", "scope_id": "org-b"},
                )
                self.assertEqual(denied_filter.status_code, 403)

                denied_context = client.post(
                    "/api/definitions/resolve",
                    json={
                        "definition_id": "roles.global",
                        "kind": EXECUTION_ROLE_CATALOG_KIND,
                        "context": {"organization_id": "org-b"},
                    },
                )
                self.assertEqual(denied_context.status_code, 403)

    def test_definitions_admin_service_scope_can_manage_global_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            service = self._service(Path(tempdir))
            actor_box = {
                "actor": AuthenticationActor(
                    identity_id="definition-admin-service",
                    principal_kind=PrincipalKind.SERVICE,
                    organization_id="org-a",
                    workspace_id="ws-a",
                    assurance=AuthenticationAssurance.SERVICE_TOKEN,
                    service_scopes=("definitions:admin",),
                )
            }

            with self._client(service, actor_box) as client:
                response = client.post(
                    "/api/definitions/drafts",
                    json={
                        "definition_id": "roles.global",
                        "kind": EXECUTION_ROLE_CATALOG_KIND,
                        "definition_schema_version": EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                        "payload": execution_role_catalog_seed_payload(),
                        "actor": "forged-human",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["record"]["created_by"],
                    "definition-admin-service",
                )


if __name__ == "__main__":
    unittest.main()
