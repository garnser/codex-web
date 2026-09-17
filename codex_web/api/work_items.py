from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from codex_web.models import WorkItemAckCreate, WorkItemHandoffCreate, WorkItemProgressUpdate
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_items import WorkItemService
from codex_web.work_item_execution_models import (
    WorkItemCheckpointCreate,
    WorkItemExecutionUpdate,
    WorkItemUsageRecord,
)


class WorkItemCommentCreate(BaseModel):
    body: str = Field(min_length=1)


def build_work_items_router(service: WorkItemService) -> APIRouter:
    router = APIRouter(tags=["work-items"])
    execution = WorkItemExecutionLifecycleService(service.host, service.state_machine)

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

    # Static-suffix work-item routes must be registered before the catch-all
    # {ref:path} route so refs containing slashes remain unambiguous.
    @router.get("/api/work-items/{ref:path}/history")
    async def get_work_item_history(ref: str, limit: int = 100) -> dict[str, Any]:
        return execution.history(ref, limit=limit)

    @router.get("/api/work-items/{ref:path}/execution")
    async def get_work_item_execution(ref: str) -> dict[str, Any]:
        return execution.execution(ref)

    @router.patch("/api/work-items/{ref:path}/execution")
    async def update_work_item_execution(
        ref: str,
        payload: WorkItemExecutionUpdate,
    ) -> dict[str, Any]:
        return execution.update(ref, payload)

    @router.post("/api/work-items/{ref:path}/checkpoints")
    async def create_work_item_checkpoint(
        ref: str,
        payload: WorkItemCheckpointCreate,
    ) -> dict[str, Any]:
        return execution.checkpoint(ref, payload)

    @router.post("/api/work-items/{ref:path}/usage")
    async def record_work_item_usage(
        ref: str,
        payload: WorkItemUsageRecord,
    ) -> dict[str, Any]:
        return execution.record_usage(ref, payload)

    @router.get("/api/work-items/{ref:path}")
    async def get_work_item(ref: str) -> dict[str, Any]:
        return await service.get(ref)

    @router.post("/api/work-items/{ref:path}/comment")
    async def add_work_item_comment(ref: str, payload: WorkItemCommentCreate) -> dict[str, Any]:
        return await service.comment(ref, payload.body)

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
