from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from codex_web.api.thread_scope import thread_scope_dependency
from codex_web.services.thread_scope import ThreadScopeService

from codex_web.services.context import ContextCompactionService


def build_context_router(service: ContextCompactionService, scope: ThreadScopeService) -> APIRouter:
    router = APIRouter(tags=["context"], dependencies=[Depends(thread_scope_dependency(scope))])

    @router.get("/api/threads/{thread_id}/context")
    async def context_status(thread_id: str) -> dict[str, Any]:
        return service.status(thread_id)

    @router.post("/api/threads/{thread_id}/compact")
    async def compact_context(thread_id: str) -> dict[str, Any]:
        return await service.compact(thread_id, reason="manual")

    return router
