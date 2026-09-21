from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.agent_teams import (
    AgentTeamCreate,
    AgentTeamLifecycle,
    AgentTeamLifecycleChange,
    AgentTeamUpdate,
    TeamCoordinatorDecision,
    TeamDelegationRequest,
)
from codex_web.api.identity import request_actor
from codex_web.services.agent_teams import (
    AgentTeamConflict,
    AgentTeamError,
    AgentTeamNotFound,
    AgentTeamService,
)
from codex_web.services.identity import AuthorizationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AgentTeamNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AgentTeamConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AgentTeamError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_agent_teams_router(service: AgentTeamService) -> APIRouter:
    router = APIRouter(prefix="/api/agent-teams", tags=["agent-teams"])

    @router.get("")
    async def list_teams(
        request: Request,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        try:
            items = service.list(
                actor=request_actor(request),
                include_archived=include_archived,
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("")
    async def create_team(
        payload: AgentTeamCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{team_id}")
    async def get_team(team_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get(team_id, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{team_id}/revisions")
    async def revisions(team_id: str, request: Request) -> dict[str, Any]:
        try:
            items = service.revisions(team_id, actor=request_actor(request))
            return {
                "items": [item.model_dump(mode="json") for item in items],
                "count": len(items),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.patch("/{team_id}")
    async def update_team(
        team_id: str,
        payload: AgentTeamUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update(team_id, payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    async def change(
        team_id: str,
        lifecycle: AgentTeamLifecycle,
        payload: AgentTeamLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.lifecycle(
                team_id,
                lifecycle,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{team_id}/disable")
    async def disable(
        team_id: str,
        payload: AgentTeamLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change(
            team_id,
            AgentTeamLifecycle.DISABLED,
            payload,
            request,
        )

    @router.post("/{team_id}/archive")
    async def archive(
        team_id: str,
        payload: AgentTeamLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change(
            team_id,
            AgentTeamLifecycle.ARCHIVED,
            payload,
            request,
        )

    @router.post("/{team_id}/restore")
    async def restore(
        team_id: str,
        payload: AgentTeamLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change(
            team_id,
            AgentTeamLifecycle.ACTIVE,
            payload,
            request,
        )

    @router.post("/{team_id}/plan")
    async def plan(
        team_id: str,
        payload: TeamDelegationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.plan(team_id, payload, actor=request_actor(request))
            return {"plan": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{team_id}/decisions")
    async def decision(
        team_id: str,
        payload: TeamCoordinatorDecision,
        delegation: TeamDelegationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.apply_coordinator_decision(
                team_id,
                delegation,
                payload,
                actor=request_actor(request),
            )
            return {"plan": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    return router
