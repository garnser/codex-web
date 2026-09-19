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
    AuthorityApprovalRequirement,
    AuthorityAutonomyRisk,
    AuthorityDelegation,
    AuthorityEnvironment,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    DefinitionRollbackRequest,
    DefinitionScope,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.resources import ResourceType
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import (
    DefinitionConflictError,
    DefinitionRegistryService,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor(
    identity_id: str,
    *,
    roles: tuple[MembershipRole, ...],
    assurance: AuthenticationAssurance = AuthenticationAssurance.MFA,
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id=identity_id,
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="ws-a",
        roles=roles,
        assurance=assurance,
    )


class AuthorityPublicationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        install_authority_roles(self.service)
        self.approver = _actor(
            "approver-a",
            roles=(MembershipRole.APPROVER,),
        )
        self.publisher = "publisher-a"
        self.baseline = self._catalog()
        first = self._draft(self.baseline)
        first_preflight = self.service.publication_preflight(first.record_id)
        self.assertTrue(first_preflight.requires_approval)
        approval = self.service.record_publication_approval(
            first.record_id,
            actor=self.approver,
            reason="approve initial workspace authority",
        )
        self.active = self.service.publish(
            first.record_id,
            DefinitionPublishRequest(
                actor=self.publisher,
                publication_approval_id=approval.id,
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _catalog() -> AuthorityRoleCatalogDefinition:
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="base-reader",
                    name="Base reader",
                    description="Read-only base.",
                    grants=(
                        AuthorityGrant(
                            id="base.read",
                            capability="repository.read",
                            level=AuthorityLevel.READ,
                            resource_types=(ResourceType.REPOSITORY,),
                        ),
                    ),
                ),
                AuthorityRoleDefinition(
                    id="developer",
                    name="Developer",
                    description="Bounded delivery authority.",
                    grants=(
                        AuthorityGrant(
                            id="developer.deploy",
                            capability="deploy.release",
                            level=AuthorityLevel.EXECUTE,
                            project_ids=("project-a",),
                            resource_types=(ResourceType.REPOSITORY,),
                            environments=(AuthorityEnvironment.STAGING,),
                            max_amount_usd=100,
                            max_input_tokens=1000,
                            max_output_tokens=500,
                            max_model_calls=2,
                            max_autonomous_risk=AuthorityAutonomyRisk.MEDIUM,
                            approvals=AuthorityApprovalRequirement(
                                count=1,
                                role_ids=("release-approver",),
                            ),
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
                    organization_id="org-a",
                    workspace_id="ws-a",
                    project_ids=("project-a",),
                ),
            ),
        )

    def _draft(
        self,
        catalog: AuthorityRoleCatalogDefinition,
        *,
        actor: str = "creator-a",
    ):
        return self.service.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type=DefinitionScope.WORKSPACE,
                scope_id="ws-a",
                payload=catalog.model_dump(mode="json"),
                actor=actor,
                reason="authority publication test",
            )
        )

    def _changed(self, mutate) -> AuthorityRoleCatalogDefinition:
        payload = self.baseline.model_dump(mode="json")
        mutate(payload)
        return AuthorityRoleCatalogDefinition.model_validate(payload)

    def test_sensitive_expansion_classes_require_approval(self) -> None:
        cases = (
            (
                "level",
                lambda p: p["roles"][1]["grants"][0].update(
                    {"level": AuthorityLevel.APPROVE}
                ),
                "authority.level_increased",
            ),
            (
                "project_scope",
                lambda p: p["roles"][1]["grants"][0].update(
                    {"project_ids": ()}
                ),
                "authority.project_ids_expanded",
            ),
            (
                "production",
                lambda p: p["roles"][1]["grants"][0].update(
                    {
                        "environments": (
                            AuthorityEnvironment.STAGING,
                            AuthorityEnvironment.PRODUCTION,
                        )
                    }
                ),
                "authority.production_scope_added",
            ),
            (
                "money",
                lambda p: p["roles"][1]["grants"][0].update(
                    {"max_amount_usd": 200}
                ),
                "authority.max_amount_usd_increased",
            ),
            (
                "tokens",
                lambda p: p["roles"][1]["grants"][0].update(
                    {"max_input_tokens": None}
                ),
                "authority.max_input_tokens_increased",
            ),
            (
                "risk",
                lambda p: p["roles"][1]["grants"][0].update(
                    {"max_autonomous_risk": AuthorityAutonomyRisk.HIGH}
                ),
                "authority.autonomy_risk_increased",
            ),
            (
                "approval",
                lambda p: p["roles"][1]["grants"][0].update(
                    {"approvals": {"count": 0, "role_ids": ()}}
                ),
                "authority.approval_requirement_reduced",
            ),
            (
                "inheritance",
                lambda p: p["roles"][1].update(
                    {"inherits": ("base-reader",)}
                ),
                "authority.inheritance_added",
            ),
            (
                "binding",
                lambda p: p["bindings"].append(
                    {
                        "id": "second-binding",
                        "role_id": "developer",
                        "subject_kind": "identity",
                        "subject_id": "developer-b",
                        "organization_id": "org-a",
                        "workspace_id": "ws-a",
                        "project_ids": ("project-a",),
                    }
                ),
                "authority.binding_added",
            ),
            (
                "delegation",
                lambda p: p["delegations"].append(
                    {
                        "id": "delegation-a",
                        "role_id": "developer",
                        "delegate_identity_id": "developer-b",
                        "delegated_by_identity_id": "approver-a",
                        "organization_id": "org-a",
                        "workspace_id": "ws-a",
                        "project_ids": ("project-a",),
                        "expires_at": 9_999_999_999.0,
                        "reason": "temporary duty",
                    }
                ),
                "authority.delegation_added",
            ),
        )
        for label, mutate, expected_class in cases:
            with self.subTest(label=label):
                draft = self._draft(self._changed(mutate))
                assessment = self.service.publication_preflight(draft.record_id)
                self.assertTrue(assessment.requires_approval)
                self.assertIn(expected_class, assessment.change_classes)
                with self.assertRaisesRegex(
                    DefinitionConflictError,
                    "requires approved preflight",
                ):
                    self.service.publish(
                        draft.record_id,
                        DefinitionPublishRequest(
                            actor=self.publisher,
                            expected_active_revision=self.active.revision,
                        ),
                    )

    def test_restrictive_revision_publishes_without_sensitive_approval(self) -> None:
        restricted = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {
                    "max_amount_usd": 50,
                    "max_input_tokens": 500,
                    "max_autonomous_risk": AuthorityAutonomyRisk.LOW,
                    "approvals": {
                        "count": 2,
                        "role_ids": ("release-approver",),
                    },
                }
            )
        )
        draft = self._draft(restricted)
        assessment = self.service.publication_preflight(draft.record_id)

        self.assertFalse(assessment.requires_approval)
        self.assertIn("authority.max_amount_usd_reduced", assessment.change_classes)
        published = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=self.publisher,
                expected_active_revision=self.active.revision,
            ),
        )
        self.assertEqual(
            published.approval_metadata["publication_requires_approval"],
            "false",
        )

    def test_distinct_current_approver_and_fingerprint_are_required(self) -> None:
        expanded = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {"max_amount_usd": 250}
            )
        )
        draft = self._draft(expanded)

        self_approver = _actor(
            "creator-a",
            roles=(MembershipRole.APPROVER,),
        )
        with self.assertRaisesRegex(DefinitionConflictError, "creator cannot approve"):
            self.service.record_publication_approval(
                draft.record_id,
                actor=self_approver,
                reason="self approval must fail",
            )

        weak_approver = _actor(
            "weak-approver",
            roles=(MembershipRole.APPROVER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        with self.assertRaisesRegex(DefinitionConflictError, "MFA"):
            self.service.record_publication_approval(
                draft.record_id,
                actor=weak_approver,
                reason="weak approval must fail",
            )

        approval = self.service.record_publication_approval(
            draft.record_id,
            actor=self.approver,
            reason="approve bounded expansion",
        )
        stored = self.service.get_record(draft.record_id)
        evidence = next(item for item in stored.publication_approvals if item.id == approval.id)
        self.assertEqual(evidence.approved_by, self.approver.identity_id)
        self.assertEqual(evidence.approver_assurance, "mfa")
        self.assertIn("approver", evidence.approver_roles)

        restriction = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {"max_amount_usd": 75}
            )
        )
        later = self._draft(restriction, actor="other-creator")
        later_published = self.service.publish(
            later.record_id,
            DefinitionPublishRequest(
                actor="other-publisher",
                expected_active_revision=self.active.revision,
            ),
        )
        self.assertGreater(later_published.revision, self.active.revision)

        with self.assertRaisesRegex(DefinitionConflictError, "stale"):
            self.service.publish(
                draft.record_id,
                DefinitionPublishRequest(
                    actor=self.publisher,
                    publication_approval_id=approval.id,
                ),
            )

    def test_free_form_approval_metadata_cannot_replace_canonical_evidence(self) -> None:
        expanded = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {"max_model_calls": 5}
            )
        )
        draft = self._draft(expanded)
        with self.assertRaisesRegex(
            DefinitionConflictError,
            "requires approved preflight evidence",
        ):
            self.service.publish(
                draft.record_id,
                DefinitionPublishRequest(
                    actor=self.publisher,
                    expected_active_revision=self.active.revision,
                    approval_metadata={
                        "publication_approval_id": "forged",
                        "approved_by": "forged-approver",
                    },
                ),
            )

    def test_expansive_rollback_is_guarded_and_leaves_reviewable_draft(self) -> None:
        expanded = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {"max_amount_usd": 200}
            )
        )
        expansion_draft = self._draft(expanded)
        approval = self.service.record_publication_approval(
            expansion_draft.record_id,
            actor=self.approver,
            reason="approve temporary expansion",
        )
        expanded_active = self.service.publish(
            expansion_draft.record_id,
            DefinitionPublishRequest(
                actor=self.publisher,
                expected_active_revision=self.active.revision,
                publication_approval_id=approval.id,
            ),
        )

        restricted = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {"max_amount_usd": 50}
            )
        )
        restricted_draft = self._draft(restricted, actor="other-creator")
        restricted_active = self.service.publish(
            restricted_draft.record_id,
            DefinitionPublishRequest(
                actor="other-publisher",
                expected_active_revision=expanded_active.revision,
            ),
        )

        with self.assertRaisesRegex(
            DefinitionConflictError,
            "requires approved preflight evidence",
        ):
            self.service.rollback(
                DefinitionRollbackRequest(
                    definition_id=AUTHORITY_ROLE_CATALOG_ID,
                    kind=AUTHORITY_ROLE_CATALOG_KIND,
                    scope_type=DefinitionScope.WORKSPACE,
                    scope_id="ws-a",
                    target_revision=expanded_active.revision,
                    actor="rollback-publisher",
                    expected_active_revision=restricted_active.revision,
                )
            )

        drafts = [
            item
            for item in self.service.list_records(
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                scope_type=DefinitionScope.WORKSPACE,
                scope_id="ws-a",
            )
            if item.lifecycle.value == "draft"
            and item.rollback_of_record_id == expanded_active.record_id
        ]
        self.assertEqual(len(drafts), 1)
        rollback_preflight = self.service.publication_preflight(
            drafts[0].record_id
        )
        self.assertTrue(rollback_preflight.requires_approval)
        self.assertIn(
            "authority.max_amount_usd_increased",
            rollback_preflight.change_classes,
        )

    def test_approval_can_publish_exact_sensitive_preflight_only_once_current(self) -> None:
        expanded = self._changed(
            lambda p: p["roles"][1]["grants"][0].update(
                {"max_model_calls": 4}
            )
        )
        draft = self._draft(expanded)
        assessment = self.service.publication_preflight(draft.record_id)
        approval = self.service.record_publication_approval(
            draft.record_id,
            actor=self.approver,
            reason="approve model-call expansion",
        )

        published = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=self.publisher,
                expected_active_revision=self.active.revision,
                publication_approval_id=approval.id,
            ),
        )

        self.assertEqual(
            published.approval_metadata["publication_preflight_fingerprint"],
            assessment.fingerprint,
        )
        self.assertEqual(
            published.approval_metadata["publication_approval_id"],
            approval.id,
        )
        self.assertEqual(
            published.approval_metadata["publication_approved_by"],
            self.approver.identity_id,
        )


class AuthorityPublicationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        install_authority_roles(self.service)
        self.actor = _actor(
            "approver-a",
            roles=(MembershipRole.APPROVER,),
        )

        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_definitions_router(self.service))
        self.client = TestClient(app)

        baseline = AuthorityPublicationGuardTests._catalog()
        draft = self.service.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type=DefinitionScope.WORKSPACE,
                scope_id="ws-a",
                payload=baseline.model_dump(mode="json"),
                actor="creator-a",
            )
        )
        approval = self.service.record_publication_approval(
            draft.record_id,
            actor=self.actor,
            reason="initial workspace catalog",
        )
        self.active = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher-a",
                publication_approval_id=approval.id,
            ),
        )

        payload = baseline.model_dump(mode="json")
        payload["roles"][1]["grants"][0]["environments"] = [
            AuthorityEnvironment.STAGING.value,
            AuthorityEnvironment.PRODUCTION.value,
        ]
        candidate = AuthorityRoleCatalogDefinition.model_validate(payload)
        self.draft = self.service.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type=DefinitionScope.WORKSPACE,
                scope_id="ws-a",
                payload=candidate.model_dump(mode="json"),
                actor="creator-b",
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_api_preflight_requires_distinct_approver_and_publish_uses_approval_id(self) -> None:
        preflight = self.client.get(
            f"/api/definitions/{self.draft.record_id}/publication-preflight"
        )
        self.assertEqual(preflight.status_code, 200)
        assessment = preflight.json()["assessment"]
        self.assertTrue(assessment["requires_approval"])
        self.assertIn(
            "authority.production_scope_added",
            assessment["change_classes"],
        )

        self.actor = _actor(
            "ordinary-admin",
            roles=(MembershipRole.ADMIN,),
        )
        denied = self.client.post(
            f"/api/definitions/{self.draft.record_id}/publication-approvals",
            json={"reason": "admin without approver role"},
        )
        self.assertEqual(denied.status_code, 403)

        self.actor = _actor(
            "approver-b",
            roles=(MembershipRole.APPROVER,),
        )
        approved = self.client.post(
            f"/api/definitions/{self.draft.record_id}/publication-approvals",
            json={"reason": "approve production authority"},
        )
        self.assertEqual(approved.status_code, 200)
        approval_id = approved.json()["approval"]["id"]

        self.actor = _actor(
            "publisher-b",
            roles=(MembershipRole.ADMIN,),
        )
        published = self.client.post(
            f"/api/definitions/{self.draft.record_id}/publish",
            json={
                "reason": "publish approved authority",
                "expected_active_revision": self.active.revision,
                "publication_approval_id": approval_id,
            },
        )
        self.assertEqual(published.status_code, 200)
        metadata = published.json()["record"]["approval_metadata"]
        self.assertEqual(metadata["publication_approval_id"], approval_id)
        self.assertEqual(metadata["publication_requires_approval"], "true")


if __name__ == "__main__":
    unittest.main()
