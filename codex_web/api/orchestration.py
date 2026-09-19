from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.orchestration_inspector import OrchestrationInspectorService


def build_orchestration_router(service: OrchestrationInspectorService) -> APIRouter:
    router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])

    def require_reader(request: Request) -> None:
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in (
                    "orchestration:read",
                    "autonomy:read",
                    "autonomy:admin",
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "orchestration:read, autonomy:read, or autonomy:admin "
                        "service scope required"
                    ),
                )
            return
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/inspector")
    async def inspector(
        request: Request,
        limit: int = 100,
        event_type: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        require_reader(request)
        return service.snapshot(
            limit=limit,
            event_type=event_type,
            source=source,
        )

    return router
