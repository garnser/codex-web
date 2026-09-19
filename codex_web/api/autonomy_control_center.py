from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import PrincipalKind
from codex_web.services.autonomy_control_center import AutonomyControlCenterService
from codex_web.services.identity import AuthorizationError, IdentityService


def build_autonomy_control_center_router(
    service: AutonomyControlCenterService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/autonomy/control-center",
        tags=["autonomy-control-center"],
    )

    def reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not {
                "autonomy:read",
                "autonomy:admin",
                "orchestration:read",
            }.intersection(actor.service_scopes):
                raise HTTPException(
                    status_code=403,
                    detail="autonomy/orchestration read scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    @router.get("")
    async def snapshot(request: Request) -> dict[str, Any]:
        actor = reader(request)
        try:
            return service.snapshot(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/actions/{intent_id}/explain")
    async def explain_action(
        intent_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            return service.explain_action(intent_id, actor=actor)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
