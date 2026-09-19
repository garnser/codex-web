from __future__ import annotations

import json
from pathlib import Path

from codex_web.models import Project
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import StateStore
from codex_web.workspaces import WorkspaceMapper


class ProjectRepository:
    namespace = "projects"

    def __init__(
        self,
        path: Path,
        *,
        workspace_mapper: WorkspaceMapper | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.path = path
        self.workspace_mapper = workspace_mapper or WorkspaceMapper.from_environment()
        self.store = store

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

    def _legacy_or_default_payload(self) -> list[dict[str, object]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            projects = [
                Project(
                    id="home",
                    name="Home",
                    path=str(self.workspace_mapper.default_project_path()),
                )
            ]
            return self._storage_payload(projects)
        raw_payload = json.loads(self.path.read_text())
        if not isinstance(raw_payload, list):
            raise ValueError("project state must be a list")
        return raw_payload

    def load(self) -> list[Project]:
        if self.store is None:
            raw_payload = self._legacy_or_default_payload()
        else:
            raw_payload = self.store.get(self.namespace)
            if raw_payload is None:
                raw_payload = self._legacy_or_default_payload()
                self.store.put(self.namespace, raw_payload)
        stored_projects = [Project.model_validate(item) for item in raw_payload]
        projects = [self._runtime_project(project) for project in stored_projects]

        normalized_payload = self._storage_payload(projects)
        if normalized_payload != raw_payload:
            if self.store is not None:
                self.store.put(self.namespace, normalized_payload)
            self._write_payload(normalized_payload)
        elif not self.path.exists():
            self._write_payload(normalized_payload)
        return projects

    def save(self, projects: list[Project]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._storage_payload(projects)
        if self.store is not None:
            self.store.put(self.namespace, payload)
        self._write_payload(payload)
