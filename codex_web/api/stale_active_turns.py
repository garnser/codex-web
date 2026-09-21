from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.identity import (
    IdentityError,
    IdentityService,
    identity_http_error,
)
from codex_web.services.stale_active_turns import (
    ActiveTurnResolutionRequest,
    StaleActiveTurnRecoveryService,
)


def build_stale_active_turn_router(
    service: StaleActiveTurnRecoveryService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/active-turn-recovery",
        tags=["runtime", "recovery"],
    )

    def admin(request: Request):
        actor = request_actor(request)
        IdentityService.require_admin(actor)
        return actor

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        try:
            admin(request)
            return await asyncio.to_thread(service.status)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.get("/inspect")
    async def inspect(
        request: Request,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            admin(request)
            return await asyncio.to_thread(
                service.inspect,
                limit=max(1, min(limit, 1000)),
                thread_id=thread_id,
            )
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/reconcile")
    async def reconcile(request: Request) -> dict[str, Any]:
        try:
            actor = admin(request)
            report = await service.reconcile(
                reason="operator",
                actor_id=actor.identity_id,
            )
            return report.model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/{thread_id}/resolve")
    async def resolve(
        thread_id: str,
        payload: ActiveTurnResolutionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
            record = await service.resolve(
                thread_id,
                payload,
                actor_id=actor.identity_id,
            )
            return {"record": record.model_dump(mode="json")}
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from exc

    return router
