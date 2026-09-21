from __future__ import annotations

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.runtime import build_runtime_router
from codex_web.api.system import build_system_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


class _RuntimeStub:
    async def status(self):
        return {"ok": True}

    async def livez(self):
        return {"ok": True, "status": "live"}

    async def readyz(self):
        return {"ok": True, "status": "ready"}

    async def healthz(self):
        return await self.readyz()

    def operations(self, *, window_seconds=900.0):
        return {"windowSeconds": window_seconds, "runtime": {"healthy": True}}

    async def recovery_resume(self):
        return {"ok": True, "resumingStaleThreads": []}

    async def rate_limits(self):
        return {"primary": {}}

    async def models(self, *, include_hidden=False):
        return {"data": [], "includeHidden": include_hidden}


class _StaticAssetsStub:
    @staticmethod
    def version():
        return "test"


class _RuntimeHealthStub:
    @staticmethod
    def health():
        return {"ok": True}


class _DiagnosticsStub:
    @staticmethod
    def snapshot(project_id):
        return {"projectId": project_id, "ok": True}


class _RoutingStub:
    @staticmethod
    def preview(payload):
        return {
            "provider": payload.provider,
            "externalConversationId": payload.external_conversation_id,
            "projectId": payload.project_id,
        }


class RuntimeOperatorBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
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

        app.include_router(build_runtime_router(_RuntimeStub()))
        app.include_router(
            build_system_router(
                _StaticAssetsStub(),
                _RuntimeHealthStub(),
                _DiagnosticsStub(),
                _RoutingStub(),
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def _route_test(self):
        return self.client.post(
            "/api/diagnostics/route-test",
            json={
                "provider": "slack",
                "external_conversation_id": "C1",
                "text": "preview only",
            },
        )

    def test_ordinary_member_cannot_read_process_wide_operator_state(self) -> None:
        for response in (
            self.client.get("/api/operations"),
            self.client.get("/api/diagnostics"),
            self._route_test(),
        ):
            self.assertEqual(response.status_code, 403)

        self.assertEqual(self.client.get("/api/status").status_code, 200)
        live = self.client.get("/api/livez")
        ready = self.client.get("/api/readyz")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(live.json()["status"], "live")
        self.assertEqual(ready.json()["status"], "ready")

    def test_human_admin_can_read_but_recovery_requires_mfa(self) -> None:
        self.actor = self.actor.model_copy(
            update={"roles": (MembershipRole.ADMIN,)}
        )

        self.assertEqual(self.client.get("/api/operations").status_code, 200)
        self.assertEqual(self.client.get("/api/diagnostics").status_code, 200)
        self.assertEqual(self._route_test().status_code, 200)

        denied = self.client.post("/api/recovery/resume")
        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        allowed = self.client.post("/api/recovery/resume")
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["ok"])

    def test_runtime_read_and_admin_service_scopes_are_distinct(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="runtime-reader",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("runtime:read",),
        )

        self.assertEqual(self.client.get("/api/operations").status_code, 200)
        self.assertEqual(self.client.get("/api/diagnostics").status_code, 200)
        self.assertEqual(self._route_test().status_code, 200)

        denied = self.client.post("/api/recovery/resume")
        self.assertEqual(denied.status_code, 403)
        self.assertIn("runtime:admin", denied.json()["detail"])

        self.actor = self.actor.model_copy(
            update={"service_scopes": ("runtime:admin",)}
        )
        self.assertEqual(self.client.get("/api/operations").status_code, 200)
        self.assertEqual(self.client.post("/api/recovery/resume").status_code, 200)

    def test_unscoped_service_cannot_read_operator_state(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="runtime-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=(),
        )

        response = self.client.get("/api/operations")

        self.assertEqual(response.status_code, 403)
        self.assertIn("runtime:read", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
