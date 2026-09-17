from __future__ import annotations

import json
from pathlib import Path

from codex_web.models import Project
from codex_web.storage.json_files import atomic_write_text
from codex_web.workspaces import WorkspaceMapper


class ProjectRepository:
    def __init__(self, path: Path, *, workspace_mapper: WorkspaceMapper | None = None) -> None:
        self.path = path
        self.workspace_mapper = workspace_mapper or WorkspaceMapper.from_environment()

    def resolve_path(self, value: str | Path) -> Path:
        return self.workspace_mapper.runtime_path(value)

    def _runtime_project(self, project: Project) -> Project:
        return project.model_copy(update={"path": str(self.workspace_mapper.runtime_path(project.path))})

    def _storage_payload(self, projects: list[Project]) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for project in projects:
            item = project.model_dump()
            item["path"] = self.workspace_mapper.storage_path(project.path)
            payload.append(item)
        return payload

    def _write_payload(self, payload: list[dict[str, object]]) -> None:
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")

    def load(self) -> list[Project]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            projects = [
                Project(
                    id="home",
                    name="Home",
                    path=str(self.workspace_mapper.default_project_path()),
                )
            ]
            self.save(projects)
            return projects

        raw_payload = json.loads(self.path.read_text())
        stored_projects = [Project.model_validate(item) for item in raw_payload]
        projects = [self._runtime_project(project) for project in stored_projects]

        # Migrate portable/source-root paths in place once they can be mapped
        # safely. Runtime callers always receive absolute paths while persisted
        # state remains independent of the container/host mount point.
        normalized_payload = self._storage_payload(projects)
        if normalized_payload != raw_payload:
            self._write_payload(normalized_payload)
        return projects

    def save(self, projects: list[Project]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_payload(self._storage_payload(projects))
