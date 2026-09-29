from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.projects import ProjectNotFoundError, ProjectService

from codex_web.services.execution_profile_definitions import (
    ExecutionProfileDefinitionService,
)


def build_execution_profiles_router(
    service: ExecutionProfileDefinitionService,
    projects: ProjectService,
) -> APIRouter:
    router = APIRouter(tags=["execution-profiles"])

    @router.get("/api/execution-profiles")
    async def list_execution_profiles(
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        if project_id is not None:
            try:
                projects.get(project_id, actor.tenant)
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=404, detail="Project not found") from exc
        return service.public(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project_id,
        )

    return router
