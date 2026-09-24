from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityEvaluationRequest,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.resources import ResourceRisk, ResourceSensitivity, ResourceType
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.authority_policy_explorer import AuthorityPolicyExplorerService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AuthorityPolicyExplorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.authority = install_authority_roles(self.registry)
        self.explorer = AuthorityPolicyExplorerService(
            self.authority,
            self.registry,
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

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _catalog(self, *, level: AuthorityLevel) -> AuthorityRoleCatalogDefinition:
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="base-reader",
                    name="Base reader",
                    description="Inherited repository read.",
                    grants=(
                        AuthorityGrant(
                            id="base.read",
                            capability="repository.read",
                            level=AuthorityLevel.READ,
                        ),
                    ),
                ),
                AuthorityRoleDefinition(
                    id="developer",
                    name="Developer",
                    description="Delivery authority.",
                    inherits=("base-reader",),
                    grants=(
                        AuthorityGrant(
                            id="developer.deploy",
                            capability="deploy.release",
                            level=level,
                            project_ids=("project-a",),
                        ),
                    ),
                ),
            ),
            bindings=(
                AuthorityRoleBinding(
                    id="developer-binding",
                    role_id="developer",
                    subject_kind="identity",
                    subject_id=self.actor.identity_id,
                    organization_id="local",
                    workspace_id="default",
                    project_ids=("project-a",),
                ),
            ),
        )

    def _draft(self, catalog: AuthorityRoleCatalogDefinition):
        return self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                payload=catalog.model_dump(mode="json"),
                actor="test",
                reason="policy explorer fixture",
            )
        )

    def _publish(self, catalog: AuthorityRoleCatalogDefinition):
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        draft = self._draft(catalog)
        self.registry.approve_publication(
            draft.record_id,
            actor="independent-approver",
            reference="TEST-APPROVAL",
            reason="fixture approval",
        )
        return self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=active.revision,
            ),
        )

    def test_effective_view_explains_assignment_inheritance_and_exact_definition(self):
        published = self._publish(self._catalog(level=AuthorityLevel.READ))

        view = self.explorer.effective(
            actor=self.actor,
            project_id="project-a",
            role_id="developer",
            now=1000.0,
        )

        self.assertEqual(
            view["definition"]["reference"]["record_id"],
            published.record_id,
        )
        self.assertEqual(view["assignments"][0]["source_id"], "developer-binding")
        rows = {
            item["grant_id"]: item
            for item in view["permission_matrix"]
        }
        self.assertEqual(rows["developer.deploy"]["inheritance_path"], ["developer"])
        self.assertEqual(
            rows["base.read"]["inheritance_path"],
            ["developer", "base-reader"],
        )
        self.assertEqual(
            view["role_view"]["effective_grants"][1]["source_type"],
            "role",
        )

    def test_exact_revision_simulation_uses_runtime_evaluator_without_activation(self):
        active = self._publish(self._catalog(level=AuthorityLevel.READ))
        candidate = self._draft(self._catalog(level=AuthorityLevel.EXECUTE))
        request = AuthorityEvaluationRequest(
            capability="deploy.release",
            level=AuthorityLevel.EXECUTE,
            project_id="project-a",
        )

        runtime = self.authority.evaluate(request, actor=self.actor, now=1000.0)
        simulated = self.authority.evaluate_record(
            candidate.record_id,
            request,
            actor=self.actor,
            now=1000.0,
        )

        self.assertEqual(runtime.outcome.value, "deny")
        self.assertEqual(simulated.outcome.value, "allow")
        self.assertEqual(simulated.definition_ref.record_id, candidate.record_id)
        still_active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        self.assertEqual(still_active.record_id, active.record_id)

    def test_impact_preview_includes_changed_subjects_projects_and_active_work(self):
        active = self._publish(self._catalog(level=AuthorityLevel.READ))
        self.registry.register_usage_provider(
            lambda reference: [
                {
                    "object_type": "work_item",
                    "object_id": "work-1",
                    "project_id": "project-a",
                }
            ]
            if reference.record_id == active.record_id
            else []
        )
        candidate = self._draft(self._catalog(level=AuthorityLevel.EXECUTE))

        impact = self.explorer.impact(candidate.record_id)

        self.assertTrue(impact["assessment"]["requires_independent_approval"])
        self.assertIn("developer", impact["affected"]["role_ids"])
        self.assertIn(self.actor.identity_id, impact["affected"]["identity_ids"])
        self.assertIn("project-a", impact["affected"]["project_ids"])
        self.assertEqual(impact["affected"]["active_work_count"], 1)
        self.assertEqual(
            impact["affected"]["active_work"][0]["object_id"],
            "work-1",
        )


    def test_effective_for_resource_filters_resource_constraints_server_side(self):
        catalog = AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="resource-reader",
                    name="Resource reader",
                    description="Resource-scoped read authority.",
                    grants=(
                        AuthorityGrant(
                            id="repo.read",
                            capability="repository.read",
                            level=AuthorityLevel.READ,
                            resource_types=(ResourceType.REPOSITORY,),
                            resource_risks=(ResourceRisk.MEDIUM,),
                        ),
                        AuthorityGrant(
                            id="prod.read",
                            capability="environment.read",
                            level=AuthorityLevel.READ,
                            resource_types=(ResourceType.ENVIRONMENT,),
                        ),
                    ),
                ),
            ),
            bindings=(
                AuthorityRoleBinding(
                    id="resource-binding",
                    role_id="resource-reader",
                    subject_kind="identity",
                    subject_id=self.actor.identity_id,
                    organization_id="local",
                    workspace_id="default",
                ),
            ),
        )
        self._publish(catalog)
        repository = SimpleNamespace(
            id="repo-a",
            name="Repository A",
            resource_type=ResourceType.REPOSITORY,
            risk=ResourceRisk.MEDIUM,
            sensitivity=ResourceSensitivity.INTERNAL,
        )

        view = self.explorer.effective_for_resource(
            actor=self.actor,
            resource=repository,
            now=1000.0,
        )

        self.assertEqual(view["resource"]["id"], "repo-a")
        self.assertEqual(
            [row["grant_id"] for row in view["permission_matrix"]],
            ["repo.read"],
        )
        self.assertEqual(
            [row["source_id"] for row in view["assignments"]],
            ["resource-binding"],
        )

    def test_effective_for_resource_preserves_unconstrained_grants(self):
        self._publish(self._catalog(level=AuthorityLevel.READ))
        repository = SimpleNamespace(
            id="repo-a",
            name="Repository A",
            resource_type=ResourceType.REPOSITORY,
            risk=ResourceRisk.MEDIUM,
            sensitivity=ResourceSensitivity.INTERNAL,
        )

        view = self.explorer.effective_for_resource(
            actor=self.actor,
            project_id="project-a",
            resource=repository,
            now=1000.0,
        )

        self.assertEqual(
            {row["grant_id"] for row in view["permission_matrix"]},
            {"base.read", "developer.deploy"},
        )


if __name__ == "__main__":
    unittest.main()
