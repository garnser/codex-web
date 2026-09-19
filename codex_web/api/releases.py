from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.releases import (
    PromotionCreate,
    PromotionQueueRequest,
    ReleaseCreate,
    RollbackQueueRequest,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.releases import (
    ReleaseConflictError,
    ReleaseError,
    ReleaseGateError,
    ReleaseService,
)


def build_releases_router(service: ReleaseService) -> APIRouter:
    router = APIRouter(prefix="/api/releases", tags=["releases"])

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "release:admin" not in actor.service_scopes:
                raise AuthorizationError("release:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, ReleaseGateError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ReleaseConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ReleaseError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("")
    async def list_releases(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list(actor)
            ]
        }

    @router.post("")
    async def create_release(
        payload: ReleaseCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ReleaseError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.get("/{release_id}")
    async def get_release(release_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get(release_id, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except ReleaseError as exc:
            raise error(exc) from exc

    @router.post("/{release_id}/promotions")
    async def add_promotion(
        release_id: str,
        payload: PromotionCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.add_promotion(
                release_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ReleaseError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.get("/{release_id}/promotions/{promotion_id}/gate")
    async def promotion_gate(
        release_id: str,
        promotion_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = service.evaluate_promotion(
                release_id,
                promotion_id,
                actor=request_actor(request),
            )
            return {"evaluation": result.model_dump(mode="json")}
        except ReleaseError as exc:
            raise error(exc) from exc

    @router.post("/{release_id}/promotions/{promotion_id}/approval")
    async def request_approval(
        release_id: str,
        promotion_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.request_promotion_approval(
                release_id,
                promotion_id,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ReleaseError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{release_id}/promotions/{promotion_id}/queue")
    async def queue_promotion(
        release_id: str,
        promotion_id: str,
        payload: PromotionQueueRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.queue_promotion(
                release_id,
                promotion_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ReleaseError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{release_id}/promotions/{promotion_id}/sync")
    async def sync_promotion(
        release_id: str,
        promotion_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.sync_promotion(
                release_id,
                promotion_id,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ReleaseError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{release_id}/promotions/{promotion_id}/rollback")
    async def queue_rollback(
        release_id: str,
        promotion_id: str,
        payload: RollbackQueueRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.queue_rollback(
                release_id,
                promotion_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ReleaseError, AuthorizationError)):
                raise error(exc) from exc
            raise

    return router
