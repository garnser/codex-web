from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.entitlements import build_entitlements_router
from codex_web.entitlements import (
    CapabilityEntitlementUpdate,
    EntitlementMode,
    QuotaBehavior,
    QuotaPolicyUpdate,
    QuotaWindow,
    UsageEventCreate,
    UsageReconciliationBatch,
)
from codex_web.services.entitlements import (
    EntitlementDeniedError,
    EntitlementService,
    QuotaExceededError,
    UsageIdempotencyConflictError,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.entitlements import EntitlementStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class EntitlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.service = EntitlementService(EntitlementStore(sqlite))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _enforce(self) -> None:
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=True, source="test-plan"),
            actor=self.actor,
        )

    def test_default_self_hosted_mode_is_explicitly_unlimited(self) -> None:
        decision = self.service.check(
            "external_actions",
            actor=self.actor,
            metric="external_action_attempts",
            projected_amount=1000,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mode, EntitlementMode.SELF_HOSTED_UNLIMITED)
        self.assertEqual(decision.reason, "self_hosted_unlimited")

    def test_enforced_mode_requires_capability_and_tracks_changes(self) -> None:
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        denied = self.service.check("external_actions", actor=self.actor)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "capability_not_entitled")

        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=True),
            actor=self.actor,
        )
        self.assertTrue(
            self.service.check("external_actions", actor=self.actor).allowed
        )

        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=False),
            actor=self.actor,
        )
        disabled = self.service.check("external_actions", actor=self.actor)
        self.assertFalse(disabled.allowed)
        self.assertEqual(disabled.reason, "capability_disabled")

    def test_atomic_hard_quota_consumption_is_idempotent(self) -> None:
        self._enforce()
        self.service.set_quota(
            "external_action_attempts",
            QuotaPolicyUpdate(
                limit=2,
                window=QuotaWindow.LIFETIME,
                behavior=QuotaBehavior.HARD_STOP,
            ),
            actor=self.actor,
        )
        first = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="attempt-1",
                metric="external_action_attempts",
                amount=1,
            ),
            actor=self.actor,
        )
        duplicate = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="attempt-1",
                metric="external_action_attempts",
                amount=1,
            ),
            actor=self.actor,
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            self.service.usage_total(
                self.actor,
                metric="external_action_attempts",
            ),
            1,
        )

        self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="attempt-2",
                metric="external_action_attempts",
                amount=1,
            ),
            actor=self.actor,
        )
        with self.assertRaises(QuotaExceededError):
            self.service.consume(
                "external_actions",
                UsageEventCreate(
                    idempotency_key="attempt-3",
                    metric="external_action_attempts",
                    amount=1,
                ),
                actor=self.actor,
            )

    def test_usage_replay_survives_plan_change_but_conflicting_key_is_rejected(self) -> None:
        self._enforce()
        first = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="stable-usage",
                metric="external_action_attempts",
                amount=1,
                source="action-intent",
                action_intent_id="action-intent-1",
            ),
            actor=self.actor,
        )
        self.assertFalse(first.duplicate)

        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=False),
            actor=self.actor,
        )
        replay = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="stable-usage",
                metric="external_action_attempts",
                amount=1,
                source="action-intent",
                action_intent_id="action-intent-1",
            ),
            actor=self.actor,
        )
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.event.id, first.event.id)

        with self.assertRaises(UsageIdempotencyConflictError):
            self.service.record_usage(
                UsageEventCreate(
                    idempotency_key="stable-usage",
                    metric="external_action_attempts",
                    amount=2,
                    source="action-intent",
                    action_intent_id="action-intent-1",
                ),
                actor=self.actor,
            )

    def test_late_usage_is_attributed_to_original_window(self) -> None:
        self._enforce()
        day = 86_400.0
        self.service.set_quota(
            "model_tokens",
            QuotaPolicyUpdate(
                limit=100,
                window=QuotaWindow.DAY,
                behavior=QuotaBehavior.HARD_STOP,
            ),
            actor=self.actor,
        )
        self.service.record_usage(
            UsageEventCreate(
                idempotency_key="late-day-one",
                metric="model_tokens",
                amount=90,
                occurred_at=day + 100,
                source="reconciliation",
            ),
            actor=self.actor,
        )
        decision = self.service.check(
            "external_actions",
            actor=self.actor,
            metric="model_tokens",
            projected_amount=20,
            at=(day * 2) + 100,
        )
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.quota)
        self.assertEqual(decision.quota.usage, 0)

    def test_non_hard_quota_reports_excess_without_denying(self) -> None:
        self._enforce()
        self.service.set_quota(
            "storage_gb",
            QuotaPolicyUpdate(
                limit=1,
                window=QuotaWindow.LIFETIME,
                behavior=QuotaBehavior.GRACE,
            ),
            actor=self.actor,
        )
        result = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="storage-1",
                metric="storage_gb",
                amount=2,
            ),
            actor=self.actor,
        )
        self.assertTrue(result.decision.allowed)
        self.assertTrue(result.decision.quota.exceeded)
        self.assertEqual(result.decision.reason, "quota_exceeded_grace")

    def test_reconciliation_batch_deduplicates_and_export_is_metadata_only(self) -> None:
        result = self.service.reconcile_usage(
            UsageReconciliationBatch(
                events=[
                    UsageEventCreate(
                        idempotency_key="late-1",
                        metric="model_tokens",
                        amount=50,
                        occurred_at=100.0,
                        source="provider-reconciliation",
                        project_id="home",
                        work_item_ref="group/app#1",
                    ),
                    UsageEventCreate(
                        idempotency_key="late-1",
                        metric="model_tokens",
                        amount=50,
                        occurred_at=100.0,
                        source="provider-reconciliation",
                        project_id="home",
                        work_item_ref="group/app#1",
                    ),
                    UsageEventCreate(
                        idempotency_key="late-2",
                        metric="model_tokens",
                        amount=25,
                        occurred_at=200.0,
                        source="provider-reconciliation",
                    ),
                ]
            ),
            actor=self.actor,
        )
        self.assertEqual(result.accepted, 2)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(
            self.service.usage_total(self.actor, metric="model_tokens"),
            75,
        )

        exported = self.service.export_usage(self.actor, limit=10)
        self.assertEqual(len(exported.records), 2)
        serialized = " ".join(item.model_dump_json() for item in exported.records)
        self.assertIn("model_tokens", serialized)
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("secret", serialized)
        self.assertIsNotNone(exported.next_received_after)

    def test_export_cursor_reads_only_newer_received_events(self) -> None:
        first = self.service.record_usage(
            UsageEventCreate(
                idempotency_key="cursor-1",
                metric="storage_gb",
                amount=1,
            ),
            actor=self.actor,
        )
        first_export = self.service.export_usage(self.actor)
        cursor = first_export.next_received_after
        self.assertIsNotNone(cursor)

        second = self.service.record_usage(
            UsageEventCreate(
                idempotency_key="cursor-2",
                metric="storage_gb",
                amount=1,
            ),
            actor=self.actor,
        )
        second_export = self.service.export_usage(
            self.actor,
            received_after=cursor,
        )
        self.assertEqual(
            [item.event_id for item in second_export.records],
            [second.event.id],
        )
        self.assertNotEqual(first.event.id, second.event.id)

    def test_entitlement_check_does_not_grant_administration(self) -> None:
        self._enforce()
        member = self.actor.model_copy(
            update={"roles": ()}
        )
        self.assertTrue(
            self.service.check("external_actions", actor=member).allowed
        )
        with self.assertRaises(AuthorizationError):
            self.service.set_capability(
                "other",
                CapabilityEntitlementUpdate(enabled=True),
                actor=member,
            )


class EntitlementApiAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = EntitlementService(EntitlementStore(sqlite))
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_entitlements_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_low_assurance_human_can_read_but_cannot_administer_or_meter(self) -> None:
        self.assertEqual(self.client.get("/api/entitlements/mode").status_code, 200)
        self.assertEqual(self.client.get("/api/entitlements/usage").status_code, 200)

        mode = self.client.put(
            "/api/entitlements/mode",
            json={"mode": "enforced"},
        )
        capability = self.client.put(
            "/api/entitlements/capabilities/external_actions",
            json={"enabled": True, "source": "test"},
        )
        quota = self.client.put(
            "/api/entitlements/quotas/external_action_attempts",
            json={
                "limit": 10,
                "window": "month",
                "behavior": "hard_stop",
                "warning_fraction": 0.8,
                "source": "test",
            },
        )
        usage = self.client.post(
            "/api/entitlements/usage",
            json={
                "idempotency_key": "manual-1",
                "metric": "external_action_attempts",
                "amount": 1,
            },
        )
        reconcile = self.client.post(
            "/api/entitlements/usage/reconcile",
            json={
                "events": [
                    {
                        "idempotency_key": "reconcile-1",
                        "metric": "external_action_attempts",
                        "amount": 1,
                    }
                ]
            },
        )

        for response in (mode, capability, quota, usage, reconcile):
            self.assertEqual(response.status_code, 403)
            self.assertIn("mfa", response.json()["detail"].lower())

    def test_mfa_human_can_administer_and_meter(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )

        mode = self.client.put(
            "/api/entitlements/mode",
            json={"mode": "enforced"},
        )
        capability = self.client.put(
            "/api/entitlements/capabilities/external_actions",
            json={"enabled": True, "source": "test"},
        )
        quota = self.client.put(
            "/api/entitlements/quotas/external_action_attempts",
            json={
                "limit": 10,
                "window": "lifetime",
                "behavior": "hard_stop",
                "warning_fraction": 0.8,
                "source": "test",
            },
        )
        usage = self.client.post(
            "/api/entitlements/usage",
            json={
                "idempotency_key": "manual-1",
                "metric": "external_action_attempts",
                "amount": 1,
            },
        )

        self.assertEqual(mode.status_code, 200)
        self.assertEqual(capability.status_code, 200)
        self.assertEqual(quota.status_code, 200)
        self.assertEqual(usage.status_code, 200)

    def test_service_admin_and_meter_scopes_remain_non_human_automation(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="entitlement-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("entitlements:admin",),
        )
        mode = self.client.put(
            "/api/entitlements/mode",
            json={"mode": "enforced"},
        )
        self.assertEqual(mode.status_code, 200)

        self.actor = self.actor.model_copy(
            update={
                "identity_id": "entitlement-meter-service",
                "service_scopes": ("entitlements:meter",),
            }
        )
        usage = self.client.post(
            "/api/entitlements/usage",
            json={
                "idempotency_key": "service-meter-1",
                "metric": "model_input_tokens",
                "amount": 5,
            },
        )
        self.assertEqual(usage.status_code, 200)


if __name__ == "__main__":
    unittest.main()
