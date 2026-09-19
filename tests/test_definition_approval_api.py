from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.definitions import build_definitions_router
from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
    validate_authority_role_catalog,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.authority_publication import (
    assess_authority_catalog_publication,
)
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class DefinitionPublicationApprovalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.service.register_schema(
            DefinitionKindSchema(
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_authority_role_catalog,
                assess_publish=assess_authority_catalog_publication,
            )
        )
        self.publisher = AuthenticationActor(
            identity_id="publisher",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.approver = AuthenticationActor(
            identity_id="approver",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.APPROVER,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.actor = self.publisher

        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_definitions_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _payload():
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="developer",
                    name="Developer",
                    description="Workspace developer authority",
                    grants=(
                        AuthorityGrant(
                            id="developer.execute",
                            capability="deploy.release",
                            level=AuthorityLevel.EXECUTE,
                        ),
                    ),
                ),
            ),
            bindings=(
                AuthorityRoleBinding(
                    id="developer-binding",
                    role_id="developer",
                    subject_kind="identity",
                    subject_id="developer-a",
                    organization_id="local",
                    workspace_id="default",
                ),
            ),
        ).model_dump(mode="json")

    def _draft(self):
        response = self.client.post(
            "/api/definitions/drafts",
            json={
                "definition_id": AUTHORITY_ROLE_CATALOG_ID,
                "kind": AUTHORITY_ROLE_CATALOG_KIND,
                "definition_schema_version": AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                "scope_type": "workspace",
                "scope_id": "default",
                "payload": self._payload(),
                "reason": "workspace authority",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["record"]

    def test_sensitive_publish_requires_different_approved_identity(self):
        record = self._draft()
        record_id = record["record_id"]

        assessment = self.client.get(
            f"/api/definitions/{record_id}/publication-assessment"
        )
        self.assertEqual(assessment.status_code, 200)
        self.assertTrue(
            assessment.json()["requires_independent_approval"]
        )
        self.assertTrue(assessment.json()["reasons"])

        # Publisher can attest, but their own attestation cannot satisfy the
        # independent-approval gate for their publication.
        self_approval = self.client.post(
            f"/api/definitions/{record_id}/publication-approvals",
            json={"reference": "SELF-1", "reason": "self review"},
        )
        self.assertEqual(self_approval.status_code, 200)
        denied = self.client.post(
            f"/api/definitions/{record_id}/publish",
            json={"reason": "attempt self-approved expansion"},
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(
            denied.json()["detail"]["code"],
            "definition_approval_required",
        )

        self.actor = self.approver
        approved = self.client.post(
            f"/api/definitions/{record_id}/publication-approvals",
            json={"reference": "CAB-123", "reason": "independent review"},
        )
        self.assertEqual(approved.status_code, 200)
        approvals = approved.json()["record"]["publication_approvals"]
        self.assertEqual(approvals[-1]["approved_by"], "approver")
        self.assertEqual(approvals[-1]["reference"], "CAB-123")

        self.actor = self.publisher
        published = self.client.post(
            f"/api/definitions/{record_id}/publish",
            json={"reason": "independently approved expansion"},
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(
            published.json()["record"]["lifecycle"],
            "published",
        )

    def test_low_assurance_or_unprivileged_actor_cannot_attest(self):
        record = self._draft()
        record_id = record["record_id"]

        self.actor = self.approver.model_copy(
            update={"assurance": AuthenticationAssurance.PRIMARY}
        )
        low_assurance = self.client.post(
            f"/api/definitions/{record_id}/publication-approvals",
            json={"reference": "CAB-low", "reason": "not enough assurance"},
        )
        self.assertEqual(low_assurance.status_code, 403)

        self.actor = self.approver.model_copy(
            update={
                "roles": (MembershipRole.MEMBER,),
                "assurance": AuthenticationAssurance.MFA,
            }
        )
        unprivileged = self.client.post(
            f"/api/definitions/{record_id}/publication-approvals",
            json={"reference": "CAB-member", "reason": "not an approver"},
        )
        self.assertEqual(unprivileged.status_code, 403)


if __name__ == "__main__":
    unittest.main()
