from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.ux_telemetry import build_ux_telemetry_router
from codex_web.configuration import (
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationScope,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.services.configuration import ConfigurationService
from codex_web.services.ux_telemetry import UxTelemetryService
from codex_web.services.ux_telemetry_configuration import (
    UX_TELEMETRY_ENABLED_CONFIG,
    install_ux_telemetry_configuration,
)
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.ux_telemetry import UxTelemetryStore
from codex_web.ux_telemetry import UxTelemetryEventCreate


class UxTelemetryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.configuration = ConfigurationService(ConfigurationRegistryStore(self.state))
        install_ux_telemetry_configuration(self.configuration)
        self.store = UxTelemetryStore(self.state)
        self.service = UxTelemetryService(self.store, self.configuration)
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def enable(self) -> None:
        draft = self.configuration.create_draft(
            ConfigurationDraftCreate(
                key=UX_TELEMETRY_ENABLED_CONFIG,
                scope_type=ConfigurationScope.WORKSPACE,
                scope_id=self.scope.workspace_id,
                value=True,
                actor="admin",
                reason="test enable",
            )
        )
        self.configuration.publish(
            draft.id,
            ConfigurationPublishRequest(actor="admin", reason="test enable"),
        )


class UxTelemetryServiceTests(UxTelemetryFixture):
    def test_default_policy_is_disabled_and_collects_nothing(self) -> None:
        result = self.service.ingest(
            UxTelemetryEventCreate(
                event_name="workflow_started",
                workflow="automation",
                step="run_now",
                journey_id="journey_1234",
            ),
            scope=self.scope,
        )
        self.assertIsNone(result)
        self.assertEqual(self.store.load().events, [])
        self.assertFalse(self.service.policy(self.scope)["enabled"])

    def test_ingestion_is_tenant_scoped_content_free_and_retention_bounded(self) -> None:
        self.enable()
        old = self.service.ingest(
            UxTelemetryEventCreate(
                event_name="workflow_started",
                workflow="project_setup",
                step="apply_plan",
                journey_id="journey_old1",
            ),
            scope=self.scope,
            now=100.0,
        )
        self.assertIsNotNone(old)
        current = self.service.ingest(
            UxTelemetryEventCreate(
                event_name="workflow_completed",
                workflow="project_setup",
                step="apply_plan",
                duration_ms=1234,
                journey_id="journey_new1",
            ),
            scope=self.scope,
            now=4_000_000.0,
        )
        self.assertIsNotNone(current)

        rows = self.service.events(scope=self.scope, now=4_000_000.0)
        self.assertEqual([item.id for item in rows], [current.id])
        self.assertNotIn(
            "TOP SECRET",
            self.store.load().model_dump_json(),
        )
        other = self.service.events(
            scope=TenantScope(organization_id="org-b", workspace_id="ws-b"),
            now=4_000_000.0,
        )
        self.assertEqual(other, [])

    def test_summary_exposes_activation_completion_and_highest_friction(self) -> None:
        self.enable()
        events = [
            ("onboarding_started", "onboarding", "execution_ready", None),
            ("onboarding_step_completed", "onboarding", "first_workflow_started", None),
            ("onboarding_completed", "onboarding", "first_useful_outcome", 5000),
            ("workflow_started", "agent_management", "edit_profile", None),
            ("validation_error_shown", "agent_management", "edit_profile", 400),
            ("action_failed", "agent_management", "edit_profile", 600),
            ("recovery_action_used", "project_setup", "retry_setup", None),
        ]
        for index, (name, workflow, step, duration) in enumerate(events):
            self.service.ingest(
                UxTelemetryEventCreate(
                    event_name=name,
                    workflow=workflow,
                    step=step,
                    duration_ms=duration,
                    journey_id=f"journey_{index:04d}",
                ),
                scope=self.scope,
                now=1000 + index,
            )

        summary = self.service.summary(scope=self.scope, now=1007)
        self.assertEqual(summary["activation"]["completion_rate"], 1.0)
        self.assertEqual(
            summary["activation"]["median_time_to_first_meaningful_outcome_ms"],
            5000,
        )
        self.assertEqual(
            summary["highest_friction_step"],
            {
                "workflow": "agent_management",
                "step": "edit_profile",
                "friction_events": 2,
            },
        )
        self.assertEqual(
            summary["workflows"]["agent_management"]["validation_errors"],
            1,
        )

        for index, route in enumerate(
            ("project_overview", "project_agents", "project_overview")
        ):
            self.service.ingest(
                UxTelemetryEventCreate(
                    event_name="route_transition",
                    workflow="navigation",
                    step="route",
                    route_group=route,
                    journey_id="journey_nav1",
                ),
                scope=self.scope,
                now=1100 + index,
            )
        navigation = self.service.summary(scope=self.scope, now=1103)["navigation"]
        self.assertEqual(navigation["transitions"], 3)
        self.assertEqual(navigation["backtracks"], 1)

    def test_unknown_steps_and_sensitive_extra_fields_are_rejected_by_schema(self) -> None:
        with self.assertRaises(ValueError):
            UxTelemetryEventCreate(
                event_name="workflow_started",
                workflow="automation",
                step="user_entered_name",
            )
        with self.assertRaises(ValueError):
            UxTelemetryEventCreate.model_validate(
                {
                    "event_name": "workflow_started",
                    "workflow": "automation",
                    "step": "run_now",
                    "prompt": "TOP SECRET prompt",
                    "repository_content": "private source code",
                    "secret": "credential-value",
                    "form_value": "user input",
                    "metadata": {"anything": "not allowed"},
                }
            )


class UxTelemetryApiTests(UxTelemetryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.enable()
        self.actor = AuthenticationActor(
            identity_id="member-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id=self.scope.organization_id,
            workspace_id=self.scope.workspace_id,
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_ux_telemetry_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        super().tearDown()

    def test_member_can_emit_but_cannot_export(self) -> None:
        response = self.client.post(
            "/api/ux-telemetry/events",
            json={
                "event_name": "workflow_started",
                "workflow": "automation",
                "step": "run_now",
                "journey_id": "journey_member1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(self.client.get("/api/ux-telemetry/events").status_code, 403)
        self.assertEqual(self.client.get("/api/ux-telemetry/summary").status_code, 403)

        self.actor = self.actor.model_copy(
            update={"roles": (MembershipRole.ADMIN,)}
        )
        exported = self.client.get("/api/ux-telemetry/events")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json()["count"], 1)

    def test_sensitive_payload_fields_fail_closed_before_storage(self) -> None:
        response = self.client.post(
            "/api/ux-telemetry/events",
            json={
                "event_name": "action_failed",
                "workflow": "agent_management",
                "step": "edit_profile",
                "prompt": "TOP SECRET prompt",
                "secret_reference": {"kind": "secret", "secret_id": "secret-a"},
                "form_value": "private user value",
            },
        )
        self.assertEqual(response.status_code, 422)
        serialized = self.store.load().model_dump_json()
        self.assertNotIn("TOP SECRET", serialized)
        self.assertNotIn("secret-a", serialized)
        self.assertNotIn("private user value", serialized)


if __name__ == "__main__":
    unittest.main()
