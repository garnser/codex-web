from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import ProjectCreate
from codex_web.services.projects import LastProjectDeletionError, ProjectService
from codex_web.storage.projects import ProjectRepository


class ProjectDomainTests(unittest.TestCase):
    def test_repository_bootstraps_and_service_persists_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ProjectRepository(root / "data" / "projects.json")
            service = ProjectService(repository)

            projects = service.list()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].id, "home")

            workspace = root / "workspace"
            workspace.mkdir()
            created = service.create(ProjectCreate(name="Test", path=str(workspace)))

            reloaded = ProjectRepository(repository.path).load()
            self.assertEqual({project.id for project in reloaded}, {"home", created.id})
            self.assertEqual(service.get(created.id).path, str(workspace.resolve()))

            service.delete(created.id)
            self.assertEqual([project.id for project in service.list()], ["home"])

    def test_last_project_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProjectRepository(Path(directory) / "projects.json")
            service = ProjectService(repository)
            service.list()

            with self.assertRaises(LastProjectDeletionError):
                service.delete("home")


if __name__ == "__main__":
    unittest.main()
