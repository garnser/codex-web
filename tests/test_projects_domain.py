from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import (
    ProjectCreate,
    ProjectRepositorySelectionUpdate,
)
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

    def test_repository_selection_policy_persists_on_create_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ProjectRepository(root / "data" / "projects.json")
            service = ProjectService(repository)
            service.list()

            workspace = root / "workspace"
            workspace.mkdir()
            created = service.create(
                ProjectCreate(
                    name="Multi",
                    path=str(workspace),
                    repository_selection_policy="explicit",
                )
            )
            self.assertEqual(
                created.repository_selection_policy,
                "explicit",
            )

            updated = service.set_repository_selection_policy(
                created.id,
                ProjectRepositorySelectionUpdate(
                    repository_selection_policy="deterministic"
                ),
            )
            self.assertEqual(
                updated.repository_selection_policy,
                "deterministic",
            )

            reloaded = ProjectRepository(repository.path).load()
            persisted = next(
                item for item in reloaded
                if item.id == created.id
            )
            self.assertEqual(
                persisted.repository_selection_policy,
                "deterministic",
            )

            coordinated = service.set_repository_selection_policy(
                created.id,
                ProjectRepositorySelectionUpdate(
                    repository_selection_policy="coordinated"
                ),
            )
            self.assertEqual(
                coordinated.repository_selection_policy,
                "coordinated",
            )

    def test_last_project_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProjectRepository(Path(directory) / "projects.json")
            service = ProjectService(repository)
            service.list()

            with self.assertRaises(LastProjectDeletionError):
                service.delete("home")


if __name__ == "__main__":
    unittest.main()
