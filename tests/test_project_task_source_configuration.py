from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from codex_web.api.identity import install_identity_middleware
from codex_web.api.projects import build_projects_router
from codex_web.api.work_items import build_work_items_router
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import Project, ProjectCreate, TaskSourceConfiguration
from codex_web.services.identity import IdentityService
from codex_web.services.projects import ProjectService
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


class ProjectTaskSourceConfigurationTests(unittest.TestCase):
    def test_catalog_exposes_canonical_configuration_schema_and_filters_foreign_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = self._service(root)
            app = self._app(root, projects)
            service = SimpleNamespace(state_machine=SimpleNamespace(),
                work_items=SimpleNamespace(load_projects=projects.list))
            app.include_router(build_work_items_router(service))
            with patch('codex_web.services.work_item_operator.WorkItemOperatorService.task_source_catalog',
                       return_value={'items': [{'project_id': 'home'}, {'project_id': 'foreign'}]}):
                with TestClient(app) as client:
                    response = client.get('/api/task-sources')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['items'], [{'project_id': 'home'}])
            self.assertEqual(response.json()['configuration_schema'], TaskSourceConfiguration.model_json_schema())

    def _service(self, root: Path) -> ProjectService:
        repository = ProjectRepository(root / "projects.json")
        service = ProjectService(repository)
        service.list()
        return service

    def _app(self, root: Path, service: ProjectService) -> FastAPI:
        app = FastAPI()
        identity = IdentityService(
            IdentityStateStore(SQLiteStateStore(root / "identity.sqlite3"))
        )
        identity.bootstrap_local()
        install_identity_middleware(app, identity)
        app.include_router(build_projects_router(service))
        return app

    def test_delivery_configuration_roundtrips_through_canonical_project_api(self):
        from unittest.mock import Mock
        from codex_web.services.project_delivery import ProjectDeliveryService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = self._service(root)
            projects.set_authoritative_task_source('home', TaskSourceConfiguration(
                source_type='github', source_instance='https://api.github.com', scope='owner/repo'))
            app = self._app(root, projects)
            scope = Mock()
            app.state.project_delivery_service = ProjectDeliveryService(
                projects=projects, identity=None, scope=scope, operator=None, states=None,
                turns=None, execution=None, controller=None, events=None, event_sink=None)
            with TestClient(app) as client:
                response = client.put('/api/projects/home/delivery-supervision', json={'thread_id': 'thread-home'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['delivery_supervision']['actor_identity_id'], 'local-admin')
                restored = ProjectService(ProjectRepository(root / 'projects.json')).get('home')
                self.assertEqual(restored.delivery_supervision.thread_id, 'thread-home')
                response = client.get('/api/projects/home/delivery-supervision')
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()['last_scan'])
                response = client.put('/api/projects/home/delivery-supervision', json={'thread_id': None})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(projects.get('home').delivery_supervision)
                response = client.put('/api/projects/home/delivery-supervision', json={
                    'thread_id': 'thread-home', 'actor_identity_id': 'foreign-admin'})
                self.assertEqual(response.status_code, 422)

    def test_task_source_configuration_is_strict_and_non_empty(self) -> None:
        source = TaskSourceConfiguration(
            source_type=" gitlab ",
            source_instance=" https://gitlab.example/api/v4 ",
            scope=" group/project ",
        )
        self.assertEqual(source.source_type, "gitlab")
        self.assertEqual(source.scope, "group/project")

        with self.assertRaises(ValidationError):
            TaskSourceConfiguration(
                source_type="",
                source_instance="instance",
                scope="scope",
            )

        with self.assertRaises(ValidationError):
            TaskSourceConfiguration.model_validate(
                {
                    "source_type": "gitlab",
                    "source_instance": "instance",
                    "scope": "scope",
                    "token": "must-not-live-here",
                }
            )

    def test_legacy_project_without_task_source_remains_valid(self) -> None:
        project = Project.model_validate(
            {
                "id": "home",
                "name": "Home",
                "path": "/tmp/home",
            }
        )
        self.assertIsNone(project.authoritative_task_source)

    def test_project_create_persists_optional_authoritative_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            service = self._service(root)
            source = TaskSourceConfiguration(
                source_type="gitlab",
                source_instance="https://gitlab.example/api/v4",
                scope="group/project",
            )

            project = service.create(
                ProjectCreate(
                    name="Source project",
                    path=str(workspace),
                    authoritative_task_source=source,
                )
            )

            reloaded = ProjectRepository(root / "projects.json").load()
            persisted = next(item for item in reloaded if item.id == project.id)
            self.assertEqual(persisted.authoritative_task_source, source)

    def test_set_replace_and_clear_preserve_exactly_one_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            first = TaskSourceConfiguration(
                source_type="gitlab",
                source_instance="https://gitlab.example/api/v4",
                scope="group/project",
            )
            second = TaskSourceConfiguration(
                source_type="reference",
                source_instance="local-reference",
                scope="backlog",
            )

            updated = service.set_authoritative_task_source("home", first)
            self.assertEqual(updated.authoritative_task_source, first)

            replaced = service.set_authoritative_task_source("home", second)
            self.assertEqual(replaced.authoritative_task_source, second)
            self.assertNotEqual(replaced.authoritative_task_source, first)

            persisted = ProjectRepository(root / "projects.json").load()[0]
            self.assertEqual(persisted.authoritative_task_source, second)

            cleared = service.set_authoritative_task_source("home", None)
            self.assertIsNone(cleared.authoritative_task_source)
            self.assertIsNone(ProjectRepository(root / "projects.json").load()[0].authoritative_task_source)

    def test_project_api_sets_and_clears_canonical_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            app = self._app(Path(directory), service)
            client = TestClient(app)

            response = client.put(
                "/api/projects/home/task-source",
                json={
                    "source_type": "gitlab",
                    "source_instance": "https://gitlab.example/api/v4",
                    "scope": "group/project",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["authoritative_task_source"],
                {
                    "source_type": "gitlab",
                    "source_instance": "https://gitlab.example/api/v4",
                    "scope": "group/project",
                },
            )

            listed = client.get("/api/projects").json()
            self.assertEqual(listed[0]["authoritative_task_source"]["scope"], "group/project")

            response = client.delete("/api/projects/home/task-source")
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["authoritative_task_source"])

    def test_project_api_returns_not_found_for_unknown_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            app = self._app(root, service)
            client = TestClient(app)

            project = client.get("/api/projects/home")
            self.assertEqual(project.status_code, 200)
            self.assertEqual(project.json()["id"], "home")

            missing = client.get("/api/projects/missing")
            self.assertEqual(missing.status_code, 404)

            response = client.put(
                "/api/projects/missing/task-source",
                json={
                    "source_type": "reference",
                    "source_instance": "local",
                    "scope": "backlog",
                },
            )
            self.assertEqual(response.status_code, 404)

    def test_task_source_mutations_require_admin_and_actor_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            service.repository.save([*service.list(), Project(id='foreign', name='Foreign', path=str(root),
                organization_id='other', workspace_id='default')])
            actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.HUMAN,
                organization_id='local', workspace_id='default', roles=(MembershipRole.MEMBER,),
                assurance=AuthenticationAssurance.MFA)
            app = FastAPI()
            @app.middleware('http')
            async def identity(request, call_next):
                request.state.identity_actor = actor
                request.state.tenant_scope = actor.tenant
                return await call_next(request)
            app.include_router(build_projects_router(service))
            source = {'source_type': 'gitlab', 'source_instance': 'https://gitlab.example',
                      'scope': 'team/project', 'credential_secret_id': 'secret-reference'}
            with TestClient(app) as client:
                self.assertEqual(client.put('/api/projects/home/task-source', json=source).status_code, 403)
                self.assertEqual(client.delete('/api/projects/home/task-source').status_code, 403)
                actor = actor.model_copy(update={'roles': (MembershipRole.ADMIN,)})
                self.assertEqual(client.put('/api/projects/foreign/task-source', json=source).status_code, 404)
                self.assertEqual(client.delete('/api/projects/foreign/task-source').status_code, 404)
                self.assertEqual(client.put('/api/projects/home/task-source', json=source).status_code, 200)
                self.assertEqual(service.get('home').authoritative_task_source.credential_secret_id, 'secret-reference')
                self.assertEqual(client.delete('/api/projects/home/task-source').status_code, 200)
                self.assertIsNone(service.get('home').authoritative_task_source)


if __name__ == "__main__":
    unittest.main()
