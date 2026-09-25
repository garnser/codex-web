from __future__ import annotations

import asyncio

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.autonomy import AutonomyExclusiveGoalScope
from codex_web.goals import (
    GoalCompletionEvaluationRequest,
    GoalCreate,
    GoalPriority,
    GoalStatus,
    GoalTransitionRequest,
    GoalUpdate,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.goals import (
    GoalConflictError,
    GoalError,
    GoalNotFoundError,
    GoalScopeError,
    GoalService,
)
from codex_web.services.identity import AuthorizationError, IdentityService


class ExclusiveContinuationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=4000)


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


def build_goals_router(
    service: GoalService,
    autonomy: AutonomyController | None = None,
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

    @router.get("")
    async def list_goals(
        request: Request,
        status: GoalStatus | None = None,
        owner_identity_id: str | None = None,
        priority: GoalPriority | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)

        def load():
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

        return await asyncio.to_thread(load)

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

    @router.get("/by-work-item")
    async def goals_by_work_item(
        request: Request,
        ref: str = Query(min_length=1),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            def load():
                rows = service.goals_for_work_item(ref, scope=actor.tenant)
                return {
                    "work_item_ref": ref,
                    "items": [
                        service.snapshot(item.id, scope=actor.tenant).model_dump(mode="json")
                        for item in rows
                    ],
                    "count": len(rows),
                }

            return await asyncio.to_thread(load)
        except (GoalError, ValueError) as exc:
            raise _error(exc) from exc

    @router.post("")
    async def create_goal(
        payload: GoalCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            goal = await asyncio.to_thread(
                service.create,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            snapshot = await asyncio.to_thread(
                service.snapshot,
                goal.id,
                scope=actor.tenant,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {"snapshot": snapshot.model_dump(mode="json")}

    @router.put("/{goal_id}/exclusive-continuation")
    async def set_exclusive_continuation(
        goal_id: str,
        payload: ExclusiveContinuationRequest,
        request: Request,
    ) -> dict[str, Any]:
        if autonomy is None:
            raise HTTPException(
                status_code=503,
                detail="autonomy controller is unavailable",
            )
        try:
            actor = mutation_actor(request)
            goal = service.get(goal_id, scope=actor.tenant)
            binding = next(
                (
                    item
                    for item in goal.work_graph_bindings
                    if item.project_id == payload.project_id
                ),
                None,
            )
            if binding is None:
                raise GoalScopeError(
                    "exclusive continuation project is outside the canonical Goal Work Graph scope"
                )
            control = autonomy.set_exclusive_goal_scope(
                AutonomyExclusiveGoalScope(
                    goal_id=goal.id,
                    project_id=payload.project_id,
                    root_work_item_refs=binding.root_work_item_refs,
                    reason=payload.reason,
                ),
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "control": control.model_dump(mode="json"),
            "goal_revision": goal.revision,
        }

    @router.delete("/{goal_id}/exclusive-continuation")
    async def clear_exclusive_continuation(
        goal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if autonomy is None:
            raise HTTPException(
                status_code=503,
                detail="autonomy controller is unavailable",
            )
        try:
            actor = mutation_actor(request)
        except AuthorizationError as exc:
            raise _error(exc) from exc
        current = autonomy.store.load().control.exclusive_goal_scope
        if current is None or current.goal_id != goal_id:
            raise HTTPException(
                status_code=404,
                detail="exclusive Goal continuation scope not found",
            )
        control = autonomy.clear_exclusive_goal_scope(
            actor_id=actor.identity_id,
        )
        return {"control": control.model_dump(mode="json")}

    @router.get("/{goal_id}")
    async def get_goal(goal_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            snapshot = await asyncio.to_thread(
                service.snapshot,
                goal_id,
                scope=actor.tenant,
            )
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

    @router.get("/{goal_id}/completion-evaluation")
    async def current_completion_evaluation(
        goal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.completion_evaluation(goal_id, scope=actor.tenant)
        except (GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "item": item.model_dump(mode="json") if item is not None else None,
        }

    @router.get("/{goal_id}/completion-evaluations")
    async def completion_evaluations(
        goal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.completion_evaluations(goal_id, scope=actor.tenant)
        except (GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/{goal_id}/completion-evaluations")
    async def evaluate_completion(
        goal_id: str,
        payload: GoalCompletionEvaluationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            item = service.evaluate_completion(
                goal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{goal_id}/transition")
    async def transition_goal(
        goal_id: str,
        payload: GoalTransitionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            goal = await asyncio.to_thread(
                service.transition,
                goal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            snapshot = await asyncio.to_thread(
                service.snapshot,
                goal.id,
                scope=actor.tenant,
            )
        except (AuthorizationError, GoalError, ValueError) as exc:
            raise _error(exc) from exc
        return {"snapshot": snapshot.model_dump(mode="json")}

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
