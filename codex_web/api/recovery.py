from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.recovery import RecoveryPolicy
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.recovery import (
    RecoveryConflictError,
    RecoveryError,
    RecoveryService,
)


def build_recovery_router(service: RecoveryService) -> APIRouter:
    router = APIRouter(prefix="/api/recovery", tags=["recovery"])

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "recovery:admin" not in actor.service_scopes:
                raise AuthorizationError("recovery:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, RecoveryConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, RecoveryError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        state = service.store.load()
        backups = [
            item.model_dump(mode="json")
            for item in sorted(
                (
                    item
                    for item in state.backups.values()
                    if item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        ]
        verifications = [
            item.model_dump(mode="json")
            for item in sorted(
                (
                    item
                    for item in state.verifications.values()
                    if item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ),
                key=lambda item: (item.verified_at, item.id),
                reverse=True,
            )
        ]
        return {
            "policy": (
                state.policy.model_dump(mode="json")
                if state.policy is not None
                else None
            ),
            "health": service.health(actor=actor).model_dump(mode="json"),
            "backups": backups,
            "verifications": verifications,
            "backup_schedule_id": state.backup_schedule_id,
            "verification_schedule_id": state.verification_schedule_id,
        }

    @router.put("/policy")
    async def configure(
        payload: RecoveryPolicy,
        request: Request,
    ) -> dict[str, Any]:
        try:
            policy = service.configure(payload, actor=admin(request))
            return {"policy": policy.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (RecoveryError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/backups")
    async def backup(request: Request) -> dict[str, Any]:
        try:
            item = service.create_backup(actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (RecoveryError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/backups/{backup_id}/verify")
    async def verify(
        backup_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.verify_restore(
                backup_id,
                actor=admin(request),
                publish_evidence=True,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (RecoveryError, AuthorizationError)):
                raise error(exc) from exc
            raise

    return router
