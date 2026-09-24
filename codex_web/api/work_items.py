from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from codex_web.models import WorkItemAckCreate, WorkItemHandoffCreate, WorkItemProgressUpdate
from codex_web.services.identity import IdentityError, IdentityService, identity_http_error
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_item_operator import WorkItemOperatorService
from codex_web.services.work_item_runs import WorkItemRunProjectionService
from codex_web.services.work_items import WorkItemService
from codex_web.services.task_source_sync_jobs import (
    GitLabSyncJobNotFound,
    GitLabSyncJobService,
)
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


def build_work_items_router(
    service: WorkItemService,
    *,
    gitlab_sync_jobs: GitLabSyncJobService | None = None,
    agent_teams: Any | None = None,
    runs: WorkItemRunProjectionService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["work-items"])
    execution = WorkItemExecutionLifecycleService(
        None,
        service.state_machine,
        dependencies=service.work_items,
    )
    if runs is not None:
        runs.work_item_execution = execution
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
        q: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await service.list(
            project_id=project_id,
            owner=owner,
            stage=stage,
            release_gate=release_gate,
            q=q,
            scope=request.state.tenant_scope,
            limit=limit,
            cursor=cursor,
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

    @router.post(
        "/api/work-items/sync-from-gitlab",
        status_code=202,
    )
    async def sync_from_gitlab(request: Request) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        if gitlab_sync_jobs is None:
            return await service.sync_from_gitlab(
                request.state.tenant_scope
            )
        job = gitlab_sync_jobs.start(
            scope=request.state.tenant_scope,
            actor_id=request.state.identity_actor.identity_id,
        )
        return {
            "ok": True,
            "accepted": True,
            "job": job.model_dump(mode="json"),
        }

    @router.get("/api/work-items/gitlab-sync")
    async def list_gitlab_sync_jobs(
        request: Request,
        limit: int = 20,
    ) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        if gitlab_sync_jobs is None:
            return {"items": [], "count": 0}
        items = gitlab_sync_jobs.list(
            scope=request.state.tenant_scope,
            limit=limit,
        )
        return {
            "items": [
                item.model_dump(mode="json")
                for item in items
            ],
            "count": len(items),
        }

    @router.get("/api/work-items/gitlab-sync/{job_id}")
    async def get_gitlab_sync_job(
        job_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        if gitlab_sync_jobs is None:
            raise HTTPException(
                status_code=404,
                detail="GitLab sync jobs are unavailable",
            )
        try:
            job = gitlab_sync_jobs.get(
                job_id,
                scope=request.state.tenant_scope,
            )
        except GitLabSyncJobNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=str(exc),
            ) from exc
        return {"job": job.model_dump(mode="json")}

    @router.delete("/api/work-items/gitlab-sync/{job_id}")
    async def cancel_gitlab_sync_job(
        job_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        if gitlab_sync_jobs is None:
            raise HTTPException(
                status_code=404,
                detail="GitLab sync jobs are unavailable",
            )
        try:
            job = gitlab_sync_jobs.cancel(
                job_id,
                scope=request.state.tenant_scope,
            )
        except GitLabSyncJobNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=str(exc),
            ) from exc
        return {"ok": True, "job": job.model_dump(mode="json")}

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

    @router.get("/api/work-items/{ref:path}/runs/{execution_id}")
    async def get_work_item_run(
        ref: str,
        execution_id: str,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        if runs is None:
            raise HTTPException(status_code=404, detail="Run projection unavailable")
        scope = request.state.tenant_scope
        return runs.get_run(
            ref,
            execution_id,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
        )

    @router.get("/api/work-items/{ref:path}/runs")
    async def list_work_item_runs(
        ref: str,
        request: Request,
        limit: int = WorkItemRunProjectionService.DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        if runs is None:
            raise HTTPException(status_code=404, detail="Run projection unavailable")
        scope = request.state.tenant_scope
        return runs.list_runs(
            ref,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            limit=limit,
            cursor=cursor,
        )

    # Static-suffix work-item routes must be registered before the catch-all
    # {ref:path} route so refs containing slashes remain unambiguous.
    @router.get("/api/work-items/{ref:path}/history")
    async def get_work_item_history(ref: str, request: Request, limit: int = 100) -> dict[str, Any]:
        require_item_scope(ref, request)
        payload = execution.history(ref, limit=limit)
        if agent_teams is not None:
            payload["teamDelegations"] = agent_teams.work_item_history(
                ref,
                actor=request.state.identity_actor,
            )
        return payload

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

    @router.get("/api/work-items/{ref:path}/continuation-snapshot")
    async def get_work_item_continuation_snapshot(
        ref: str,
        request: Request,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.continuation_snapshot(ref)

    @router.get("/api/work-items/{ref:path}/continuation-delta")
    async def get_work_item_continuation_delta(
        ref: str,
        request: Request,
        max_events: int = 100,
        event_offset: int = 0,
    ) -> dict[str, Any]:
        require_item_scope(ref, request)
        return execution.continuation_delta(
            ref,
            max_events=max_events,
            event_offset=event_offset,
        )

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
