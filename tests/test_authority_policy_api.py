from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.authority import build_authority_router
from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.authority_policy_explorer import AuthorityPolicyExplorerService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.projects import ProjectNotFoundError
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Projects:
    def get(self, project_id, scope):
        if (
            project_id == "project-a"
            and scope.organization_id == "local"
            and scope.workspace_id == "default"
        ):
            return object()
        raise ProjectNotFoundError("project not found")


class _WorkItems:
    def get(self, ref):
        if ref != "work-visible":
            raise LookupError("work item not found")
        return {
            "ref": ref,
            "organization_id": "local",
            "workspace_id": "default",
            "project_id": "project-a",
        }

    def list(self, *, project_id, owner, stage, release_gate, scope):
        del project_id, owner, stage, release_gate
        if scope.organization_id != "local" or scope.workspace_id != "default":
            return {"items": [], "count": 0}
        return {
            "items": [{"ref": "work-visible", "project_id": "project-a"}],
            "count": 1,
        }


class _Identity:
    def actor_for_identity(self, identity_id, *, scope):
        raise AssertionError(
            f"unexpected cross-identity lookup for {identity_id} in {scope}"
        )


class AuthorityPolicyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.authority = install_authority_roles(self.registry)
        self.explorer = AuthorityPolicyExplorerService(
            self.authority,
            self.registry,
        )
        self.admin = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        app = FastAPI()

        @app.middleware("http")
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.admin
            return await call_next(request)

        app.include_router(
            build_authority_router(
                self.explorer,
                self.authority,
                _Identity(),
                _WorkItems(),
                _Projects(),
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _catalog(level: AuthorityLevel) -> AuthorityRoleCatalogDefinition:
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="local-admin",
                    name="Local admin",
                    description="Fixture admin.",
                    grants=(
                        AuthorityGrant(
                            id="local-admin.deploy",
                            capability="deploy.release",
                            level=level,
                            project_ids=("project-a",),
                        ),
                    ),
                ),
            ),
            bindings=(
                AuthorityRoleBinding(
                    id="local-admin-binding",
                    role_id="local-admin",
                    subject_kind="identity",
                    subject_id="local-admin",
                    organization_id="local",
                    workspace_id="default",
                    project_ids=("project-a",),
                ),
            ),
        )

    def _draft(self, level: AuthorityLevel):
        return self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                payload=self._catalog(level).model_dump(mode="json"),
                actor="test",
                reason="authority API fixture",
            )
        )

    def _publish(self, level: AuthorityLevel):
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        draft = self._draft(level)
        self.registry.approve_publication(
            draft.record_id,
            actor="other-admin",
            reference="TEST",
            reason="fixture approval",
        )
        return self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=active.revision,
            ),
        )

    def test_effective_project_context_is_fenced_by_canonical_project_service(self):
        allowed = self.client.get(
            "/api/authority/effective",
            params={"project_id": "project-a"},
        )
        self.assertEqual(allowed.status_code, 200)

        denied = self.client.get(
            "/api/authority/effective",
            params={"project_id": "project-other"},
        )
        self.assertEqual(denied.status_code, 404)

    def test_exact_revision_simulation_does_not_publish_candidate(self):
        self._publish(AuthorityLevel.READ)
        candidate = self._draft(AuthorityLevel.EXECUTE)

        response = self.client.post(
            "/api/authority/simulate",
            json={
                "record_id": candidate.record_id,
                "request": {
                    "capability": "deploy.release",
                    "level": "execute",
                    "project_id": "project-a",
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "exact-revision")
        self.assertEqual(
            response.json()["decision"]["definition_ref"]["record_id"],
            candidate.record_id,
        )
        self.assertEqual(response.json()["decision"]["outcome"], "allow")
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        self.assertNotEqual(active.record_id, candidate.record_id)

    def test_impact_filters_usage_to_tenant_visible_work_items(self):
        active = self._publish(AuthorityLevel.READ)
        self.registry.register_usage_provider(
            lambda reference: [
                {
                    "object_type": "work_item",
                    "object_id": "work-visible",
                    "project_id": "project-a",
                },
                {
                    "object_type": "work_item",
                    "object_id": "work-hidden",
                    "project_id": "other-project",
                },
            ]
            if reference.record_id == active.record_id
            else []
        )
        candidate = self._draft(AuthorityLevel.EXECUTE)

        response = self.client.get(f"/api/authority/impact/{candidate.record_id}")

        self.assertEqual(response.status_code, 200)
        affected = response.json()["affected"]
        self.assertEqual(affected["active_work_count"], 1)
        self.assertEqual(affected["active_work"][0]["object_id"], "work-visible")


if __name__ == "__main__":
    unittest.main()
