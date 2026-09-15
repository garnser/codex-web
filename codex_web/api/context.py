from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.services.context import ContextCompactionService


def build_context_router(service: ContextCompactionService) -> APIRouter:
    router = APIRouter(tags=["context"])

    @router.get("/api/threads/{thread_id}/context")
    async def context_status(thread_id: str) -> dict[str, Any]:
        return service.status(thread_id)

    @router.post("/api/threads/{thread_id}/compact")
    async def compact_context(thread_id: str) -> dict[str, Any]:
        return await service.compact(thread_id, reason="manual")

    return router
