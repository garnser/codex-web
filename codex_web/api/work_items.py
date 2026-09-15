from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from codex_web.models import WorkItemAckCreate, WorkItemHandoffCreate, WorkItemProgressUpdate
from codex_web.services.work_items import WorkItemService


def build_work_items_router(service: WorkItemService) -> APIRouter:
    router = APIRouter(tags=["work-items"])

    @router.get("/api/work-items")
    async def list_work_items(
        project_id: str | None = None,
        owner: str | None = None,
        stage: str | None = None,
        release_gate: bool | None = None,
    ) -> dict[str, Any]:
        return await service.list(
            project_id=project_id,
            owner=owner,
            stage=stage,
            release_gate=release_gate,
        )

    @router.post("/api/work-items/sync-from-gitlab")
    async def sync_from_gitlab() -> dict[str, Any]:
        return await service.sync_from_gitlab()

    @router.get("/api/work-items/{ref:path}")
    async def get_work_item(ref: str) -> dict[str, Any]:
        return await service.get(ref)

    @router.post("/api/work-items/{ref:path}/handoff")
    async def create_handoff(ref: str, payload: WorkItemHandoffCreate) -> dict[str, Any]:
        return await service.handoff(ref, payload)

    @router.post("/api/work-items/{ref:path}/ack")
    async def acknowledge_handoff(ref: str, payload: WorkItemAckCreate) -> dict[str, Any]:
        return await service.acknowledge(ref, payload)

    @router.post("/api/work-items/{ref:path}/progress")
    async def update_progress(ref: str, payload: WorkItemProgressUpdate) -> dict[str, Any]:
        return await service.progress(ref, payload)

    return router
