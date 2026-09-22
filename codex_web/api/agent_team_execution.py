from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.agent_teams import (
    TeamDecisionExecutionRequest,
    TeamExecutionRequest,
    TeamMemberResultEvent,
)
from codex_web.api.identity import request_actor
from codex_web.services.agent_team_execution import AgentTeamExecutionService
from codex_web.services.agent_teams import (
    AgentTeamConflict,
    AgentTeamError,
    AgentTeamNotFound,
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
    if isinstance(exc, HTTPException):
        return exc
    return HTTPException(status_code=503, detail=str(exc))


def build_agent_team_execution_router(
    service: AgentTeamExecutionService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-teams",
        tags=["agent-teams"],
    )

    @router.post("/{team_id}/execute")
    async def execute(
        team_id: str,
        payload: TeamExecutionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = await service.execute(
                team_id,
                payload,
                actor=request_actor(request),
            )
            return {"result": result.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{team_id}/execute-decision")
    async def execute_decision(
        team_id: str,
        payload: TeamDecisionExecutionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = await service.execute_decision(
                team_id,
                payload,
                actor=request_actor(request),
            )
            return {"result": result.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{team_id}/member-results")
    async def member_result(
        team_id: str,
        payload: TeamMemberResultEvent,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = await service.member_result(
                team_id,
                payload,
                actor=request_actor(request),
            )
            return {"result": result.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    return router
