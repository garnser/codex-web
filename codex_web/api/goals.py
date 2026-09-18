from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.goals import (
    GoalCreate,
    GoalPriority,
    GoalStatus,
    GoalTransitionRequest,
    GoalUpdate,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.goals import (
    GoalConflictError,
    GoalError,
    GoalNotFoundError,
    GoalScopeError,
    GoalService,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, GoalNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GoalConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AuthorizationError, GoalScopeError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (GoalError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_goals_router(service: GoalService) -> APIRouter:
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

    @router.get("")
    async def list_goals(
        request: Request,
        status: GoalStatus | None = None,
        owner_identity_id: str | None = None,
        priority: GoalPriority | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        rows = service.list(
            scope=actor.tenant,
            status=status,
            owner_identity_id=owner_identity_id,
            priority=priority,
        )
        return {
            "items": [
                service.snapshot(item.id, scope=actor.tenant).model_dump(mode="json")
                for item in rows
            ],
            "count": len(rows),
        }

    @router.get("/events")
    async def events(
        request: Request,
        goal_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.events(
                scope=actor.tenant,
                goal_id=goal_id,
                limit=limit,
            )
        except (GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("")
    async def create_goal(
        payload: GoalCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            goal = service.create(
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "snapshot": service.snapshot(
                goal.id,
                scope=actor.tenant,
            ).model_dump(mode="json")
        }

    @router.get("/{goal_id}")
    async def get_goal(goal_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            snapshot = service.snapshot(goal_id, scope=actor.tenant)
        except (GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {"snapshot": snapshot.model_dump(mode="json")}

    @router.patch("/{goal_id}")
    async def revise_goal(
        goal_id: str,
        payload: GoalUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            goal = service.revise(
                goal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "snapshot": service.snapshot(
                goal.id,
                scope=actor.tenant,
            ).model_dump(mode="json")
        }

    @router.post("/{goal_id}/transition")
    async def transition_goal(
        goal_id: str,
        payload: GoalTransitionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            goal = service.transition(
                goal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "snapshot": service.snapshot(
                goal.id,
                scope=actor.tenant,
            ).model_dump(mode="json")
        }

    @router.get("/{goal_id}/revisions")
    async def revisions(goal_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.revisions(goal_id, scope=actor.tenant)
        except (GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    return router
