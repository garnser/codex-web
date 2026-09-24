from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from pathlib import Path

from codex_web.models import Project
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import StateStore
from codex_web.workspaces import WorkspaceMapper, WorkspacePathError


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
        self._snapshot: ContextVar[list[dict[str, object]] | None] = ContextVar(
            "codex_web_projects_snapshot",
            default=None,
        )

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

    def _validate_portable_paths(
        self,
        raw_payload: list[dict[str, object]],
    ) -> None:
        if self.workspace_mapper.root is not None:
            return
        for item in raw_payload:
            raw_path = str(item.get("path") or "").strip()
            if raw_path and not Path(raw_path).expanduser().is_absolute():
                raise WorkspacePathError(
                    "Portable relative project paths require "
                    "CODEX_WEB_WORKSPACE_ROOT; refusing to resolve and "
                    "rewrite them relative to the current working directory"
                )

    def load(self) -> list[Project]:
        if self.store is None:
            raw_payload = self._legacy_or_default_payload()
        else:
            raw_payload = self.store.get(self.namespace)
            if raw_payload is None:
                raw_payload = self._legacy_or_default_payload()
                self.store.put(self.namespace, raw_payload)
        self._validate_portable_paths(raw_payload)
        stored_projects = [Project.model_validate(item) for item in raw_payload]
        projects = [self._runtime_project(project) for project in stored_projects]

        normalized_payload = self._storage_payload(projects)
        if normalized_payload != raw_payload:
            if self.store is not None:
                self.store.put(self.namespace, normalized_payload)
            self._write_payload(normalized_payload)
        elif not self.path.exists():
            self._write_payload(normalized_payload)
        self._snapshot.set(copy.deepcopy(normalized_payload))
        return projects

    def save(self, projects: list[Project]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._storage_payload(projects)
        merged = payload
        if self.store is not None:
            base = self._snapshot.get()
            if base is None:
                self.store.put(self.namespace, payload)
            else:
                base_by_id = {
                    str(item.get("id")): item
                    for item in base
                    if item.get("id")
                }
                payload_by_id = {
                    str(item.get("id")): item
                    for item in payload
                    if item.get("id")
                }
                changed = {
                    item_id: item
                    for item_id, item in payload_by_id.items()
                    if base_by_id.get(item_id) != item
                }
                deleted = set(base_by_id) - set(payload_by_id)
                payload_order = [
                    str(item.get("id"))
                    for item in payload
                    if item.get("id")
                ]

                def merge(current):
                    current_rows = list(current) if isinstance(current, list) else []
                    result = []
                    seen = set()
                    for item in current_rows:
                        item_id = str(item.get("id") or "")
                        if not item_id or item_id in deleted:
                            continue
                        result.append(changed.get(item_id, item))
                        seen.add(item_id)
                    for item_id in payload_order:
                        if item_id in changed and item_id not in seen:
                            result.append(changed[item_id])
                            seen.add(item_id)
                    return result

                merged = self.store.update(
                    self.namespace,
                    merge,
                    default=[],
                )
        self._snapshot.set(copy.deepcopy(merged))
        self._write_payload(merged)
