from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.models import BotBindingCreate, BotConnectionCreate, BotInboundMessage
from codex_web.services.bots import BotService


def build_bots_router(service: BotService) -> APIRouter:
    router = APIRouter(tags=["bots"])

    @router.get("/api/bots")
    async def status() -> dict[str, Any]:
        return service.status()

    @router.get("/api/bots/connections")
    async def list_connections() -> list[dict[str, Any]]:
        return service.list_connections()

    @router.post("/api/bots/connections")
    async def save_connection(payload: BotConnectionCreate) -> dict[str, Any]:
        return await service.save_connection(payload)

    @router.get("/api/bots/bindings")
    async def list_bindings() -> list[dict[str, Any]]:
        return service.list_bindings()

    @router.get("/api/bots/channels")
    async def list_channels(project_id: str = "home") -> list[dict[str, str]]:
        return await service.list_channels(project_id)

    @router.post("/api/bots/bindings")
    async def create_binding(payload: BotBindingCreate) -> dict[str, Any]:
        return await service.create_binding(payload)

    @router.post("/api/bots/inbound")
    async def inbound(payload: BotInboundMessage) -> dict[str, Any]:
        return await service.inbound(payload)

    return router
