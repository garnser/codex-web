from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.services.runtime import RuntimeService


def build_runtime_router(service: RuntimeService) -> APIRouter:
    router = APIRouter(tags=["runtime"])

    @router.get("/api/status")
    async def status() -> dict[str, Any]:
        return await service.status()

    @router.get("/api/healthz")
    async def healthz() -> dict[str, Any]:
        return await service.healthz()

    @router.get("/api/operations")
    async def operations(window_seconds: float = 900.0) -> dict[str, Any]:
        return service.operations(window_seconds=window_seconds)

    @router.post("/api/recovery/resume")
    async def recovery_resume() -> dict[str, Any]:
        return await service.recovery_resume()

    @router.get("/api/account/rate-limits")
    async def account_rate_limits() -> dict[str, Any]:
        return await service.rate_limits()

    @router.get("/api/models")
    async def list_models(include_hidden: bool = False) -> dict[str, Any]:
        return await service.models(include_hidden=include_hidden)

    return router
