from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.services.reconciliation_gates import (
    ReconciliationGateConflict,
    ReconciliationGateError,
    ReconciliationGateService,
)


class ReconciliationApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlation_id: str | None = None


class ReconciliationPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)


def build_reconciliation_gates_router(
    service: ReconciliationGateService,
) -> APIRouter:
    router = APIRouter(tags=["reconciliation-gates"])

    @router.get("/api/projects/{project_id}/reconciliation-gates")
    async def status(project_id: str, request: Request) -> dict[str, Any]:
        try:
            return service.status(
                project_id,
                actor=request_actor(request),
            )
        except ReconciliationGateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/api/projects/{project_id}/reconciliation-gates/{service_id}/approve"
    )
    async def approve(
        project_id: str,
        service_id: str,
        payload: ReconciliationApprovalRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.approve(
                service_id,
                project_id,
                actor=request_actor(request),
                correlation_id=payload.correlation_id,
            ).model_dump(mode="json")
        except ReconciliationGateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReconciliationGateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/api/projects/{project_id}/reconciliation-gates/{service_id}/pause"
    )
    async def pause(
        project_id: str,
        service_id: str,
        payload: ReconciliationPauseRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.pause(
                service_id,
                project_id,
                actor=request_actor(request),
                reason=payload.reason,
            ).model_dump(mode="json")
        except ReconciliationGateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/api/projects/{project_id}/reconciliation-gates/{service_id}/resume"
    )
    async def resume(
        project_id: str,
        service_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.resume(
                service_id,
                project_id,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except ReconciliationGateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
