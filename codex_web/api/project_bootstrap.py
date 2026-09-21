from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from codex_web.api.identity import request_actor
from codex_web.project_bootstrap import ProjectBootstrapManifest
from codex_web.services.project_bootstrap import (
    ProjectBootstrapApprovalRequired,
    ProjectBootstrapBlocked,
    ProjectBootstrapConcurrentApply,
    ProjectBootstrapError,
    ProjectBootstrapPlanStale,
    ProjectBootstrapService,
)


class ProjectBootstrapPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: ProjectBootstrapManifest
    migrate_legacy: bool = True


class ProjectBootstrapApplyRequest(ProjectBootstrapPlanRequest):
    expected_plan_id: str
    approve_authority_changes: bool = False


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectBootstrapConcurrentApply):
        return HTTPException(
            status_code=409,
            detail={"code": "bootstrap_apply_conflict", "message": str(exc)},
        )
    if isinstance(exc, ProjectBootstrapPlanStale):
        return HTTPException(
            status_code=409,
            detail={"code": "bootstrap_plan_stale", "message": str(exc)},
        )
    if isinstance(exc, ProjectBootstrapApprovalRequired):
        return HTTPException(
            status_code=409,
            detail={
                "code": "bootstrap_authority_approval_required",
                "message": str(exc),
            },
        )
    if isinstance(exc, ProjectBootstrapBlocked):
        return HTTPException(
            status_code=409,
            detail={"code": "bootstrap_blocked", "message": str(exc)},
        )
    return HTTPException(status_code=400, detail=str(exc))


def build_project_bootstrap_router(
    service: ProjectBootstrapService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/projects/{project_id}/bootstrap",
        tags=["projects", "bootstrap"],
    )

    @router.post("/preflight")
    async def preflight(
        project_id: str,
        payload: ProjectBootstrapPlanRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            value = service.preflight(
                project_id,
                payload.manifest,
                actor=request_actor(request),
                migrate_legacy=payload.migrate_legacy,
            )
            return {
                "preflight": value.model_dump(mode="json"),
                "blocked": value.blocked,
                "report": service.human_report(value),
            }
        except ProjectBootstrapError as exc:
            raise _error(exc) from exc

    @router.post("/plan")
    async def plan(
        project_id: str,
        payload: ProjectBootstrapPlanRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            value = service.plan(
                project_id,
                payload.manifest,
                actor=request_actor(request),
                migrate_legacy=payload.migrate_legacy,
            )
            return {
                "plan": value.model_dump(mode="json"),
                "counts": value.counts(),
                "blocked": bool(value.blockers),
                "report": service.human_report(value),
            }
        except ProjectBootstrapError as exc:
            raise _error(exc) from exc

    @router.post("/apply")
    async def apply(
        project_id: str,
        payload: ProjectBootstrapApplyRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = request_actor(request)
            plan = service.plan(
                project_id,
                payload.manifest,
                actor=actor,
                migrate_legacy=payload.migrate_legacy,
            )
            if plan.id != payload.expected_plan_id:
                raise ProjectBootstrapPlanStale(
                    "bootstrap plan changed since operator review"
                )
            execution = service.apply(
                plan,
                actor=actor,
                approve_authority_changes=payload.approve_authority_changes,
            )
            return {
                "execution": execution.model_dump(mode="json"),
                "counts": execution.plan.counts(),
                "report": service.human_report(execution),
            }
        except ProjectBootstrapError as exc:
            raise _error(exc) from exc

    @router.get("/status")
    async def status(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
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
        except ProjectBootstrapError as exc:
            raise _error(exc) from exc

    return router
