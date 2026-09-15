from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.models import TurnCreate
from codex_web.services.turns import TurnService


def build_turns_router(service: TurnService) -> APIRouter:
    router = APIRouter(tags=["turns"])

    @router.post("/api/threads/{thread_id}/resume")
    async def resume_thread(
        thread_id: str,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        force_resume: bool = False,
    ) -> dict[str, Any]:
        return await service.resume(
            thread_id,
            project_id=project_id,
            sandbox=sandbox,
            approval_policy=approval_policy,
            model=model,
            reasoning_effort=reasoning_effort,
            force_resume=force_resume,
        )

    @router.post("/api/threads/{thread_id}/replace")
    async def replace_thread(thread_id: str) -> dict[str, Any]:
        return await service.replace(thread_id)

    @router.post("/api/threads/{thread_id}/turns")
    async def start_turn(thread_id: str, payload: TurnCreate) -> dict[str, Any]:
        return await service.start(thread_id, payload)

    @router.get("/api/threads/{thread_id}/queue")
    async def thread_queue(thread_id: str) -> dict[str, Any]:
        return service.queue(thread_id)

    @router.post("/api/threads/{thread_id}/queue/steer")
    async def steer_latest(thread_id: str) -> dict[str, Any]:
        return await service.steer_latest(thread_id)

    @router.post("/api/threads/{thread_id}/queue/{queued_id}/steer")
    async def steer_specific(thread_id: str, queued_id: str) -> dict[str, Any]:
        return await service.steer(thread_id, queued_id)

    return router
