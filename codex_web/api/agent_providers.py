from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from codex_web.agent_providers import (
    AgentProviderCapability,
    AgentProviderUpsert,
)
from codex_web.api.identity import request_actor
from codex_web.services.agent_providers import (
    AgentProviderConflictError,
    AgentProviderError,
    AgentProviderService,
)
from codex_web.services.identity import AuthorizationError


class AgentProviderDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_capabilities: tuple[AgentProviderCapability, ...] = ()


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AgentProviderConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AgentProviderError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_agent_providers_router(service: AgentProviderService) -> APIRouter:
    router = APIRouter(prefix="/api/agent-providers", tags=["agent-providers"])

    @router.get("")
    async def list_providers(request: Request) -> dict[str, Any]:
        items = service.list(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/{provider_id}")
    async def get_provider(provider_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get(provider_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.put("/{provider_id}")
    async def put_provider(
        provider_id: str,
        payload: AgentProviderUpsert,
        request: Request,
    ) -> dict[str, Any]:
        if provider_id != payload.id:
            raise HTTPException(status_code=422, detail="provider id mismatch")
        try:
            item = service.upsert(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/discover")
    async def discover(
        payload: AgentProviderDiscoveryRequest,
        request: Request,
    ) -> dict[str, Any]:
        items = service.discover(
            request_actor(request),
            required_capabilities=payload.required_capabilities,
        )
        return {"items": [item.model_dump(mode="json") for item in items]}

    return router
