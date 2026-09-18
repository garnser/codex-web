from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.action_providers import (
    ActionBindingNotFoundError,
    ActionExecutionService,
    ActionProviderError,
    ActionProviderNotFoundError,
    ActionProviderRegistry,
    ActionRequirementError,
    ActionResolutionError,
)
from codex_web.services.identity import AuthorizationError, IdentityService, TenantIsolationError
from codex_web.services.resources import ResourceNotFoundError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ActionBindingNotFoundError, ResourceNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (ActionResolutionError, ActionRequirementError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ActionProviderNotFoundError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ActionProviderError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_action_providers_router(
    registry: ActionProviderRegistry,
    execution: ActionExecutionService,
) -> APIRouter:
    router = APIRouter(tags=["action-providers"])

    @router.get("/api/action-providers")
    async def action_provider_catalog(request: Request) -> dict[str, Any]:
        return {"items": registry.catalog(request_actor(request))}

    @router.get("/api/action-providers/bindings")
    async def list_bindings(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in registry.list_bindings(actor)
            ]
        }

    @router.post("/api/action-providers/bindings")
    async def create_binding(
        payload: ActionProviderBindingCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            if actor.principal_kind != PrincipalKind.SERVICE:
                IdentityService.require_admin(actor)
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            binding = registry.bind(
                payload,
                actor=actor,
                resources=execution.resources,
            )
            return {"item": binding.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ActionProviderError,
                    AuthorizationError,
                    TenantIsolationError,
                    ResourceNotFoundError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/action-providers/bindings/{binding_id}")
    async def get_binding(binding_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            binding = registry.binding(binding_id, actor)
            provider = registry.provider(
                binding.provider_type,
                binding.provider_instance,
            )
            return {
                "binding": binding.model_dump(mode="json"),
                "actions": [
                    item.model_dump(mode="json")
                    for item in provider.actions()
                ],
            }
        except Exception as exc:
            if isinstance(
                exc,
                (ActionProviderError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-providers/bindings/{binding_id}/prepare")
    async def prepare_action(
        binding_id: str,
        payload: ActionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            preparation = await execution.prepare(
                binding_id,
                payload,
                actor=request_actor(request),
            )
            return preparation.model_dump(mode="json")
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ActionProviderError,
                    AuthorizationError,
                    TenantIsolationError,
                    ResourceNotFoundError,
                    RuntimeError,
                ),
            ):
                raise _error(exc) from exc
            raise

    return router
