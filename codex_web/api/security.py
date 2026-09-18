from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.security import TrustZone, ZONE_CLASS
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.security_boundary import SecurityBoundaryService


def build_security_router(service: SecurityBoundaryService) -> APIRouter:
    router = APIRouter(tags=["security"])

    @router.get("/api/security/trust-zones")
    async def trust_zones(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {
            "items": [
                {
                    "zone": zone.value,
                    "trust_class": ZONE_CLASS[zone].value,
                }
                for zone in TrustZone
            ]
        }

    @router.get("/api/security/events")
    async def security_events(
        request: Request,
        violation_only: bool = False,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        items = service.events(actor, violation_only=violation_only)
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    return router
