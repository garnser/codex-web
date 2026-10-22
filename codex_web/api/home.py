from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.models import HomeOperatorUpdate
from codex_web.services.home_operator import HomeOperatorConflict
from codex_web.services.identity import IdentityError, identity_http_error
from codex_web.services.home_overview import HomeOverviewService
from codex_web.services.projects import ProjectNotFoundError


def build_home_router(service: HomeOverviewService, operator=None, operator_goals=None) -> APIRouter:
    router = APIRouter(prefix="/api/home", tags=["home"])

    @router.get("/operator")
    def operator_status(request: Request):
        if operator is None:
            raise HTTPException(503, "Home operator configuration unavailable")
        try:
            return operator.status(request_actor(request))
        except ProjectNotFoundError as exc:
            raise HTTPException(404, "Home Project not found") from exc

    @router.put("/operator")
    def configure_operator(payload: HomeOperatorUpdate, request: Request):
        if operator is None:
            raise HTTPException(503, "Home operator configuration unavailable")
        try:
            return operator.configure(payload, request_actor(request))
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(404, "Home Project not found") from exc
        except HomeOperatorConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/operator/goals")
    def inspect_operator_goals(request: Request, expected_revision: int = Query(ge=1)):
        if operator_goals is None:
            raise HTTPException(503, 'Home operator Goal inspection unavailable')
        try:
            return operator_goals.inspect(request_actor(request), expected_revision=expected_revision)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(404, 'Home Project not found') from exc
        except HomeOperatorConflict as exc:
            raise HTTPException(409, str(exc)) from exc

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
