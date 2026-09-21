from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService

from codex_web.services.runtime import RuntimeService


def build_runtime_router(service: RuntimeService) -> APIRouter:
    router = APIRouter(tags=["runtime"])

    def require_runtime_reader(request: Request) -> None:
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("runtime:read", "runtime:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="runtime:read or runtime:admin service scope required",
                )
            return
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def require_runtime_admin(request: Request) -> None:
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "runtime:admin" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="runtime:admin service scope required",
                )
            return
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/api/status")
    async def status() -> dict[str, Any]:
        return await service.status()

    @router.get("/api/livez")
    async def livez() -> dict[str, Any]:
        return await service.livez()

    @router.get("/api/readyz")
    async def readyz() -> dict[str, Any]:
        return await service.readyz()

    @router.get("/api/healthz")
    async def healthz() -> dict[str, Any]:
        return await service.healthz()

    @router.get("/api/operations")
    async def operations(
        request: Request,
        window_seconds: float = 900.0,
    ) -> dict[str, Any]:
        require_runtime_reader(request)
        return service.operations(window_seconds=window_seconds)

    @router.get("/api/operations/distributed")
    async def distributed_operations(
        request: Request,
    ) -> dict[str, Any]:
        require_runtime_reader(request)
        return await service.distributed_status()

    @router.post("/api/recovery/resume")
    async def recovery_resume(request: Request) -> dict[str, Any]:
        require_runtime_admin(request)
        return await service.recovery_resume()

    @router.get("/api/account/rate-limits")
    async def account_rate_limits() -> dict[str, Any]:
        return await service.rate_limits()

    @router.get("/api/models")
    async def list_models(include_hidden: bool = False) -> dict[str, Any]:
        return await service.models(include_hidden=include_hidden)

    return router
