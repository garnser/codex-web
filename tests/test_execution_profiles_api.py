from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

from codex_web.api.execution_profiles import build_execution_profiles_router
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.identity import AuthenticationActor
from codex_web.models import Project
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.projects import ProjectService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutionProfilesApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(self.store))
        profiles = install_execution_profile_definitions(self.registry)
        repository = ProjectRepository(root / "projects.json")
        repository.save([
            Project(id="project-a", name="A", path=str(root), organization_id="org-a", workspace_id="workspace-a"),
            Project(id="project-c", name="C", path=str(root), organization_id="org-a", workspace_id="workspace-a"),
            Project(id="empty", name="Empty", path=str(root), organization_id="org-a", workspace_id="workspace-a"),
            Project(id="foreign", name="Foreign", path=str(root), organization_id="org-b", workspace_id="workspace-b"),
        ])
        for scope, scope_id, name in [
            ("workspace", "workspace-a", "Workspace A"),
            ("workspace", "workspace-b", "Workspace B"),
            ("project", "project-a", "Project A"),
            ("project", "project-c", "Project C"),
            ("project", "foreign", "Foreign Project"),
        ]:
            payload = execution_profile_catalog_seed_payload()
            payload["profiles"][0]["name"] = name
            record = self.registry.create_draft(DefinitionDraftCreate(
                kind="execution-profile-catalog", definition_id="execution.profiles.default",
                definition_schema_version="1.0", scope_type=scope, scope_id=scope_id,
                payload=payload, actor="fixture",
            ))
            self.registry.publish(record.record_id, DefinitionPublishRequest(actor="fixture"))
        app = FastAPI()

        @app.middleware("http")
        async def fixture_identity(request: Request, call_next):
            tenant = request.headers.get("x-fixture-tenant")
            if tenant in {"a", "b"}:
                request.state.identity_actor = AuthenticationActor(
                    identity_id=f"user-{tenant}", principal_kind="human", assurance="primary",
                    organization_id=f"org-{tenant}", workspace_id=f"workspace-{tenant}", roles=("member",),
                )
            return await call_next(request)

        app.include_router(build_execution_profiles_router(profiles, ProjectService(repository)))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def test_authentication_and_foreign_project_fail_closed(self):
        response = await self.client.get("/api/execution-profiles?project_id=project-a")
        self.assertEqual(response.status_code, 401)
        for project in ["foreign", "missing", ""]:
            response = await self.client.get("/api/execution-profiles", params={"project_id": project}, headers={"x-fixture-tenant": "a"})
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("Foreign Project", response.text)

    async def test_project_catalog_and_inherited_workspace_follow_actor_context(self):
        for project, name in [("project-a", "Project A"), ("project-c", "Project C"), ("empty", "Workspace A")]:
            response = await self.client.get("/api/execution-profiles", params={"project_id": project}, headers={"x-fixture-tenant": "a"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"][0]["name"], name)
            self.assertTrue(response.json()["definition"]["record_id"])

    async def test_tenant_catalog_without_project_uses_authenticated_workspace(self):
        for tenant in ["a", "b"]:
            response = await self.client.get("/api/execution-profiles", headers={"x-fixture-tenant": tenant})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"][0]["name"], f"Workspace {tenant.upper()}")
