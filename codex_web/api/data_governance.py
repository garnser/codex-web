from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.data_governance import (
    ContextFilterRequest,
    DataCategory,
    DataClassification,
    ExportAuthorizationRequest,
    GovernedDataCreate,
    GovernanceActionRequest,
    LegalHoldRequest,
    RetentionSweepRequest,
)
from codex_web.services.data_governance import (
    DataGovernanceError,
    DataGovernanceService,
    GovernanceConflictError,
    GovernanceNotFoundError,
)
from codex_web.services.identity import AuthorizationError, TenantIsolationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, GovernanceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, GovernanceConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (DataGovernanceError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_data_governance_router(service: DataGovernanceService) -> APIRouter:
    router = APIRouter(prefix="/api/data-governance", tags=["data-governance"])

    @router.get("/records")
    async def list_records(
        request: Request,
        category: DataCategory | None = None,
        classification: DataClassification | None = None,
        project_id: str | None = None,
        include_inactive: bool = True,
    ) -> dict[str, Any]:
        items = service.list_records(
            request_actor(request),
            category=category,
            classification=classification,
            project_id=project_id,
            include_inactive=include_inactive,
        )
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.post("/records")
    async def register_record(
        payload: GovernedDataCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.register(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (DataGovernanceError, AuthorizationError, TenantIsolationError, ValueError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/records/{record_id}")
    async def get_record(record_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get_record(record_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/records/{record_id}/legal-hold")
    async def set_legal_hold(
        record_id: str,
        payload: LegalHoldRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_legal_hold(
                record_id,
                payload.reason,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.delete("/records/{record_id}/legal-hold")
    async def release_legal_hold(record_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.release_legal_hold(record_id, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.get("/actions")
    async def list_action_requests(request: Request) -> dict[str, Any]:
        items = service.list_requests(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.post("/actions")
    async def request_action(
        payload: GovernanceActionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.request_action(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/actions/{request_id}/execute")
    async def execute_action(request_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.execute_request(request_id, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/retention/sweep")
    async def retention_sweep(
        payload: RetentionSweepRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = service.retention_sweep(
                actor=request_actor(request),
                now=payload.now,
                execute=payload.execute,
            )
            return result.model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/context/filter")
    async def filter_context(
        payload: ContextFilterRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.filter_context(
                payload,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/exports/authorize")
    async def authorize_export(
        payload: ExportAuthorizationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.authorize_export(
                payload,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.get("/events")
    async def list_events(request: Request) -> dict[str, Any]:
        try:
            items = service.events(request_actor(request))
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (DataGovernanceError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    return router
