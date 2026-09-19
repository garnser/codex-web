from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from codex_web.api.identity import request_actor
from codex_web.services.conversation_channels import ConversationChannelService


def build_conversation_channels_router(
    service: ConversationChannelService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/conversation-channels",
        tags=["conversation-channels"],
    )

    @router.get("/providers")
    async def providers() -> dict[str, Any]:
        rows = service.provider_capabilities()
        return {
            "items": list(rows),
            "count": len(rows),
        }

    @router.get("/messages")
    async def messages(
        request: Request,
        provider_type: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        rows = service.message_states(
            actor=actor,
            provider_type=provider_type,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.get("/receipts")
    async def receipts(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        rows = service.receipts(actor=actor, limit=limit)
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    return router
