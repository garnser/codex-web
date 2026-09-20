from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from codex_web.models import WorkItemAckCreate, WorkItemHandoffCreate, WorkItemProgressUpdate
from codex_web.services.identity import IdentityError, IdentityService, identity_http_error
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_item_operator import WorkItemOperatorService
from codex_web.services.work_items import WorkItemService
from codex_web.work_item_execution_models import (
    WorkItemCheckpointCreate,
    WorkItemExecutionUpdate,
    WorkItemUsageRecord,
)


class WorkItemCommentCreate(BaseModel):
    body: str = Field(min_length=1)


class WorkItemOperatorAction(BaseModel):
    actor: str | None = None
    reason: str | None = None


def build_work_items_router(service: WorkItemService) -> APIRouter:
    router = APIRouter(tags=["work-items"])
    execution = WorkItemExecutionLifecycleService(
        None,
        service.state_machine,
        dependencies=service.work_items,
    )
    operator = WorkItemOperatorService(service)

    def require_item_scope(ref: str, request: Request) -> None:
        state = service.state_machine._work_item_state(ref)
        scope = request.state.tenant_scope
        if (
            state.organization_id != scope.organization_id
            or state.workspace_id != scope.workspace_id
        ):
            # Do not reveal cross-tenant object existence.
            raise HTTPException(status_code=404, detail="Work item not found")

    def require_project_scope(project_id: str, request: Request) -> None:
        scope = request.state.tenant_scope
        project = next(
            (
                project
                for project in service.work_items.load_projects()
                if project.id == project_id
                and project.organization_id == scope.organization_id
                and project.workspace_id == scope.workspace_id
            ),
            None,
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

    @router.get("/api/work-items")
    async def list_work_items(
        request: Request,
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
            scope=request.state.tenant_scope,
        )

    @router.get("/api/task-sources")
    async def list_task_sources(request: Request) -> dict[str, Any]:
        scope = request.state.tenant_scope
        allowed = {
            project.id
            for project in service.work_items.load_projects()
            if project.organization_id == scope.organization_id
            and project.workspace_id == scope.workspace_id
        }
        payload = operator.task_source_catalog()
        return {
            **payload,
            "items": [
                item
                for item in payload.get("items", [])
                if item.get("project_id") is None or item.get("project_id") in allowed
            ],
        }

    @router.post("/api/work-items/sync-from-gitlab")
    async def sync_from_gitlab(request: Request) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        return await service.sync_from_gitlab(request.state.tenant_scope)

    @router.post("/api/work-items/sync/{project_id}")
    async def sync_authoritative_task_source(
        project_id: str,
        payload: WorkItemOperatorAction,
        request: Request,
    ) -> dict[str, Any]:
        require_project_scope(project_id, request)
        try:
            IdentityService.require_admin(request.state.identity_actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        return await operator.sync_project(
            project_id,
            actor=payload.actor,
            reason=payload.reason,
        )

    # Static-suffix work-item routes must be registered before the catch-all
    # {ref:path} route so refs containing slashes remain unambiguous.
    @router.get("/api/work-items/{ref:path}/history")
    async def get_work_item_history(ref: str, request: Request, limit: int = 100) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.history(ref, limit=limit)

    @router.get("/api/work-items/{ref:path}/execution")
    async def get_work_item_execution(ref: str, request: Request) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.execution(ref)

    @router.patch("/api/work-items/{ref:path}/execution")
    async def update_work_item_execution(
        ref: str,
        payload: WorkItemExecutionUpdate,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.update(ref, payload)

    @router.post("/api/work-items/{ref:path}/checkpoints")
    async def create_work_item_checkpoint(
        ref: str,
        payload: WorkItemCheckpointCreate,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.checkpoint(ref, payload)

    @router.post("/api/work-items/{ref:path}/usage")
    async def record_work_item_usage(
        ref: str,
        payload: WorkItemUsageRecord,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.record_usage(ref, payload)

    @router.get("/api/work-items/{ref:path}/operator")
    async def get_work_item_operator_detail(ref: str, request: Request) -> dict[str, Any]:
        require_item_scope(ref, request)
        return operator.detail(ref)

    @router.post("/api/work-items/{ref:path}/retry")
    async def retry_work_item(
        ref: str,
        payload: WorkItemOperatorAction,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await operator.retry(ref, actor=payload.actor, reason=payload.reason)

    @router.post("/api/work-items/{ref:path}/reconcile")
    async def reconcile_work_item(
        ref: str,
        payload: WorkItemOperatorAction,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await operator.reconcile(ref, actor=payload.actor, reason=payload.reason)

    @router.get("/api/work-items/{ref:path}")
    async def get_work_item(ref: str, request: Request) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await service.get(ref)

    @router.post("/api/work-items/{ref:path}/comment")
    async def add_work_item_comment(
        ref: str,
        payload: WorkItemCommentCreate,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await service.comment(ref, payload.body)

    @router.post("/api/work-items/{ref:path}/handoff")
    async def create_handoff(
        ref: str,
        payload: WorkItemHandoffCreate,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await service.handoff(ref, payload)

    @router.post("/api/work-items/{ref:path}/ack")
    async def acknowledge_handoff(
        ref: str,
        payload: WorkItemAckCreate,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await service.acknowledge(ref, payload)

    @router.post("/api/work-items/{ref:path}/progress")
    async def update_progress(
        ref: str,
        payload: WorkItemProgressUpdate,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return await service.progress(ref, payload)

    return router
