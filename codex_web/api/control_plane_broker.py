from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.control_plane_broker import ControlPlaneBrokerService
from codex_web.services.identity import IdentityService


def _operator_actor(request: Request):
    actor = request_actor(request)
    IdentityService.require_admin(actor)
    if actor.principal_kind != PrincipalKind.SERVICE:
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
    return actor


def build_control_plane_broker_router(
    service: ControlPlaneBrokerService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/control-plane-broker",
        tags=["control-plane-broker"],
    )

    @router.get("/operations")
    async def list_operations(request: Request) -> dict[str, Any]:
        _operator_actor(request)
        return {
            "items": [
                {
                    "id": item.id,
                    "method": item.method,
                    "path_template": item.path_template,
                    "capability": item.capability,
                    "authority_level": item.authority_level.value,
                }
                for item in service.operations()
            ],
            "limits": service.limits.model_dump(mode="json"),
            "transport": "assignment-bound-unix-socket",
            "credential_exposed": False,
        }

    @router.get("/audit")
    async def list_audit(
        request: Request,
        assignment_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        actor = _operator_actor(request)
        events = service.audit_events(
            actor=actor,
            assignment_id=assignment_id,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in events],
            "count": len(events),
        }

    return router
