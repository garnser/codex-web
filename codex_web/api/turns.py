from __future__ import annotations

from typing import Any
import asyncio

from fastapi import APIRouter, Depends, Request, HTTPException

from codex_web.api.thread_scope import thread_scope_dependency
from codex_web.services.thread_scope import ThreadScopeService

from codex_web.api.identity import request_actor
from codex_web.models import TurnCreate, QueueOrderRepairRequest, QueuedBootstrapRecoveryRequest, QueuedOperatorAdmissionRequest
from codex_web.services.turns import TurnService
from codex_web.notification_batches import NotificationBatchRecoveryRequest
from codex_web.notification_results import NotificationResultCaptureRequest
from codex_web.api.notification_processing import attach_notification_processing


def build_turns_router(service: TurnService, scope: ThreadScopeService, order_recovery=None, bootstrap_recovery=None, notification_batches=None, batch_recovery=None, batch_results=None, operator_admission=None, batch_processing=None) -> APIRouter:
    router = APIRouter(tags=["turns"], dependencies=[Depends(thread_scope_dependency(scope))])
    if batch_processing is not None:
        attach_notification_processing(router, batch_processing)

    @router.post("/api/threads/{thread_id}/resume")
    async def resume_thread(
        thread_id: str,
        request: Request,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        force_resume: bool = False,
    ) -> dict[str, Any]:
        return await service.resume(
            thread_id,
            project_id=request.state.thread_project.id,
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
    async def start_turn(
        thread_id: str,
        payload: TurnCreate,
        request: Request,
    ) -> dict[str, Any]:
        if payload.read_only_resource_ids:
            raise HTTPException(status_code=409,detail={"code":"resource_context_versioned_endpoint_required","route":"/api/threads/{thread_id}/turns/resource-context-v1"})
        return await service.start(
            thread_id,
            payload.model_copy(update={"project_id": request.state.thread_project.id}),
            actor=request_actor(request),
        )

    @router.post('/api/threads/{thread_id}/turns/resource-context-v1')
    async def start_context_turn(thread_id: str,payload: TurnCreate,request: Request):
        # Old servers return404 before admission; no ignored-field downgrade.
        return await service.start(thread_id,
            payload.model_copy(update={'project_id':request.state.thread_project.id}),
            actor=request_actor(request))

    @router.get("/api/threads/{thread_id}/preflight-attempts")
    async def preflight_attempts(
        thread_id: str,
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        return service.preflight_attempts(
            thread_id,
            actor=request_actor(request),
            limit=limit,
        )

    if notification_batches is not None:
        @router.get('/api/threads/{thread_id}/notification-batches')
        async def list_notification_batches(thread_id: str, request: Request, limit: int = 20, after: str | None = None):
            return notification_batches.list(thread_id=thread_id,
                project=request.state.thread_project, actor=request_actor(request), limit=limit, after=after)

        @router.get('/api/threads/{thread_id}/notification-batches/{batch_id}')
        async def inspect_notification_batch(thread_id: str, batch_id: str, request: Request):
            return notification_batches.detail(batch_id, thread_id=thread_id,
                project=request.state.thread_project, actor=request_actor(request))

    if batch_results is not None:
        @router.post('/api/threads/{thread_id}/notification-batches/{batch_id}/results')
        async def capture_batch_result(thread_id: str, batch_id: str, payload: NotificationResultCaptureRequest, request: Request):
            return await asyncio.to_thread(batch_results.collect_for_operator, batch_id, payload,
                thread_id=thread_id, project=request.state.thread_project, actor=request_actor(request))

    if batch_recovery is not None:
        @router.get('/api/threads/{thread_id}/notification-batches/{batch_id}/recovery')
        async def preview_batch_recovery(thread_id: str,batch_id: str,request: Request):
            return await asyncio.to_thread(batch_recovery.preview,batch_id,thread_id=thread_id,
                project=request.state.thread_project,actor=request_actor(request))

        @router.post('/api/threads/{thread_id}/notification-batches/{batch_id}/recovery')
        async def restore_batch_originals(thread_id: str,batch_id: str,payload: NotificationBatchRecoveryRequest,request: Request):
            return await batch_recovery.recover(batch_id,payload,thread_id=thread_id,
                project=request.state.thread_project,actor=request_actor(request))

    if operator_admission is not None:
        @router.get('/api/threads/{thread_id}/preflight-attempts/{attempt_id}/operator-admission')
        async def preview_operator_admission(thread_id: str, attempt_id: str, request: Request):
            return await asyncio.to_thread(operator_admission.preview, attempt_id,
                thread_id=thread_id, project=request.state.thread_project, actor=request_actor(request))

        @router.post('/api/threads/{thread_id}/preflight-attempts/{attempt_id}/operator-admission')
        async def admit_original(thread_id: str, attempt_id: str,
                                 payload: QueuedOperatorAdmissionRequest, request: Request):
            return await operator_admission.admit(attempt_id, payload, thread_id=thread_id,
                project=request.state.thread_project, actor=request_actor(request))

    @router.post(
        "/api/threads/{thread_id}/preflight-attempts/{attempt_id}/retry"
    )
    async def retry_preflight_attempt(
        thread_id: str,
        attempt_id: str,
        request: Request,
    ) -> dict[str, Any]:
        return await service.retry_preflight(
            thread_id,
            attempt_id,
            actor=request_actor(request),
        )

    @router.post('/api/threads/{thread_id}/preflight-attempts/{attempt_id}/reconcile-prestart')
    async def reconcile_prestart(thread_id: str, attempt_id: str, request: Request) -> dict[str, Any]:
        return service.reconcile_preflight(thread_id, attempt_id, actor=request_actor(request))

    if bootstrap_recovery is not None:
        @router.get('/api/threads/{thread_id}/preflight-attempts/{attempt_id}/bootstrap-recovery')
        async def preview_bootstrap_recovery(thread_id: str, attempt_id: str, request: Request):
            return bootstrap_recovery.preview(attempt_id, thread_id=thread_id,
                project=request.state.thread_project, actor=request_actor(request))

        @router.post('/api/threads/{thread_id}/preflight-attempts/{attempt_id}/bootstrap-recovery')
        async def recover_bootstrap(thread_id: str, attempt_id: str,
                                    payload: QueuedBootstrapRecoveryRequest, request: Request):
            return await bootstrap_recovery.recover(attempt_id, thread_id=thread_id,
                project=request.state.thread_project, actor=request_actor(request),
                expected_snapshot_sha256=payload.expected_snapshot_sha256, reason=payload.reason)

    @router.post('/api/threads/{thread_id}/preflight-attempts/{attempt_id}/retire-scheduling')
    async def retire_scheduling(thread_id: str, attempt_id: str, request: Request) -> dict[str, Any]:
        return await service.retire_preflight_scheduling(
            thread_id,
            attempt_id,
            actor=request_actor(request),
        )

    @router.get("/api/threads/{thread_id}/queue")
    async def thread_queue(thread_id: str) -> dict[str, Any]:
        return service.queue(thread_id)

    if order_recovery is not None:
        @router.get("/api/threads/{thread_id}/queue/order-recovery")
        async def preview_queue_order(thread_id: str, request: Request):
            return order_recovery.preview(thread_id, project=request.state.thread_project,
                actor=request_actor(request))

        @router.post("/api/threads/{thread_id}/queue/order-recovery")
        async def repair_queue_order(thread_id: str, payload: QueueOrderRepairRequest, request: Request):
            return await order_recovery.repair(thread_id, payload,
                project=request.state.thread_project, actor=request_actor(request))

    @router.post("/api/threads/{thread_id}/queue/steer")
    async def steer_latest(thread_id: str) -> dict[str, Any]:
        return await service.steer_latest(thread_id)

    @router.post("/api/threads/{thread_id}/queue/{queued_id}/steer")
    async def steer_specific(thread_id: str, queued_id: str) -> dict[str, Any]:
        return await service.steer(thread_id, queued_id)

    return router
