from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.agent_providers import AgentProviderCapability
from codex_web.api.identity import request_actor
from codex_web.goal_execution_bindings import (
    GoalExecutionBindingCreate,
    GoalExecutionBindingReconcile,
    GoalExecutionBindingStatus,
    GoalExecutionBindingUpdate,
)
from codex_web.goals import GoalCreate, GoalWorkGraphBinding
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.goal_execution_bindings import (
    GoalExecutionBindingConflictError,
    GoalExecutionBindingError,
    GoalExecutionBindingNotFoundError,
    GoalExecutionBindingService,
)
from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.goal_continuation import GoalContinuationService
from codex_web.services.goals import GoalError, GoalService
from codex_web.services.identity import AuthorizationError, IdentityService


class RuntimeBindingControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=4000)


class RuntimeObjectiveAttachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    work_item_refs: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


class RuntimeObjectivePromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1)
    work_item_refs: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, GoalExecutionBindingNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GoalExecutionBindingConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (GoalExecutionBindingError, GoalError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_goal_execution_bindings_router(
    service: GoalExecutionBindingService,
    agent_sessions: AgentSessionService,
    goals: GoalService,
    continuation: GoalContinuationService | None = None,
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

    @router.get("/runtime-objectives/unbound")
    async def list_unbound_runtime_objectives(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        bound_session_ids = {
            item.agent_session_id
            for item in service.list_all(scope=actor.tenant)
            if item.agent_session_id
        }
        items: list[dict[str, Any]] = []
        for session in agent_sessions.list(actor):
            if session.id in bound_session_ids:
                continue
            if (
                AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES
                not in set(session.capability_snapshot)
            ):
                continue
            result = await agent_sessions.read_objective(session.id, actor=actor)
            payload = result.payload if isinstance(result.payload, dict) else {}
            objective = payload.get("objective")
            if objective is None and isinstance(payload.get("goal"), dict):
                objective = payload["goal"].get("objective")
            if not objective:
                continue
            items.append(
                {
                    "agent_session_id": session.id,
                    "provider_id": session.provider_id,
                    "runtime_id": session.runtime_id,
                    "project_id": session.project_id,
                    "provider_native_session_id": session.provider_native_session_id,
                    "objective": objective,
                    "status": payload.get("status")
                    or (
                        payload.get("goal", {}).get("status")
                        if isinstance(payload.get("goal"), dict)
                        else None
                    ),
                    "payload": payload,
                }
            )
        return {"items": items, "count": len(items)}

    @router.post("/runtime-objectives/{session_id}/promote")
    async def promote_runtime_objective(
        session_id: str,
        payload: RuntimeObjectivePromoteRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            if any(
                item.agent_session_id == session_id
                for item in service.list_all(scope=actor.tenant)
            ):
                raise GoalExecutionBindingConflictError(
                    "agent session already has a canonical Goal execution binding"
                )
            session = agent_sessions.get(session_id, actor)
            if (
                AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES
                not in set(session.capability_snapshot)
            ):
                raise GoalExecutionBindingConflictError(
                    "agent session runtime does not support native execution objectives"
                )
            result = await agent_sessions.read_objective(session.id, actor=actor)
            objective_payload = (
                result.payload if isinstance(result.payload, dict) else {}
            )
            nested_goal = (
                objective_payload.get("goal")
                if isinstance(objective_payload.get("goal"), dict)
                else {}
            )
            objective = objective_payload.get("objective") or nested_goal.get("objective")
            if not objective:
                raise GoalExecutionBindingNotFoundError(
                    "runtime objective not found for agent session"
                )
            provider_native_objective_id = (
                objective_payload.get("id")
                or nested_goal.get("id")
                or objective_payload.get("goalId")
                or nested_goal.get("goalId")
            )
            title = payload.title or str(objective).strip().splitlines()[0][:120]
            provenance_reason = (
                f"{payload.reason}; promoted runtime objective from "
                f"{session.provider_id}/{session.runtime_id} session {session.id}"
            )
            if provider_native_objective_id:
                provenance_reason += (
                    f" objective {provider_native_objective_id}"
                )
            goal = goals.create(
                GoalCreate(
                    title=title,
                    description=str(objective),
                    owner_identity_id=actor.identity_id,
                    work_graph_bindings=(
                        GoalWorkGraphBinding(
                            project_id=session.project_id,
                            root_work_item_refs=payload.work_item_refs,
                        ),
                    ),
                ),
                scope=actor.tenant,
                actor_id=actor.identity_id,
                reason=provenance_reason,
            )
            return {
                "goal": goal.model_dump(mode="json"),
                "runtime_objective": {
                    "objective": objective,
                    "provider_native_objective_id": provider_native_objective_id,
                    "payload": objective_payload,
                },
            }
        except (
            AuthorizationError,
            GoalExecutionBindingError,
            GoalError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc

    @router.post("/runtime-objectives/{session_id}/attach/{goal_id}")
    async def attach_runtime_objective(
        session_id: str,
        goal_id: str,
        payload: RuntimeObjectiveAttachRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            if any(
                item.agent_session_id == session_id
                for item in service.list_all(scope=actor.tenant)
            ):
                raise GoalExecutionBindingConflictError(
                    "agent session already has a canonical Goal execution binding"
                )
            session = agent_sessions.get(session_id, actor)
            if (
                AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES
                not in set(session.capability_snapshot)
            ):
                raise GoalExecutionBindingConflictError(
                    "agent session runtime does not support native execution objectives"
                )
            result = await agent_sessions.read_objective(session.id, actor=actor)
            objective_payload = (
                result.payload if isinstance(result.payload, dict) else {}
            )
            nested_goal = (
                objective_payload.get("goal")
                if isinstance(objective_payload.get("goal"), dict)
                else {}
            )
            objective = objective_payload.get("objective") or nested_goal.get("objective")
            if not objective:
                raise GoalExecutionBindingNotFoundError(
                    "runtime objective not found for agent session"
                )
            provider_native_objective_id = (
                objective_payload.get("id")
                or nested_goal.get("id")
                or objective_payload.get("goalId")
                or nested_goal.get("goalId")
            )
            item = service.create(
                goal_id,
                GoalExecutionBindingCreate(
                    project_id=session.project_id,
                    work_item_refs=payload.work_item_refs,
                    provider_id=session.provider_id,
                    runtime_id=session.runtime_id,
                    agent_session_id=session.id,
                    thread_id=session.provider_native_session_id,
                    execution_owner_id=actor.identity_id,
                    provider_native_objective_id=provider_native_objective_id,
                    native_objective_supported=True,
                    capability_snapshot=tuple(
                        capability.value
                        for capability in session.capability_snapshot
                    ),
                    reason=payload.reason,
                ),
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            return {
                "item": item.model_dump(mode="json"),
                "runtime_objective": {
                    "objective": objective,
                    "payload": objective_payload,
                },
            }
        except (
            AuthorizationError,
            GoalExecutionBindingError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc

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
            if existing.status == GoalExecutionBindingStatus.UNKNOWN:
                raise GoalExecutionBindingConflictError(
                    "UNKNOWN execution binding must use explicit reconciliation"
                )
            if payload.status == GoalExecutionBindingStatus.UNKNOWN:
                raise GoalExecutionBindingConflictError(
                    "UNKNOWN execution binding status is system-managed"
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

    @router.post("/{goal_id}/execution-bindings/{binding_id}/pause")
    async def pause_binding(
        goal_id: str,
        binding_id: str,
        payload: RuntimeBindingControlRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            existing = service.get(binding_id, scope=actor.tenant)
            if existing.goal_id != goal_id:
                raise GoalExecutionBindingNotFoundError(
                    "goal execution binding not found"
                )
            if existing.status == GoalExecutionBindingStatus.ACTIVE:
                await agent_sessions.interrupt(existing.agent_session_id, actor=actor)
            item = service.operator_stop(
                binding_id,
                scope=actor.tenant,
                actor_id=actor.identity_id,
                cancelled=False,
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except (AuthorizationError, GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{goal_id}/execution-bindings/{binding_id}/resume")
    async def resume_binding(
        goal_id: str,
        binding_id: str,
        payload: RuntimeBindingControlRequest,
        request: Request,
    ) -> dict[str, Any]:
        if continuation is None:
            raise HTTPException(
                status_code=503,
                detail="Goal continuation service is unavailable",
            )
        try:
            actor = mutation_actor(request)
            existing = service.get(binding_id, scope=actor.tenant)
            if existing.goal_id != goal_id:
                raise GoalExecutionBindingNotFoundError(
                    "goal execution binding not found"
                )
            item = service.resume_operator_pause(
                binding_id,
                scope=actor.tenant,
                actor_id=actor.identity_id,
                reason=payload.reason,
            )
            result = await continuation.dispatch_once(
                item.id,
                scope=actor.tenant,
            )
            return {
                "item": service.get(item.id, scope=actor.tenant).model_dump(mode="json"),
                "continuation": result.__dict__,
            }
        except (AuthorizationError, GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc

    @router.post("/{goal_id}/execution-bindings/{binding_id}/cancel")
    async def cancel_binding(
        goal_id: str,
        binding_id: str,
        payload: RuntimeBindingControlRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            existing = service.get(binding_id, scope=actor.tenant)
            if existing.goal_id != goal_id:
                raise GoalExecutionBindingNotFoundError(
                    "goal execution binding not found"
                )
            if existing.status == GoalExecutionBindingStatus.ACTIVE:
                await agent_sessions.interrupt(existing.agent_session_id, actor=actor)
            item = service.operator_stop(
                binding_id,
                scope=actor.tenant,
                actor_id=actor.identity_id,
                cancelled=True,
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except (AuthorizationError, GoalExecutionBindingError, ValueError) as exc:
            raise _error(exc) from exc
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{goal_id}/execution-bindings/{binding_id}/reconcile")
    async def reconcile_binding(
        goal_id: str,
        binding_id: str,
        payload: GoalExecutionBindingReconcile,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            existing = service.get(binding_id, scope=actor.tenant)
            if existing.goal_id != goal_id:
                raise GoalExecutionBindingNotFoundError(
                    "goal execution binding not found"
                )
            item = service.reconcile_unknown(
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
