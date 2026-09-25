from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.configuration import build_configuration_router
from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationResetRequest,
    ConfigurationRollbackRequest,
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
    FeatureTargeting,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.configuration import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationService,
)
from codex_web.services.projects import ProjectNotFoundError
from codex_web.services.resources import ResourceNotFoundError
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ConfigurationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.tempdir.name) / "state.db")
        self.registry_store = ConfigurationRegistryStore(self.state)
        self.service = ConfigurationService(self.registry_store)
        self.service.register_spec(
            ConfigurationSpec(
                key="runtime.retry_limit",
                value_kind=ConfigurationValueKind.INTEGER,
                default=1,
            )
        )
        self.service.register_spec(
            ConfigurationSpec(
                key="feature.operator_v2",
                value_kind=ConfigurationValueKind.BOOLEAN,
                default=False,
                feature_flag=True,
                kill_switch_capable=True,
            )
        )
        self.service.register_spec(
            ConfigurationSpec(
                key="feature.percent_fixture",
                value_kind=ConfigurationValueKind.BOOLEAN,
                default=False,
                feature_flag=True,
            )
        )
        self.service.register_spec(
            ConfigurationSpec(
                key="integration.credential",
                value_kind=ConfigurationValueKind.SECRET_REF,
                required=True,
                default=None,
            )
        )
        self.service.register_spec(
            ConfigurationSpec(
                key="runtime.contract",
                value_kind=ConfigurationValueKind.DEFINITION_REF,
                required=True,
                default=None,
            )
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _publish(
        self,
        key: str,
        value,
        *,
        scope_type: ConfigurationScope = ConfigurationScope.GLOBAL,
        scope_id: str | None = None,
        targeting: FeatureTargeting | None = None,
        force_disabled: bool = False,
        expected_active_revision: int | None = None,
    ):
        draft = self.service.create_draft(
            ConfigurationDraftCreate(
                key=key,
                scope_type=scope_type,
                scope_id=scope_id,
                value=value,
                actor="operator",
                feature_targeting=targeting,
                force_disabled=force_disabled,
            )
        )
        return self.service.publish(
            draft.id,
            ConfigurationPublishRequest(
                actor="publisher",
                expected_active_revision=expected_active_revision,
            ),
        )

    def test_schema_metadata_drives_constraints_sensitivity_and_editability(self) -> None:
        secret = self.service.specs.get("integration.credential")
        self.assertTrue(secret.sensitive)
        self.assertEqual(secret.category, "Advanced")

        self.service.register_spec(
            ConfigurationSpec(
                key="runtime.mode",
                value_kind=ConfigurationValueKind.STRING,
                category="Execution",
                allowed_values=("safe", "fast"),
                default="safe",
            )
        )
        self.service.register_spec(
            ConfigurationSpec(
                key="runtime.cost_limit",
                value_kind=ConfigurationValueKind.NUMBER,
                category="Execution",
                minimum=0,
                maximum=10,
                default=1,
            )
        )
        self.service.register_spec(
            ConfigurationSpec(
                key="runtime.external",
                value_kind=ConfigurationValueKind.STRING,
                category="Advanced",
                editable=False,
                default="provider-owned",
            )
        )

        with self.assertRaisesRegex(ValueError, "must be one of"):
            self.service.specs.get("runtime.mode").validate_value("invalid")
        with self.assertRaisesRegex(ValueError, "at least 0"):
            self.service.specs.get("runtime.cost_limit").validate_value(-1)
        with self.assertRaisesRegex(ConfigurationError, "read-only"):
            self.service.create_draft(
                ConfigurationDraftCreate(
                    key="runtime.external",
                    scope_type=ConfigurationScope.GLOBAL,
                    value="local-change",
                    actor="operator",
                )
            )

    def test_scope_precedence_is_deterministic(self) -> None:
        global_record = self._publish("runtime.retry_limit", 2)
        project_record = self._publish(
            "runtime.retry_limit",
            5,
            scope_type=ConfigurationScope.PROJECT,
            scope_id="project-a",
        )

        project = self.service.resolve(
            "runtime.retry_limit",
            ConfigurationContext(project_id="project-a"),
        )
        other = self.service.resolve(
            "runtime.retry_limit",
            ConfigurationContext(project_id="project-b"),
        )

        self.assertEqual(project.value, 5)
        self.assertEqual(project.record_id, project_record.id)
        self.assertEqual(project.published_by, "publisher")
        self.assertIsNotNone(project.published_at)
        self.assertEqual(other.value, 2)
        self.assertEqual(other.record_id, global_record.id)

    def test_publish_uses_optimistic_active_revision_check(self) -> None:
        first = self._publish("runtime.retry_limit", 2)
        draft = self.service.create_draft(
            ConfigurationDraftCreate(
                key="runtime.retry_limit",
                scope_type=ConfigurationScope.GLOBAL,
                value=3,
                actor="operator",
            )
        )
        with self.assertRaises(ConfigurationConflictError):
            self.service.publish(
                draft.id,
                ConfigurationPublishRequest(
                    actor="operator",
                    expected_active_revision=first.revision + 10,
                ),
            )

    def test_rollback_creates_new_revision_and_preserves_history(self) -> None:
        first = self._publish("runtime.retry_limit", 2)
        second = self._publish(
            "runtime.retry_limit",
            3,
            expected_active_revision=first.revision,
        )

        rolled_back = self.service.rollback(
            ConfigurationRollbackRequest(
                key="runtime.retry_limit",
                scope_type=ConfigurationScope.GLOBAL,
                target_revision=first.revision,
                actor="operator",
                expected_active_revision=second.revision,
            )
        )

        self.assertGreater(rolled_back.revision, second.revision)
        self.assertEqual(rolled_back.value, 2)
        self.assertEqual(rolled_back.rollback_of_id, first.id)
        effective = self.service.resolve("runtime.retry_limit")
        self.assertEqual(effective.record_id, rolled_back.id)
        history = self.service.list_records(key="runtime.retry_limit")
        self.assertEqual(len(history), 3)

    def test_resolution_chain_explains_selected_override_and_fallbacks(self) -> None:
        global_record = self._publish("runtime.retry_limit", 2)
        project_record = self._publish(
            "runtime.retry_limit",
            5,
            scope_type=ConfigurationScope.PROJECT,
            scope_id="project-a",
        )

        effective = self.service.resolve(
            "runtime.retry_limit",
            ConfigurationContext(project_id="project-a"),
        )

        self.assertEqual(effective.record_id, project_record.id)
        self.assertEqual(
            [step.source for step in effective.resolution_chain],
            ["published", "published", "default"],
        )
        self.assertEqual(
            [step.record_id for step in effective.resolution_chain],
            [project_record.id, global_record.id, None],
        )
        self.assertEqual(
            [step.selected for step in effective.resolution_chain],
            [True, False, False],
        )
        self.assertEqual(
            [step.value for step in effective.resolution_chain],
            [5, 2, 3],
        )

    def test_reset_override_preserves_history_and_falls_back_to_inheritance(self) -> None:
        global_record = self._publish("runtime.retry_limit", 2)
        project_record = self._publish(
            "runtime.retry_limit",
            5,
            scope_type=ConfigurationScope.PROJECT,
            scope_id="project-a",
        )

        tombstone = self.service.reset_override(
            ConfigurationResetRequest(
                key="runtime.retry_limit",
                scope_type=ConfigurationScope.PROJECT,
                scope_id="project-a",
                actor="operator",
                reason="return to inherited value",
                expected_active_revision=project_record.revision,
            )
        )

        self.assertEqual(tombstone.state.value, "disabled")
        self.assertEqual(tombstone.supersedes_id, project_record.id)
        self.assertGreater(tombstone.revision, project_record.revision)
        effective = self.service.resolve(
            "runtime.retry_limit",
            ConfigurationContext(project_id="project-a"),
        )
        self.assertEqual(effective.value, 2)
        self.assertEqual(effective.record_id, global_record.id)
        history = self.service.list_records(key="runtime.retry_limit")
        prior = next(item for item in history if item.id == project_record.id)
        self.assertEqual(prior.state.value, "superseded")
        self.assertEqual(prior.superseded_by_id, tombstone.id)

        with self.assertRaises(ConfigurationConflictError):
            self.service.reset_override(
                ConfigurationResetRequest(
                    key="runtime.retry_limit",
                    scope_type=ConfigurationScope.PROJECT,
                    scope_id="project-a",
                    actor="operator",
                    expected_active_revision=project_record.revision,
                )
            )

    def test_feature_targeting_expiry_and_kill_switch_are_deterministic(self) -> None:
        beta = self._publish(
            "feature.operator_v2",
            True,
            targeting=FeatureTargeting(cohorts=["beta"], percentage=100),
        )
        self.assertEqual(
            self.service.resolve(
                "feature.operator_v2",
                ConfigurationContext(subject_id="u1", cohort="beta"),
            ).record_id,
            beta.id,
        )
        self.assertFalse(
            self.service.resolve(
                "feature.operator_v2",
                ConfigurationContext(subject_id="u2", cohort="stable"),
            ).value
        )

        project = self._publish(
            "feature.operator_v2",
            True,
            scope_type=ConfigurationScope.PROJECT,
            scope_id="project-a",
        )
        self.assertEqual(
            self.service.resolve(
                "feature.operator_v2",
                ConfigurationContext(project_id="project-a", cohort="stable"),
            ).record_id,
            project.id,
        )

        expired_draft = self.service.create_draft(
            ConfigurationDraftCreate(
                key="feature.operator_v2",
                scope_type=ConfigurationScope.WORKSPACE,
                scope_id="workspace-expired",
                value=True,
                actor="operator",
                feature_targeting=FeatureTargeting(
                    percentage=100,
                    expires_at=100.0,
                ),
            )
        )
        self.service.publish(
            expired_draft.id,
            ConfigurationPublishRequest(actor="operator"),
        )
        expired = self.service.resolve(
            "feature.operator_v2",
            ConfigurationContext(workspace_id="workspace-expired"),
            now=101.0,
        )
        self.assertFalse(expired.value)
        self.assertEqual(expired.source, "default")

        kill = self._publish(
            "feature.operator_v2",
            False,
            force_disabled=True,
            expected_active_revision=beta.revision,
        )
        effective = self.service.resolve(
            "feature.operator_v2",
            ConfigurationContext(project_id="project-a", cohort="stable"),
        )
        self.assertFalse(effective.value)
        self.assertEqual(effective.record_id, kill.id)
        self.assertEqual(effective.reason, "kill_switch")

    def test_secret_and_definition_values_are_reference_only(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_draft(
                ConfigurationDraftCreate(
                    key="integration.credential",
                    scope_type=ConfigurationScope.PROJECT,
                    scope_id="project-a",
                    value="raw-secret",
                    actor="operator",
                )
            )

        secret = self._publish(
            "integration.credential",
            {"kind": "secret", "secret_id": "credential-1"},
            scope_type=ConfigurationScope.PROJECT,
            scope_id="project-a",
        )
        self.assertEqual(secret.value["secret_id"], "credential-1")
        self.assertNotIn("value", secret.value)

        definition = self._publish(
            "runtime.contract",
            {
                "kind": "definition",
                "definition_id": "execution-role.james",
                "revision": "4",
            },
            scope_type=ConfigurationScope.PROJECT,
            scope_id="project-a",
        )
        self.assertEqual(definition.value["revision"], "4")

    def test_percentage_targeting_is_stable_for_same_subject(self) -> None:
        record = self._publish(
            "feature.percent_fixture",
            True,
            targeting=FeatureTargeting(percentage=50),
        )
        first = self.service.resolve(
            "feature.percent_fixture",
            ConfigurationContext(subject_id="stable-subject"),
        )
        second = self.service.resolve(
            "feature.percent_fixture",
            ConfigurationContext(subject_id="stable-subject"),
        )
        self.assertEqual(first.value, second.value)
        self.assertEqual(first.record_id, second.record_id)
        self.assertIn(first.record_id, {None, record.id})

    def test_unversioned_store_payload_migrates_deterministically(self) -> None:
        record = self.service.create_draft(
            ConfigurationDraftCreate(
                key="runtime.retry_limit",
                scope_type=ConfigurationScope.GLOBAL,
                value=7,
                actor="migration-test",
            )
        )
        self.state.put(
            self.registry_store.namespace,
            {"records": [record.model_dump(mode="json")]},
        )
        loaded = self.registry_store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].value, 7)
        self.assertEqual(loaded[0].schema_version, "1.0")

    def test_required_unset_and_permission_escalation_fail_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.service.resolve("integration.credential")

        with self.assertRaisesRegex(ValueError, "cannot grant authority"):
            ConfigurationSpec(
                key="unsafe.permission",
                value_kind=ConfigurationValueKind.BOOLEAN,
                default=False,
                grants_authority=True,
            )

    def test_startup_only_setting_cannot_claim_hot_reload(self) -> None:
        with self.assertRaisesRegex(ValueError, "startup-only"):
            ConfigurationSpec(
                key="runtime.startup_only",
                value_kind=ConfigurationValueKind.STRING,
                default="value",
                startup_only=True,
                hot_reloadable=True,
            )


