from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.goal_execution_bindings import (
    GoalExecutionBindingCreate,
    GoalExecutionBindingUpdate,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.goal_execution_bindings import (
    GoalExecutionBindingConflictError,
    GoalExecutionBindingError,
    GoalExecutionBindingNotFoundError,
    GoalExecutionBindingService,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, GoalExecutionBindingNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GoalExecutionBindingConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (GoalExecutionBindingError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_goal_execution_bindings_router(
    service: GoalExecutionBindingService,
) -> APIRouter:
    router = APIRouter(prefix="/api/goals", tags=["goals"])

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "goals:admin" not in actor.service_scopes:
                raise AuthorizationError("goals:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/{goal_id}/execution-bindings")
    async def list_bindings(goal_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.list(goal_id, scope=actor.tenant)
            return {
                "items": [item.model_dump(mode="json") for item in rows],
                "count": len(rows),
            }
        except (GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc

    @router.post("/{goal_id}/execution-bindings")
    async def create_binding(
        goal_id: str,
        payload: GoalExecutionBindingCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            item = service.create(
                goal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            return {"item": item.model_dump(mode="json")}
        except (AuthorizationError, GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc

    @router.patch("/{goal_id}/execution-bindings/{binding_id}")
    async def update_binding(
        goal_id: str,
        binding_id: str,
        payload: GoalExecutionBindingUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            existing = service.get(binding_id, scope=actor.tenant)
            if existing.goal_id != goal_id:
                raise GoalExecutionBindingNotFoundError(
                    "goal execution binding not found"
                )
            item = service.update(
                binding_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            return {"item": item.model_dump(mode="json")}
        except (AuthorizationError, GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc

    @router.get("/{goal_id}/execution-binding-events")
    async def binding_events(goal_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.events(goal_id, scope=actor.tenant)
            return {
                "items": [item.model_dump(mode="json") for item in rows],
                "count": len(rows),
            }
        except (GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc

    return router
