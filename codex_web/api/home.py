from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.services.home_overview import HomeOverviewService
from codex_web.services.projects import ProjectNotFoundError


def build_home_router(service: HomeOverviewService) -> APIRouter:
    router = APIRouter(prefix="/api/home", tags=["home"])

    @router.get("")
    async def overview(
        request: Request,
        project_id: str = Query(min_length=1, max_length=500),
    ) -> dict[str, Any]:
        try:
            return await service.overview(
                actor=request_actor(request),
                project_id=project_id,
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    return router
