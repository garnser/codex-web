from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.models import ProjectCreate
from codex_web.services.projects import InvalidProjectPathError, ProjectService
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.workspaces import WorkspaceMapper, WorkspacePathError


class WorkspaceMapperTests(unittest.TestCase):
    def test_relative_paths_resolve_below_runtime_root_and_persist_portably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project_dir = workspace / "customer" / "api"
            project_dir.mkdir(parents=True)
            repository = ProjectRepository(
                root / "data" / "projects.json",
                workspace_mapper=WorkspaceMapper(workspace),
            )
            service = ProjectService(repository)

            projects = service.list()
            self.assertEqual(projects[0].path, str(workspace.resolve()))
            created = service.create(ProjectCreate(name="API", path="customer/api"))
            self.assertEqual(created.path, str(project_dir.resolve()))
            self.assertEqual(service.get(created.id).path, str(project_dir.resolve()))

            persisted = json.loads(repository.path.read_text())
            by_id = {item["id"]: item for item in persisted}
            self.assertEqual(by_id["home"]["path"], ".")
            self.assertEqual(by_id[created.id]["path"], "customer/api")

    def test_absolute_source_root_paths_are_migrated_to_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime_project = workspace / "product"
            runtime_project.mkdir(parents=True)
            data_file = root / "projects.json"
            data_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "product",
                            "name": "Product",
                            "path": "/srv/development/product",
                            "model": None,
                            "sandbox": "workspace-write",
                            "approval_policy": "on-request",
                        }
                    ]
                )
            )
            repository = ProjectRepository(
                data_file,
                workspace_mapper=WorkspaceMapper(
                    workspace,
                    source_root=Path("/srv/development"),
                ),
            )

            projects = repository.load()

            self.assertEqual(projects[0].path, str(runtime_project.resolve()))
            persisted = json.loads(data_file.read_text())
            self.assertEqual(persisted[0]["path"], "product")

    def test_relative_escape_and_unmapped_absolute_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            mapper = WorkspaceMapper(workspace)

            with self.assertRaises(WorkspacePathError):
                mapper.runtime_path("../outside")
            with self.assertRaises(WorkspacePathError):
                mapper.runtime_path("/etc")

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / "escape").symlink_to(outside, target_is_directory=True)
            mapper = WorkspaceMapper(workspace)

            with self.assertRaises(WorkspacePathError):
                mapper.runtime_path("escape")

    def test_project_service_reports_workspace_boundary_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            service = ProjectService(
                ProjectRepository(
                    root / "projects.json",
                    workspace_mapper=WorkspaceMapper(workspace),
                )
            )

            with self.assertRaises(InvalidProjectPathError):
                service.create(ProjectCreate(name="Escape", path="/etc"))

    def test_native_mode_preserves_absolute_path_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            mapper = WorkspaceMapper()

            self.assertEqual(mapper.runtime_path(project), project.resolve())
            self.assertEqual(mapper.storage_path(project), str(project.resolve()))

    def test_relative_stored_path_without_workspace_root_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "projects.json"
            original = [
                {
                    "id": "product",
                    "name": "Product",
                    "path": "customer/product",
                    "model": None,
                    "sandbox": "workspace-write",
                    "approval_policy": "on-request",
                }
            ]
            data_file.write_text(json.dumps(original))
            repository = ProjectRepository(
                data_file,
                workspace_mapper=WorkspaceMapper(),
            )

            with self.assertRaisesRegex(
                WorkspacePathError,
                "CODEX_WEB_WORKSPACE_ROOT",
            ):
                repository.load()

            self.assertEqual(json.loads(data_file.read_text()), original)

    def test_relative_shared_state_without_workspace_root_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_file = root / "projects.json"
            original = [
                {
                    "id": "product",
                    "name": "Product",
                    "path": "customer/product",
                    "model": None,
                    "sandbox": "workspace-write",
                    "approval_policy": "on-request",
                }
            ]
            data_file.write_text(json.dumps(original))
            store = SQLiteStateStore(root / "state.sqlite3")
            store.put("projects", original)
            repository = ProjectRepository(
                data_file,
                workspace_mapper=WorkspaceMapper(),
                store=store,
            )

            with self.assertRaisesRegex(
                WorkspacePathError,
                "CODEX_WEB_WORKSPACE_ROOT",
            ):
                repository.load()

            self.assertEqual(store.get("projects"), original)
            self.assertEqual(json.loads(data_file.read_text()), original)


if __name__ == "__main__":
    unittest.main()
