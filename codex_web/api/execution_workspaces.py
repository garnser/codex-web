from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.execution_workspace_backend import ExecutionWorkspaceBackendError
from codex_web.execution_workspaces import (
    ExecutionWorkspaceAcquire,
    ExecutionWorkspaceRelease,
    ExecutionWorkspaceRenew,
    WorkspaceIntegrationRecord,
)
from codex_web.services.execution_workspaces import (
    ExecutionWorkspaceConflictError,
    ExecutionWorkspaceError,
    ExecutionWorkspaceLeaseError,
    ExecutionWorkspaceNotFoundError,
    ExecutionWorkspaceQuotaError,
    ExecutionWorkspaceService,
)
from codex_web.services.identity import AuthorizationError, IdentityService, TenantIsolationError
from codex_web.services.resources import ResourceNotFoundError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ExecutionWorkspaceNotFoundError, ResourceNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ExecutionWorkspaceQuotaError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, (ExecutionWorkspaceConflictError, ExecutionWorkspaceLeaseError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ExecutionWorkspaceBackendError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ExecutionWorkspaceError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_execution_workspaces_router(service: ExecutionWorkspaceService) -> APIRouter:
    router = APIRouter(tags=["execution-workspaces"])

    @router.get("/api/execution-workspaces")
    async def list_workspaces(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list(actor)
            ]
        }

    @router.post("/api/execution-workspaces")
    async def acquire_workspace(
        payload: ExecutionWorkspaceAcquire,
        request: Request,
    ) -> dict[str, Any]:
        try:
            workspace = service.acquire(payload, actor=request_actor(request))
            return {"item": workspace.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExecutionWorkspaceError,
                    ExecutionWorkspaceBackendError,
                    AuthorizationError,
                    TenantIsolationError,
                    ResourceNotFoundError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/execution-workspaces/recover")
    async def recover_workspaces(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
            items = service.recover_expired(scope=actor.tenant)
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExecutionWorkspaceError,
                    ExecutionWorkspaceBackendError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/execution-workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get(workspace_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkspaceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.get("/api/execution-workspaces/{workspace_id}/events")
    async def workspace_events(workspace_id: str, request: Request) -> dict[str, Any]:
        try:
            items = service.events(workspace_id, actor=request_actor(request))
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkspaceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/api/execution-workspaces/{workspace_id}/renew")
    async def renew_workspace(
        workspace_id: str,
        payload: ExecutionWorkspaceRenew,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.renew(
                workspace_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkspaceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/api/execution-workspaces/{workspace_id}/integration")
    async def record_integration(
        workspace_id: str,
        payload: WorkspaceIntegrationRecord,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.record_integration(
                workspace_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExecutionWorkspaceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/api/execution-workspaces/{workspace_id}/release")
    async def release_workspace(
        workspace_id: str,
        payload: ExecutionWorkspaceRelease,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.release(
                workspace_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExecutionWorkspaceError,
                    ExecutionWorkspaceBackendError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    return router
