from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.agent_routing import AgentRoutingRequest
from codex_web.api.identity import request_actor
from codex_web.services.agent_routing import AgentRoutingError, AgentRoutingService
from codex_web.services.model_gateway import ModelRoutingError


def build_agent_routing_router(service: AgentRoutingService) -> APIRouter:
    router = APIRouter(prefix="/api/agent-routing", tags=["agent-routing"])

    @router.post("/route")
    async def route(
        payload: AgentRoutingRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = await service.route(payload, actor=request_actor(request))
        except (AgentRoutingError, ModelRoutingError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"route": result.model_dump(mode="json")}

    return router
