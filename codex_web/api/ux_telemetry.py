from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.identity import PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.ux_telemetry import UxTelemetryService
from codex_web.ux_telemetry import UxTelemetryEventCreate


def build_ux_telemetry_router(service: UxTelemetryService) -> APIRouter:
    router = APIRouter(prefix="/api/ux-telemetry", tags=["ux-telemetry"])

    def require_reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "ux-telemetry:read" not in actor.service_scopes:
                raise AuthorizationError("ux-telemetry:read service scope required")
            return actor
        IdentityService.require_admin(actor)
        return actor

    @router.get("/policy")
    async def policy(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return service.policy(actor.tenant)

    @router.post("/events")
    async def record_event(
        payload: UxTelemetryEventCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "ux-telemetry:write" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="ux-telemetry:write service scope required",
                )
        item = service.ingest(payload, scope=actor.tenant)
        if item is None:
            return {"accepted": False, "reason": "disabled"}
        return {
            "accepted": True,
            "event_id": item.id,
            "schema_version": item.schema_version,
        }

    @router.get("/events")
    async def export_events(
        request: Request,
        start_at: float | None = None,
        end_at: float | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, Any]:
        try:
            actor = require_reader(request)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        rows = service.events(
            scope=actor.tenant,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        return {
            "schema_version": "1.0",
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.get("/summary")
    async def summary(
        request: Request,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        try:
            actor = require_reader(request)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return service.summary(
            scope=actor.tenant,
            start_at=start_at,
            end_at=end_at,
        )

    return router
