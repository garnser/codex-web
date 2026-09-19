from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityApprovalRequirement,
    AuthorityAutonomyRisk,
    AuthorityDelegation,
    AuthorityEnvironment,
    AuthorityEvaluationRequest,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.resources import (
    ResourceCreate,
    ResourceLifecycle,
    ResourceRisk,
    ResourceSensitivity,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AuthorityRoleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.service = install_authority_roles(self.registry, self.resources)
        self.admin = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.actor = AuthenticationActor(
            identity_id="developer-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            team_ids=("team-platform",),
            assurance=AuthenticationAssurance.MFA,
        )
        self.repo = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="application repository",
                risk=ResourceRisk.MEDIUM,
                sensitivity=ResourceSensitivity.INTERNAL,
            ),
            actor=self.admin,
        )
        self.prod = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.ENVIRONMENT,
                name="production",
                risk=ResourceRisk.CRITICAL,
                sensitivity=ResourceSensitivity.RESTRICTED,
            ),
            actor=self.admin,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(self, catalog: AuthorityRoleCatalogDefinition):
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                payload=catalog.model_dump(mode="json"),
                actor="test",
                reason="test authority catalog",
            )
        )
        self.registry.approve_publication(
            draft.record_id,
            actor="test-approver",
            reference="TEST-APPROVAL",
            reason="test fixture approval",
        )
        return self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="test",
                reason="activate test authority catalog",
                expected_active_revision=active.revision,
            ),
        )

    @staticmethod
    def _grant(**overrides):
        payload = {
            "id": "developer.execute",
            "capability": "deploy.release",
            "level": AuthorityLevel.EXECUTE,
            "project_ids": ("project-a",),
            "resource_types": (ResourceType.REPOSITORY,),
            "resource_risks": (ResourceRisk.MEDIUM,),
            "resource_sensitivities": (ResourceSensitivity.INTERNAL,),
            "environments": (AuthorityEnvironment.STAGING,),
            "max_amount_usd": 100.0,
            "max_input_tokens": 1000,
            "max_output_tokens": 500,
            "max_model_calls": 2,
            "max_autonomous_risk": AuthorityAutonomyRisk.MEDIUM,
            "approvals": AuthorityApprovalRequirement(
                count=1,
                role_ids=("release-approver",),
            ),
        }
        payload.update(overrides)
        return AuthorityGrant(**payload)

    def _catalog(
        self,
        *,
        grant: AuthorityGrant | None = None,
        bindings=(),
        delegations=(),
        inherits=(),
    ):
        grant = grant or self._grant()
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="base-reader",
                    name="Base reader",
                    description="Read-only inherited authority.",
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
                    description="Scoped delivery authority.",
                    inherits=tuple(inherits),
                    grants=(grant,),
                ),
            ),
            bindings=tuple(bindings),
            delegations=tuple(delegations),
        )

    def _binding(self, **overrides):
        payload = {
            "id": "developer-binding",
            "role_id": "developer",
            "subject_kind": "identity",
            "subject_id": self.actor.identity_id,
            "organization_id": "local",
            "workspace_id": "default",
        }
        payload.update(overrides)
        return AuthorityRoleBinding(**payload)

    def _request(self, **overrides):
        payload = {
            "capability": "deploy.release",
            "level": AuthorityLevel.EXECUTE,
            "project_id": "project-a",
            "resource_ids": (self.repo.id,),
            "environment": AuthorityEnvironment.STAGING,
            "amount_usd": 80.0,
            "input_tokens": 900,
            "output_tokens": 400,
            "model_calls": 2,
            "autonomous_risk": AuthorityAutonomyRisk.MEDIUM,
            "approval_role_ids": ("release-approver",),
        }
        payload.update(overrides)
        return AuthorityEvaluationRequest(**payload)

    def test_bootstrap_local_admin_is_exactly_attributed_and_tenant_scoped(self):
        allowed = self.service.evaluate(
            AuthorityEvaluationRequest(
                capability="anything.execute",
                level=AuthorityLevel.EXECUTE,
                autonomous_risk=AuthorityAutonomyRisk.CRITICAL,
            ),
            actor=self.admin,
        )
        self.assertEqual(allowed.outcome.value, "allow")
        self.assertEqual(allowed.matched_role_ids, ("local-admin",))
        self.assertEqual(allowed.matched_grant_ids, ("local-admin.all",))
        self.assertIsNotNone(allowed.definition_ref)
        self.assertEqual(allowed.definition_ref.revision, 1)

        other_tenant = self.admin.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        denied = self.service.evaluate(
            AuthorityEvaluationRequest(
                capability="anything.execute",
                level=AuthorityLevel.EXECUTE,
            ),
            actor=other_tenant,
        )
        self.assertEqual(denied.outcome.value, "deny")
        self.assertIn("no matching operational Role", denied.reasons[0])

    def test_one_complete_grant_must_satisfy_scope_budget_risk_and_approval(self):
        self._publish(self._catalog(bindings=(self._binding(),)))

        allowed = self.service.evaluate(self._request(), actor=self.actor)
        self.assertEqual(allowed.outcome.value, "allow")
        self.assertEqual(allowed.matched_grant_ids, ("developer.execute",))
        self.assertEqual(allowed.definition_ref.revision, 2)

        cases = (
            (
                {"project_id": "project-b"},
                "project is outside grant",
            ),
            (
                {"environment": AuthorityEnvironment.PRODUCTION},
                "environment is outside grant",
            ),
            (
                {"amount_usd": None},
                "explicit monetary amount",
            ),
            (
                {"amount_usd": 101.0},
                "monetary amount exceeds grant",
            ),
            (
                {"input_tokens": None},
                "explicit input-token budget",
            ),
            (
                {"input_tokens": 1001},
                "input-token budget exceeds grant",
            ),
            (
                {"output_tokens": None},
                "explicit output-token budget",
            ),
            (
                {"output_tokens": 501},
                "output-token budget exceeds grant",
            ),
            (
                {"model_calls": None},
                "explicit model-call budget",
            ),
            (
                {"model_calls": 3},
                "model-call budget exceeds grant",
            ),
            (
                {"autonomous_risk": AuthorityAutonomyRisk.HIGH},
                "autonomous risk exceeds grant",
            ),
            (
                {"approval_role_ids": ()},
                "qualifying approval",
            ),
        )
        for override, finding in cases:
            with self.subTest(override=override):
                denied = self.service.evaluate(
                    self._request(**override),
                    actor=self.actor,
                )
                self.assertEqual(denied.outcome.value, "deny")
                self.assertTrue(
                    any(finding in reason for reason in denied.reasons),
                    denied.reasons,
                )

    def test_resource_type_risk_sensitivity_and_explicit_target_fail_closed(self):
        self._publish(self._catalog(bindings=(self._binding(),)))

        missing = self.service.evaluate(
            self._request(resource_ids=()),
            actor=self.actor,
        )
        self.assertEqual(missing.outcome.value, "deny")
        self.assertTrue(
            any("explicit resource" in reason for reason in missing.reasons)
        )

        denied = self.service.evaluate(
            self._request(resource_ids=(self.prod.id,)),
            actor=self.actor,
        )
        self.assertEqual(denied.outcome.value, "deny")
        combined = " ".join(denied.reasons)
        self.assertIn("resource types outside grant", combined)
        self.assertIn("resource risk outside grant", combined)
        self.assertIn("resource sensitivity outside grant", combined)

        self.resources.update(
            self.repo.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DISABLED),
            actor=self.admin,
        )
        inactive = self.service.evaluate(
            self._request(resource_ids=(self.repo.id,)),
            actor=self.actor,
        )
        self.assertEqual(inactive.outcome.value, "deny")
        self.assertTrue(
            any("not active for privileged use" in reason for reason in inactive.reasons)
        )

    def test_inheritance_and_team_bindings_preserve_grant_origin(self):
        team_binding = self._binding(
            id="team-binding",
            subject_kind="team",
            subject_id="team-platform",
            role_id="developer",
        )
        self._publish(
            self._catalog(
                bindings=(team_binding,),
                inherits=("base-reader",),
            )
        )

        decision = self.service.evaluate(
            AuthorityEvaluationRequest(
                capability="repository.read",
                level=AuthorityLevel.READ,
                resource_ids=(self.repo.id,),
            ),
            actor=self.actor,
        )
        self.assertEqual(decision.outcome.value, "allow")
        self.assertEqual(decision.matched_role_ids, ("base-reader",))
        self.assertEqual(decision.matched_grant_ids, ("base.read",))

    def test_delegated_role_expires_and_cannot_escape_project(self):
        self._publish(
            self._catalog(
                delegations=(
                    AuthorityDelegation(
                        id="delegation-1",
                        role_id="developer",
                        delegate_identity_id=self.actor.identity_id,
                        delegated_by_identity_id="manager-a",
                        organization_id="local",
                        workspace_id="default",
                        project_ids=("project-a",),
                        expires_at=200.0,
                        reason="temporary release duty",
                    ),
                )
            )
        )

        allowed = self.service.evaluate(
            self._request(),
            actor=self.actor,
            now=100.0,
        )
        self.assertEqual(allowed.outcome.value, "allow")
        self.assertEqual(allowed.delegation_ids, ("delegation-1",))
        self.assertEqual(allowed.expires_at, 200.0)

        expired = self.service.evaluate(
            self._request(),
            actor=self.actor,
            now=201.0,
        )
        self.assertEqual(expired.outcome.value, "deny")
        self.assertIn("no matching operational Role", expired.reasons[0])

        wrong_project = self.service.evaluate(
            self._request(project_id="project-b"),
            actor=self.actor,
            now=100.0,
        )
        self.assertEqual(wrong_project.outcome.value, "deny")
        self.assertIn("no matching operational Role", wrong_project.reasons[0])

    def test_missing_definition_fails_closed_instead_of_falling_back(self):
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        self.registry.quarantine(
            active.record_id,
            actor="security",
            reason="test fail closed",
        )

        decision = self.service.evaluate(
            AuthorityEvaluationRequest(
                capability="repository.read",
                level=AuthorityLevel.READ,
            ),
            actor=self.admin,
        )
        self.assertEqual(decision.outcome.value, "deny")
        self.assertIsNone(decision.definition_ref)
        self.assertIn("definition unavailable", decision.reasons[0])

    def test_catalog_validation_rejects_cycles_unknown_roles_and_duplicate_bindings(self):
        with self.assertRaises(ValidationError):
            AuthorityRoleCatalogDefinition(
                roles=(
                    AuthorityRoleDefinition(
                        id="a",
                        name="A",
                        description="A",
                        inherits=("b",),
                    ),
                    AuthorityRoleDefinition(
                        id="b",
                        name="B",
                        description="B",
                        inherits=("a",),
                    ),
                )
            )

        with self.assertRaises(ValidationError):
            AuthorityRoleCatalogDefinition(
                roles=(
                    AuthorityRoleDefinition(
                        id="a",
                        name="A",
                        description="A",
                    ),
                ),
                bindings=(
                    AuthorityRoleBinding(
                        id="bad",
                        role_id="missing",
                        subject_kind="identity",
                        subject_id="x",
                    ),
                ),
            )

        binding = AuthorityRoleBinding(
            id="one",
            role_id="a",
            subject_kind="identity",
            subject_id="x",
        )
        with self.assertRaises(ValidationError):
            AuthorityRoleCatalogDefinition(
                roles=(
                    AuthorityRoleDefinition(
                        id="a",
                        name="A",
                        description="A",
                    ),
                ),
                bindings=(
                    binding,
                    binding.model_copy(update={"id": "two"}),
                ),
            )


if __name__ == "__main__":
    unittest.main()
