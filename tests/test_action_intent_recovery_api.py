from __future__ import annotations

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.action_intents import build_action_intents_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


class _Dumpable:
    def __init__(self, **payload):
        self.payload = payload

    def model_dump(self, mode="json"):
        return self.payload


class _ActionIntentServiceStub:
    def __init__(self) -> None:
        self.recovery_calls = []
        self.claim_actors = []

    def list(self, actor, *, work_item_ref=None, status=None):
        return []

    def recover_stale_claims(self, *, organization_id=None, workspace_id=None):
        self.recovery_calls.append((organization_id, workspace_id))
        return ["intent-stale"]

    def claim(self, payload, *, actor, intent_id=None):
        self.claim_actors.append(actor)
        return _Dumpable(id="intent-1", status="claimed")


class ActionIntentRecoveryAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _ActionIntentServiceStub()
        self.actor = AuthenticationActor(
            identity_id="member",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_action_intents_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_read_only_listing_remains_available_without_step_up(self) -> None:
        response = self.client.get("/api/action-intents")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_low_assurance_human_admin_cannot_recover_stale_claims(self) -> None:
        self.actor = self.actor.model_copy(
            update={"roles": (MembershipRole.ADMIN,)}
        )

        response = self.client.post("/api/action-intents/recover-stale")

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())
        self.assertEqual(self.service.recovery_calls, [])

    def test_mfa_human_admin_can_recover_stale_claims(self) -> None:
        self.actor = self.actor.model_copy(
            update={
                "roles": (MembershipRole.ADMIN,),
                "assurance": AuthenticationAssurance.MFA,
            }
        )

        response = self.client.post("/api/action-intents/recover-stale")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent_ids"], ["intent-stale"])
        self.assertEqual(self.service.recovery_calls, [("org-a", "ws-a")])

    def test_action_intent_admin_service_scope_can_recover(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="intent-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:admin",),
        )

        response = self.client.post("/api/action-intents/recover-stale")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.recovery_calls, [("org-a", "ws-a")])

    def test_worker_claim_route_is_not_mfa_gated_by_recovery_boundary(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="intent-worker",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker",),
        )

        response = self.client.post(
            "/api/action-intents/claim",
            json={"worker_id": "worker-1", "lease_seconds": 120},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["status"], "claimed")
        self.assertEqual(self.service.claim_actors[0].identity_id, "intent-worker")


if __name__ == "__main__":
    unittest.main()
