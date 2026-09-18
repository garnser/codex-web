from __future__ import annotations

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.execution_workspaces import build_execution_workspaces_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


class _WorkspaceServiceStub:
    def __init__(self) -> None:
        self.recovery_scopes = []

    def list(self, actor):
        return []

    def recover_expired(self, *, scope):
        self.recovery_scopes.append(scope)
        return []


class ExecutionWorkspaceRecoveryAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _WorkspaceServiceStub()
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

        app.include_router(build_execution_workspaces_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_read_only_listing_remains_available_without_step_up(self) -> None:
        response = self.client.get("/api/execution-workspaces")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_low_assurance_human_admin_cannot_trigger_recovery(self) -> None:
        self.actor = self.actor.model_copy(
            update={"roles": (MembershipRole.ADMIN,)}
        )

        response = self.client.post("/api/execution-workspaces/recover")

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())
        self.assertEqual(self.service.recovery_scopes, [])

    def test_mfa_human_admin_can_trigger_recovery(self) -> None:
        self.actor = self.actor.model_copy(
            update={
                "roles": (MembershipRole.ADMIN,),
                "assurance": AuthenticationAssurance.MFA,
            }
        )

        response = self.client.post("/api/execution-workspaces/recover")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})
        self.assertEqual(len(self.service.recovery_scopes), 1)

    def test_workspace_admin_service_scope_can_trigger_recovery(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="workspace-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("execution-workspace:admin",),
        )

        response = self.client.post("/api/execution-workspaces/recover")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.service.recovery_scopes), 1)

    def test_unscoped_service_cannot_trigger_recovery(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="workspace-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=(),
        )

        response = self.client.post("/api/execution-workspaces/recover")

        self.assertEqual(response.status_code, 403)
        self.assertIn("execution-workspace:admin", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
