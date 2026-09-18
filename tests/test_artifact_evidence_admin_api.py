from __future__ import annotations

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.artifact_evidence import build_artifact_evidence_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


class _ArtifactEvidenceServiceStub:
    def __init__(self) -> None:
        self.requirement_actors = []
        self.governance_actors = []
        self.expire_calls = 0

    def list_artifacts(self, actor, *, work_item_ref=None, include_inactive=True):
        return []

    def set_work_item_requirements(self, ref, requirements, *, actor):
        self.requirement_actors.append(actor)
        return tuple(requirements)

    def sync_governance_records(self, *, actor):
        self.governance_actors.append(actor)
        return {"artifacts": 0, "evidence": 0}

    def expire_retention(self):
        self.expire_calls += 1
        return {"artifacts": [], "evidence": []}


class ArtifactEvidenceAdminAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _ArtifactEvidenceServiceStub()
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

        app.include_router(build_artifact_evidence_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_read_only_artifact_listing_remains_available_without_step_up(self) -> None:
        response = self.client.get("/api/artifacts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_low_assurance_human_admin_cannot_change_requirements_or_governance(self) -> None:
        self.actor = self.actor.model_copy(
            update={"roles": (MembershipRole.ADMIN,)}
        )

        requirements = self.client.put(
            "/api/evidence-requirements/work-items/WI-1",
            json={"requirements": []},
        )
        governance = self.client.post("/api/artifact-evidence/governance/sync")
        retention = self.client.post("/api/artifact-evidence/expire-retention")

        for response in (requirements, governance, retention):
            self.assertEqual(response.status_code, 403)
            self.assertIn("mfa", response.json()["detail"].lower())
        self.assertEqual(self.service.expire_calls, 0)

    def test_mfa_human_admin_can_manage_requirements_and_governance(self) -> None:
        self.actor = self.actor.model_copy(
            update={
                "roles": (MembershipRole.ADMIN,),
                "assurance": AuthenticationAssurance.MFA,
            }
        )

        requirements = self.client.put(
            "/api/evidence-requirements/work-items/WI-1",
            json={"requirements": []},
        )
        governance = self.client.post("/api/artifact-evidence/governance/sync")
        retention = self.client.post("/api/artifact-evidence/expire-retention")

        self.assertEqual(requirements.status_code, 200)
        self.assertEqual(governance.status_code, 200)
        self.assertEqual(retention.status_code, 200)
        self.assertEqual(len(self.service.requirement_actors), 1)
        self.assertEqual(len(self.service.governance_actors), 1)
        self.assertEqual(self.service.expire_calls, 1)

    def test_artifact_evidence_admin_service_scope_remains_supported(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="evidence-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("artifact-evidence:admin",),
        )

        response = self.client.post("/api/artifact-evidence/governance/sync")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.governance_actors[0].identity_id, "evidence-admin-service")

    def test_unscoped_service_cannot_use_admin_routes(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="evidence-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=(),
        )

        response = self.client.post("/api/artifact-evidence/expire-retention")

        self.assertEqual(response.status_code, 403)
        self.assertIn("artifact-evidence:admin", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
