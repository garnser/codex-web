from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from codex_web.api.projects import build_projects_router
from codex_web.models import Project, ProjectCreate, TaskSourceConfiguration
from codex_web.services.projects import ProjectService
from codex_web.storage.projects import ProjectRepository


class ProjectTaskSourceConfigurationTests(unittest.TestCase):
    def _service(self, root: Path) -> ProjectService:
        repository = ProjectRepository(root / "projects.json")
        service = ProjectService(repository)
        service.list()
        return service

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
            app = FastAPI()
            app.include_router(build_projects_router(service))
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
            service = self._service(Path(directory))
            app = FastAPI()
            app.include_router(build_projects_router(service))
            client = TestClient(app)

            response = client.put(
                "/api/projects/missing/task-source",
                json={
                    "source_type": "reference",
                    "source_instance": "local",
                    "scope": "backlog",
                },
            )
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