class ConfigurationApiTests(unittest.TestCase):
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

    class _Resources:
        @staticmethod
        def get(resource_id, actor):
            if (
                resource_id == "resource-a"
                and actor.organization_id == "org-a"
                and actor.workspace_id == "ws-a"
            ):
                return object()
            raise ResourceNotFoundError("resource not found")

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.tempdir.name) / "state.db")
        self.service = ConfigurationService(ConfigurationRegistryStore(self.state))
        self.service.register_spec(
            ConfigurationSpec(
                key="feature.api_fixture",
                value_kind=ConfigurationValueKind.BOOLEAN,
                default=False,
                feature_flag=True,
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

        app.include_router(
            build_configuration_router(
                self.service,
                self._Projects(),
                self._Resources(),
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _draft_payload(self, **overrides):
        payload = {
            "key": "feature.api_fixture",
            "scope_type": "workspace",
            "scope_id": "ws-a",
            "value": True,
            "actor": "forged-client-actor",
        }
        payload.update(overrides)
        return payload

    def test_api_uses_authenticated_actor_and_tenant_default_resolution(self) -> None:
        global_record = self.service.create_draft(
            ConfigurationDraftCreate(
                key="feature.api_fixture",
                scope_type=ConfigurationScope.GLOBAL,
                value=False,
                actor="bootstrap",
            )
        )
        self.service.publish(
            global_record.id,
            ConfigurationPublishRequest(actor="bootstrap"),
        )

        draft_response = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(),
        )
        self.assertEqual(draft_response.status_code, 200)
        self.assertEqual(
            draft_response.json()["record"]["created_by"],
            self.actor.identity_id,
        )
        record_id = draft_response.json()["record"]["id"]

        publish = self.client.post(
            f"/api/configuration/{record_id}/publish",
            json={"actor": "forged-publisher"},
        )
        self.assertEqual(publish.status_code, 200)
        self.assertEqual(
            publish.json()["record"]["published_by"],
            self.actor.identity_id,
        )

        resolved = self.client.post(
            "/api/configuration/resolve",
            json={"key": "feature.api_fixture", "context": {}},
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertTrue(resolved.json()["effective"]["value"])
        self.assertEqual(
            resolved.json()["effective"]["record_id"],
            record_id,
        )
        chain = resolved.json()["effective"]["resolution_chain"]
        self.assertEqual([step["source"] for step in chain], ["published", "published", "default"])
        self.assertEqual(chain[0]["record_id"], record_id)
        self.assertTrue(chain[0]["selected"])
        self.assertFalse(chain[1]["selected"])

    def test_reset_endpoint_reverts_explicit_override_to_default(self) -> None:
        draft = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(),
        ).json()["record"]
        publish = self.client.post(
            f"/api/configuration/{draft['id']}/publish",
            json={},
        )
        self.assertEqual(publish.status_code, 200)
        active = publish.json()["record"]

        reset = self.client.post(
            "/api/configuration/reset",
            json={
                "key": "feature.api_fixture",
                "scope_type": "workspace",
                "scope_id": "ws-a",
                "reason": "use inherited default",
                "expected_active_revision": active["revision"],
            },
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["record"]["state"], "disabled")
        self.assertEqual(reset.json()["record"]["supersedes_id"], active["id"])

        resolved = self.client.post(
            "/api/configuration/resolve",
            json={"key": "feature.api_fixture", "context": {}},
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertFalse(resolved.json()["effective"]["value"])
        self.assertEqual(resolved.json()["effective"]["source"], "default")

    def test_low_assurance_human_cannot_mutate_but_can_read(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.PRIMARY}
        )

        self.assertEqual(self.client.get("/api/configuration/specs").status_code, 200)
        response = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())

    def test_cross_tenant_records_filters_and_context_fail_closed(self) -> None:
        own = self.service.create_draft(
            ConfigurationDraftCreate(
                key="feature.api_fixture",
                scope_type=ConfigurationScope.ORGANIZATION,
                scope_id="org-a",
                value=True,
                actor="bootstrap",
            )
        )
        other = self.service.create_draft(
            ConfigurationDraftCreate(
                key="feature.api_fixture",
                scope_type=ConfigurationScope.ORGANIZATION,
                scope_id="org-b",
                value=True,
                actor="bootstrap",
            )
        )

        rows = self.client.get("/api/configuration/records")
        self.assertEqual(rows.status_code, 200)
        ids = {item["id"] for item in rows.json()["items"]}
        self.assertIn(own.id, ids)
        self.assertNotIn(other.id, ids)

        denied_filter = self.client.get(
            "/api/configuration/records",
            params={"scope_type": "organization", "scope_id": "org-b"},
        )
        self.assertEqual(denied_filter.status_code, 403)

        hidden = self.client.get(f"/api/configuration/{other.id}/impact")
        self.assertEqual(hidden.status_code, 404)

        denied_context = self.client.post(
            "/api/configuration/resolve",
            json={
                "key": "feature.api_fixture",
                "context": {"workspace_id": "ws-b"},
            },
        )
        self.assertEqual(denied_context.status_code, 403)

    def test_project_and_resource_scopes_use_canonical_tenant_lookups(self) -> None:
        project = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="project",
                scope_id="project-a",
            ),
        )
        self.assertEqual(project.status_code, 200)

        denied_project = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="project",
                scope_id="project-b",
            ),
        )
        self.assertEqual(denied_project.status_code, 403)

        resource = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="resource",
                scope_id="resource-a",
            ),
        )
        self.assertEqual(resource.status_code, 200)

        denied_resource = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="resource",
                scope_id="resource-b",
            ),
        )
        self.assertEqual(denied_resource.status_code, 403)

    def test_service_scopes_separate_tenant_and_platform_configuration(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="configuration-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("configuration:admin",),
        )
        workspace = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(),
        )
        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(
            workspace.json()["record"]["created_by"],
            "configuration-service",
        )

        global_denied = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="global",
                scope_id=None,
            ),
        )
        self.assertEqual(global_denied.status_code, 403)
        self.assertIn("configuration:global-admin", global_denied.json()["detail"])

        self.actor = self.actor.model_copy(
            update={"service_scopes": ("configuration:global-admin",)}
        )
        global_allowed = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="global",
                scope_id=None,
            ),
        )
        self.assertEqual(global_allowed.status_code, 200)

    def test_global_human_mutation_requires_local_trusted_platform_context(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )

        response = self.client.post(
            "/api/configuration/drafts",
            json=self._draft_payload(
                scope_type="global",
                scope_id=None,
            ),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("local-trusted", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
