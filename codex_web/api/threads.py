from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.models import (
    ThreadPrimaryChannelUpdate,
    ThreadPrimaryUpdate,
    ThreadRename,
    ThreadRunSettings,
)
from codex_web.services.threads import ThreadService


def build_threads_router(service: ThreadService) -> APIRouter:
    router = APIRouter(tags=["threads"])

    @router.get("/api/threads")
    async def list_threads(
        project_id: str | None = None,
        archived: bool = False,
        search: str | None = None,
    ) -> dict[str, Any]:
        return await service.list(project_id, archived, search)

    @router.post("/api/threads")
    async def create_thread(
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        return await service.create(
            project_id=project_id,
            sandbox=sandbox,
            approval_policy=approval_policy,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    @router.get("/api/threads/{thread_id}")
    async def read_thread(
        thread_id: str,
        message_limit: int | None = None,
        turn_limit: int | None = None,
    ) -> dict[str, Any]:
        return await service.read(
            thread_id,
            message_limit=message_limit,
            turn_limit=turn_limit,
        )

    @router.post("/api/threads/{thread_id}/name")
    async def rename_thread(thread_id: str, payload: ThreadRename) -> dict[str, Any]:
        return await service.rename(thread_id, payload.name)

    @router.post("/api/threads/{thread_id}/settings")
    async def update_thread_settings(thread_id: str, payload: ThreadRunSettings) -> dict[str, Any]:
        return service.update_settings(thread_id, payload)

    @router.get("/api/thread-settings")
    async def list_thread_settings() -> dict[str, Any]:
        return service.list_settings()

    @router.get("/api/threads/{thread_id}/settings")
    async def get_thread_settings(thread_id: str) -> dict[str, Any]:
        return service.get_settings(thread_id)

    @router.post("/api/threads/{thread_id}/primary")
    async def update_thread_primary(thread_id: str, payload: ThreadPrimaryUpdate) -> dict[str, Any]:
        return await service.update_primary(thread_id, payload)

    @router.post("/api/threads/{thread_id}/primary-channel")
    async def update_thread_primary_channel(
        thread_id: str,
        payload: ThreadPrimaryChannelUpdate,
    ) -> dict[str, Any]:
        return await service.update_primary_channel(thread_id, payload)

    @router.post("/api/threads/{thread_id}/archive")
    async def archive_thread(thread_id: str) -> dict[str, Any]:
        return await service.archive(thread_id)

    @router.post("/api/threads/{thread_id}/unarchive")
    async def unarchive_thread(thread_id: str) -> dict[str, Any]:
        return await service.unarchive(thread_id)

    @router.post("/api/turns/interrupt")
    async def interrupt_turn(thread_id: str) -> dict[str, Any]:
        return await service.interrupt(thread_id)

    return router
