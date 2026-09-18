from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.configuration import build_configuration_router
from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationRollbackRequest,
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
    FeatureTargeting,
)
from codex_web.services.configuration import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationService,
)
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
    def test_api_uses_same_canonical_service_for_publish_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state = SQLiteStateStore(Path(tempdir) / "state.db")
            service = ConfigurationService(ConfigurationRegistryStore(state))
            service.register_spec(
                ConfigurationSpec(
                    key="feature.api_fixture",
                    value_kind=ConfigurationValueKind.BOOLEAN,
                    default=False,
                    feature_flag=True,
                )
            )
            app = FastAPI()
            app.include_router(build_configuration_router(service))

            with TestClient(app) as client:
                draft_response = client.post(
                    "/api/configuration/drafts",
                    json={
                        "key": "feature.api_fixture",
                        "scope_type": "workspace",
                        "scope_id": "workspace-a",
                        "value": True,
                        "actor": "admin",
                    },
                )
                self.assertEqual(draft_response.status_code, 200)
                record_id = draft_response.json()["record"]["id"]

                publish = client.post(
                    f"/api/configuration/{record_id}/publish",
                    json={"actor": "admin"},
                )
                self.assertEqual(publish.status_code, 200)

                resolved = client.post(
                    "/api/configuration/resolve",
                    json={
                        "key": "feature.api_fixture",
                        "context": {"workspace_id": "workspace-a"},
                    },
                )
                self.assertEqual(resolved.status_code, 200)
                self.assertTrue(resolved.json()["effective"]["value"])
                self.assertEqual(
                    resolved.json()["effective"]["record_id"],
                    record_id,
                )


if __name__ == "__main__":
    unittest.main()
