from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Query, Request

from codex_web.identity import AuthenticationActor
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.resources import ResourceCatalogService


def project_record_context_parameter(
    project_id: str | None = Query(default=None, description="Optional Project view; shared inherited records retain their own scope."),
) -> str | None:
    # Publish the optional context on every route's OpenAPI contract. Canonical
    # visibility and scope matching are enforced below after authentication.
    return project_id


@dataclass(frozen=True)
class ProjectRecordScope:
    """Requested record view; existing tenant and mutation authority still apply."""

    project_id: str | None = None
    resource_ids: frozenset[str] = frozenset()

    @classmethod
    def from_request(
        cls,
        request: Request,
        actor: AuthenticationActor,
        projects: ProjectService | None,
        resources: ResourceCatalogService | None = None,
    ) -> ProjectRecordScope:
        if 'project_id' not in request.query_params:
            return cls()
        project_id = request.query_params['project_id']
        if not project_id or projects is None:
            raise HTTPException(status_code=404, detail='Project not found')
        try:
            project = projects.get(project_id, actor.tenant)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail='Project not found') from exc
        resource_ids = frozenset(
            item.id for item in resources.project_resources(project, actor=actor)
        ) if resources is not None else frozenset()
        return cls(project_id, resource_ids)

    def includes(self, scope_type: Any, scope_id: str | None) -> bool:
        if self.project_id is None:
            return True
        kind = getattr(scope_type, 'value', scope_type)
        if kind == 'project':
            return scope_id == self.project_id
        if kind == 'resource':
            return scope_id in self.resource_ids
        return True  # Inherited shared records retain their canonical scope.

    def require(self, scope_type: Any, scope_id: str | None) -> None:
        if not self.includes(scope_type, scope_id):
            raise HTTPException(status_code=404, detail='record not found in Project')

    def bind_context(self, context):
        if self.project_id is None:
            return context
        if context.project_id not in (None, self.project_id):
            raise HTTPException(status_code=404, detail='context not found in Project')
        resource_id = getattr(context, 'resource_id', None)
        if resource_id is not None:
            self.require('resource', resource_id)
        return context.model_copy(update={'project_id': self.project_id})
