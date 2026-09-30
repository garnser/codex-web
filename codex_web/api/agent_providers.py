from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.services.agent_provider_administration import AgentProviderAdministration
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


def build_agent_providers_router(service: AgentProviderService, projects=None, administration=None) -> APIRouter:
    router = APIRouter(prefix="/api/agent-providers", tags=["agent-providers"],
                      dependencies=[Depends(project_record_context_parameter)])
    administration = administration or AgentProviderAdministration(service)

    def context(request: Request):
        ProjectRecordScope.from_request(request, request_actor(request), projects)

    router.dependencies.append(Depends(context))

    @router.get("/administration")
    async def administration_catalog(request: Request):
        return administration.catalog(request_actor(request))

    @router.get("/{provider_id}/impact")
    async def provider_impact(provider_id: str, request: Request):
        try:
            return administration.impact(provider_id, request_actor(request))
        except Exception as exc:
            raise _error(exc) from exc

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
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if provider_id != payload.id:
            raise HTTPException(status_code=422, detail="provider id mismatch")
        try:
            item = service.upsert(payload, actor=request_actor(request), expected_revision=expected_revision)
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
