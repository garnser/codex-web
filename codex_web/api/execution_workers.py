from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentCompleteRequest,
    AssignmentRenewRequest,
    AssignmentStartRequest,
    ExecutionAssignmentCreate,
    ExecutionWorkerRegister,
    WorkerHeartbeatRequest,
    WorkerLifecycle,
    WorkerLifecycleUpdate,
)
from codex_web.services.execution_workers import (
    AssignmentNotFoundError,
    ExecutionWorkerError,
    ExecutionWorkerService,
    WorkerCapabilityError,
    WorkerConflictError,
    WorkerLeaseError,
    WorkerNotFoundError,
)
from codex_web.services.identity import AuthorizationError, TenantIsolationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (WorkerNotFoundError, AssignmentNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (WorkerConflictError, WorkerLeaseError, WorkerCapabilityError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ExecutionWorkerError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_execution_workers_router(service: ExecutionWorkerService) -> APIRouter:
    router = APIRouter(prefix="/api/execution-workers", tags=["execution-workers"])

    @router.get("")
    async def list_workers(request: Request) -> dict[str, Any]:
        try:
            items = service.list_workers(request_actor(request))
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("")
    async def register_worker(
        payload: ExecutionWorkerRegister,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.register(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/heartbeat")
    async def heartbeat(
        worker_id: str,
        payload: WorkerHeartbeatRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.heartbeat(
                worker_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/drain")
    async def drain(
        worker_id: str,
        payload: WorkerLifecycleUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_lifecycle(
                worker_id,
                WorkerLifecycle.DRAINING,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/quarantine")
    async def quarantine(
        worker_id: str,
        payload: WorkerLifecycleUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_lifecycle(
                worker_id,
                WorkerLifecycle.QUARANTINED,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/revoke")
    async def revoke(
        worker_id: str,
        payload: WorkerLifecycleUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_lifecycle(
                worker_id,
                WorkerLifecycle.REVOKED,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.get("/assignments")
    async def list_assignments(
        request: Request,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            items = service.list_assignments(
                request_actor(request),
                worker_id=worker_id,
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/assignments")
    async def create_assignment(
        payload: ExecutionAssignmentCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_assignment(
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/claim")
    async def claim(
        worker_id: str,
        payload: AssignmentClaimRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.claim(
                worker_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json") if item else None}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/assignments/{assignment_id}/renew")
    async def renew(
        worker_id: str,
        assignment_id: str,
        payload: AssignmentRenewRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.renew(
                worker_id,
                assignment_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/assignments/{assignment_id}/start")
    async def start(
        worker_id: str,
        assignment_id: str,
        payload: AssignmentStartRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.start(
                worker_id,
                assignment_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{worker_id}/assignments/{assignment_id}/complete")
    async def complete(
        worker_id: str,
        assignment_id: str,
        payload: AssignmentCompleteRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.complete(
                worker_id,
                assignment_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/assignments/recover-expired")
    async def recover_expired(request: Request) -> dict[str, Any]:
        try:
            ids = service.recover_expired(actor=request_actor(request))
            return {"assignment_ids": ids}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/assignments/{assignment_id}/retry")
    async def retry_lost(assignment_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.retry_lost(
                assignment_id,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.get("/events")
    async def events(request: Request) -> dict[str, Any]:
        try:
            items = service.events(request_actor(request))
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkerError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    return router
