from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.agent_teams import (
    AgentTeamAssignmentRequest,
    AgentTeamCoordinatorDecision,
    AgentTeamCreate,
    AgentTeamLifecycle,
    AgentTeamLifecycleChange,
    AgentTeamUpdate,
    AgentTeamUsageUpdate,
)
from codex_web.api.identity import request_actor
from codex_web.services.agent_teams import (
    AgentTeamAccessDenied,
    AgentTeamBudgetExceeded,
    AgentTeamConflict,
    AgentTeamError,
    AgentTeamNotFound,
    AgentTeamService,
    AgentTeamStaleDecision,
)
from codex_web.services.identity import AuthorizationError


class AgentTeamMemberResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    member_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    execution_id: str | None = Field(default=None, min_length=1)
    succeeded: bool


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AgentTeamNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AgentTeamAccessDenied, AuthorizationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(
        exc,
        (
            AgentTeamBudgetExceeded,
            AgentTeamStaleDecision,
            AgentTeamConflict,
        ),
    ):
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
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("")
    async def create_team(
        payload: AgentTeamCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.create(
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/delegations")
    async def delegation_history(
        request: Request,
        team_id: str | None = None,
        work_item_ref: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            items = service.history(
                actor=request_actor(request),
                team_id=team_id,
                work_item_ref=work_item_ref,
                limit=limit,
            )
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/delegations/{delegation_id}/coordinator-decision")
    async def coordinator_decision(
        delegation_id: str,
        payload: AgentTeamCoordinatorDecision,
        request: Request,
    ) -> dict[str, Any]:
        try:
            if payload.delegation_id != delegation_id:
                raise AgentTeamConflict(
                    "delegation ID does not match request path"
                )
            return {
                "item": await service.submit_coordinator_decision(
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/delegations/{delegation_id}/member-result")
    async def member_result(
        delegation_id: str,
        payload: AgentTeamMemberResultRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": await service.record_member_result(
                    delegation_id,
                    member_id=payload.member_id,
                    event_id=payload.event_id,
                    succeeded=payload.succeeded,
                    actor=request_actor(request),
                    execution_id=payload.execution_id,
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/delegations/{delegation_id}/usage")
    async def record_usage(
        delegation_id: str,
        payload: AgentTeamUsageUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.usage(
                    delegation_id,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{team_id}")
    async def get_team(
        team_id: str,
        request: Request,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.get(
                    team_id,
                    actor=request_actor(request),
                    revision=revision,
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{team_id}/revisions")
    async def revisions(
        team_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            items = service.revisions(
                team_id,
                actor=request_actor(request),
            )
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _error(exc) from exc

    @router.patch("/{team_id}")
    async def update_team(
        team_id: str,
        payload: AgentTeamUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.update(
                    team_id,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    async def change_lifecycle(
        team_id: str,
        lifecycle: AgentTeamLifecycle,
        payload: AgentTeamLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.lifecycle(
                    team_id,
                    lifecycle,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{team_id}/disable")
    async def disable(
        team_id: str,
        payload: AgentTeamLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change_lifecycle(
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
        return await change_lifecycle(
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
        return await change_lifecycle(
            team_id,
            AgentTeamLifecycle.ACTIVE,
            payload,
            request,
        )

    @router.post("/{team_id}/assign")
    async def assign(
        team_id: str,
        payload: AgentTeamAssignmentRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": await service.assign(
                    team_id,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    return router
