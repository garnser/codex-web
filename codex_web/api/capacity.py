from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.capacity import CapacityPolicy, CapacityQualificationReport
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.capacity import CapacityService
from codex_web.services.identity import AuthorizationError, IdentityService


def build_capacity_router(service: CapacityService) -> APIRouter:
    router = APIRouter(prefix="/api/capacity", tags=["capacity"])

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "capacity:admin" not in actor.service_scopes:
                raise AuthorizationError("capacity:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    @router.get("")
    async def status(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        state = service.store.load()
        return {
            "policy": state.policy.model_dump(mode="json"),
            "health": service.health(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            ).model_dump(mode="json"),
            "circuits": [
                item.model_dump(mode="json")
                for item in state.circuits.values()
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ],
            "qualifications": [
                item.model_dump(mode="json")
                for item in state.qualifications.values()
            ],
        }

    @router.put("/policy")
    async def set_policy(
        payload: CapacityPolicy,
        request: Request,
    ) -> dict[str, Any]:
        try:
            admin(request)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {
            "policy": service.set_policy(payload).model_dump(mode="json")
        }

    @router.post("/qualifications")
    async def qualify(
        payload: CapacityQualificationReport,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        result = service.qualify(
            payload,
            actor=actor,
            publish_evidence=True,
        )
        return {"item": result.model_dump(mode="json")}

    return router
