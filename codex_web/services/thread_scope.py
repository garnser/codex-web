from __future__ import annotations

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor
from codex_web.models import Project
from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.storage.thread_index import ThreadIndexRepository


class ThreadScopeService:
    """Resolve existing canonical session/Project ownership; never ask a runtime."""

    def __init__(self, projects: ProjectService, sessions: AgentSessionService,
                 index: ThreadIndexRepository) -> None:
        self.projects, self.sessions, self.index = projects, sessions, index

    @staticmethod
    def unavailable() -> HTTPException:
        return HTTPException(status_code=404, detail="Thread or Project not found")

    def project(self, project_id: str | None, actor: AuthenticationActor) -> Project:
        try:
            return self.projects.get(project_id, actor.tenant)
        except ProjectNotFoundError as exc:
            raise self.unavailable() from exc

    def ownership_snapshot(self) -> dict[str, set[tuple[str, str, str]]]:
        owners: dict[str, set[tuple[str, str, str]]] = {}
        for item in self.sessions.store.list():
            if item.provider_native_session_id:
                owners.setdefault(item.provider_native_session_id, set()).add(
                    (item.organization_id, item.workspace_id, item.project_id))
        return owners

    def project_id_for_thread(self, thread_id: str) -> str | None:
        """Resolve one canonical Project owner without widening actor scope."""
        owners = self.ownership_snapshot().get(thread_id, set())
        if owners:
            if len(owners) != 1:
                return None
            return next(iter(owners))[2]
        row = self.index.get(thread_id)
        if row is None:
            return None
        if row.project_id:
            return row.project_id
        matches = [
            item.id
            for item in self.projects.list()
            if row.cwd and item.path == row.cwd
        ]
        return matches[0] if len(matches) == 1 else None

    def for_thread(self, thread_id: str, actor: AuthenticationActor,
                   project_id: str | None = None, *,
                   ownership: dict[str, set[tuple[str, str, str]]] | None = None) -> Project:
        # Match globally before checking actor scope: a foreign session must not
        # fall through into a legacy index row, and ambiguous native IDs fail closed.
        snapshot = self.ownership_snapshot() if ownership is None else ownership
        owners = snapshot.get(thread_id, set())
        if owners:
            if len(owners) != 1:
                raise self.unavailable()
            organization, workspace, owner = next(iter(owners))
            if (organization, workspace) != (actor.organization_id, actor.workspace_id):
                raise self.unavailable()
        else:
            row = self.index.get(thread_id)
            if row is None:
                raise self.unavailable()
            # Legacy rows may predate canonical sessions. A persisted Project ID
            # wins; a path-only row requires exactly one canonical Project owner.
            if row.project_id:
                owner = row.project_id
            else:
                matches = [item for item in self.projects.list() if row.cwd and item.path == row.cwd]
                if len(matches) != 1:
                    raise self.unavailable()
                owner = matches[0].id
        if project_id is not None and project_id != owner:
            raise self.unavailable()
        return self.project(owner, actor)
