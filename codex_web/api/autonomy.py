from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.autonomy import AutonomyControlUpdate
from codex_web.autonomy_policy import (
    AutonomyBreakGlassActivate,
    AutonomyBreakGlassRequest,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.autonomy_policy import (
    AutonomyBreakGlassError,
    AutonomyPolicyError,
    AutonomyPolicyService,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def build_autonomy_router(
    service: AutonomyController,
    policy: AutonomyPolicyService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])

    def require_reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("autonomy:read", "autonomy:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="autonomy:read or autonomy:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    def require_admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "autonomy:admin" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="autonomy:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    @router.get("")
    async def status(request: Request) -> dict[str, Any]:
        require_reader(request)
        return service.status()

    @router.patch("/control")
    async def update_control(
        payload: AutonomyControlUpdate,
        request: Request,
    ) -> dict[str, Any]:
        actor = require_admin(request)
        control = service.update_control(payload, actor_id=actor.identity_id)
        return {"control": control.model_dump(mode="json")}

    @router.post("/pause")
    async def pause(request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        control = service.pause(actor_id=actor.identity_id)
        return {"control": control.model_dump(mode="json")}

    @router.post("/resume")
    async def resume(request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        control = service.resume(actor_id=actor.identity_id)
        return {"control": control.model_dump(mode="json")}

    @router.post("/kill")
    async def kill(request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        control = service.kill(actor_id=actor.identity_id)
        return {"control": control.model_dump(mode="json")}

    @router.get("/policy/effective")
    async def effective_policy(
        request: Request,
        project_id: str | None = Query(default=None),
        action_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        actor = require_reader(request)
        if policy is None:
            raise HTTPException(
                status_code=503,
                detail="autonomy policy service is unavailable",
            )
        try:
            effective, role_ids = policy.effective(
                actor=actor,
                project_id=project_id,
                action_id=action_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "effective": effective.model_dump(mode="json"),
            "role_ids": list(role_ids),
        }

    @router.post("/break-glass/requests")
    async def request_break_glass(
        payload: AutonomyBreakGlassRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = require_admin(request)
        if policy is None:
            raise HTTPException(
                status_code=503,
                detail="autonomy policy service is unavailable",
            )
        try:
            approval = await policy.request_break_glass(
                payload,
                actor=actor,
            )
        except AutonomyPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"approval_request": approval.model_dump(mode="json")}

    @router.post("/break-glass/activate")
    async def activate_break_glass(
        payload: AutonomyBreakGlassActivate,
        request: Request,
    ) -> dict[str, Any]:
        actor = require_admin(request)
        if policy is None:
            raise HTTPException(
                status_code=503,
                detail="autonomy policy service is unavailable",
            )
        try:
            grant = await policy.activate_break_glass(
                payload.approval_request_id,
                actor=actor,
            )
        except AutonomyBreakGlassError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"grant": grant.model_dump(mode="json")}

    @router.get("/break-glass")
    async def active_break_glass(
        request: Request,
        project_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        actor = require_reader(request)
        if policy is None:
            return {"items": [], "count": 0}
        rows = policy.break_glass_grants(
            actor=actor,
            project_id=project_id,
        )
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    return router
