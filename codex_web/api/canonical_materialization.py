from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from codex_web.api.identity import request_actor
from codex_web.canonical_materialization import (
    CanonicalMaterializationPlan,
)
from codex_web.services.canonical_materialization import (
    CanonicalMaterializationBlocked,
    CanonicalMaterializationError,
    CanonicalMaterializationPlanStale,
    CanonicalMaterializationService,
)


class CanonicalMaterializationPlanRequest(BaseModel):
    confirm_generic_target: bool = False


class CanonicalMaterializationApplyRequest(BaseModel):
    plan: CanonicalMaterializationPlan


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CanonicalMaterializationPlanStale):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CanonicalMaterializationBlocked):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CanonicalMaterializationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_canonical_materialization_router(
    service: CanonicalMaterializationService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/projects/{project_id}/canonical-materialization",
        tags=["projects"],
    )

    @router.post("/plan")
    async def plan(
        project_id: str,
        payload: CanonicalMaterializationPlanRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            value = service.plan(
                project_id,
                actor=request_actor(request),
                confirm_generic_target=payload.confirm_generic_target,
            )
            return {
                "plan": value.model_dump(mode="json"),
                "counts": value.counts(),
                "blocked": bool(value.blockers),
                "report": service.human_report(value),
            }
        except Exception as exc:
            if isinstance(exc, CanonicalMaterializationError):
                raise _error(exc) from exc
            raise

    @router.post("/apply")
    async def apply(
        project_id: str,
        payload: CanonicalMaterializationApplyRequest,
        request: Request,
    ) -> dict[str, Any]:
        if payload.plan.project_id != project_id:
            raise HTTPException(
                status_code=400,
                detail="materialization plan Project does not match route",
            )
        try:
            execution = service.apply(
                payload.plan,
                actor=request_actor(request),
            )
            return {
                "execution": execution.model_dump(mode="json"),
                "counts": execution.counts(),
                "report": service.human_report(execution),
            }
        except Exception as exc:
            if isinstance(exc, CanonicalMaterializationError):
                raise _error(exc) from exc
            raise

    @router.get("/status")
    async def status(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        values = service.status(
            project_id,
            actor=request_actor(request),
        )
        return {
            "version": "1.0",
            "items": [
                item.model_dump(mode="json")
                for item in values
            ],
        }

    return router
