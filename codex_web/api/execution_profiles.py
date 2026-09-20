from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.execution_profile_definitions import (
    ExecutionProfileDefinitionService,
)
from codex_web.services.projects import ProjectNotFoundError, ProjectService


def build_execution_profiles_router(
    service: ExecutionProfileDefinitionService,
    projects: ProjectService,
) -> APIRouter:
    router = APIRouter(prefix="/api/execution-profiles", tags=["execution-profiles"])

    @router.get("")
    async def list_profiles(
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        effective_project_id = project_id
        if effective_project_id is not None:
            try:
                projects.get(effective_project_id, actor.tenant)
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=404, detail="project not found") from exc
        catalog = service.catalog(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=effective_project_id,
        )
        reference = service.reference(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=effective_project_id,
        )
        return {
            "items": [
                profile.public()
                for profile in catalog.profiles
                if profile.lifecycle != "disabled"
            ],
            "defaultProfileId": catalog.default_profile_id,
            "roleToProfile": dict(catalog.role_to_profile),
            "definition": reference.model_dump(mode="json"),
        }

    return router
