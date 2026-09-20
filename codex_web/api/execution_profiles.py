from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.services.execution_profile_definitions import (
    ExecutionProfileDefinitionService,
)


def build_execution_profiles_router(
    service: ExecutionProfileDefinitionService,
) -> APIRouter:
    router = APIRouter(tags=["execution-profiles"])

    @router.get("/api/execution-profiles")
    async def list_execution_profiles(
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return service.public(project_id=project_id)

    return router
