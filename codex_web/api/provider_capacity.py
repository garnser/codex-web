from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.api.identity import request_actor
from codex_web.services.provider_capacity import ProviderCapacityService


def build_provider_capacity_router(
    service: ProviderCapacityService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/provider-capacity",
        tags=["provider-capacity"],
    )

    @router.get("")
    async def capacity(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        records = service.list(actor)
        waits = service.list_waits(actor)
        return {
            "items": [item.model_dump(mode="json") for item in records],
            "waits": [item.model_dump(mode="json") for item in waits],
            "count": len(records),
            "waiting": sum(1 for item in waits if item.status.value == "waiting"),
        }

    return router
