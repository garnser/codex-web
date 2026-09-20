from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.legacy_project_migration import LegacyMigrationPlan
from codex_web.services.identity import IdentityError, IdentityService, identity_http_error
from codex_web.services.legacy_project_migration import (
    LegacyProjectMigrationApprovalRequired,
    LegacyProjectMigrationBlocked,
    LegacyProjectMigrationError,
    LegacyProjectMigrationPlanStale,
    LegacyProjectMigrationService,
)


class LegacyMigrationApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: LegacyMigrationPlan
    approve_material_authority_changes: bool = False
    compatibility_window_seconds: int = Field(
        default=7 * 24 * 60 * 60,
        ge=0,
        le=90 * 24 * 60 * 60,
    )


def build_legacy_project_migration_router(
    service: LegacyProjectMigrationService,
) -> APIRouter:
    router = APIRouter(tags=["legacy-project-migration"])

    def admin(request: Request):
        actor = request.state.identity_actor
        IdentityService.require_admin(actor)
        return actor

    @router.post("/api/projects/{project_id}/legacy-migration/dry-run")
    async def dry_run(project_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = admin(request)
            return service.plan(project_id, actor=actor).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except LegacyProjectMigrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/projects/{project_id}/legacy-migration/apply")
    async def apply(
        project_id: str,
        payload: LegacyMigrationApplyRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
            if payload.plan.project_id != project_id:
                raise HTTPException(
                    status_code=409,
                    detail="migration plan project does not match route project",
                )
            result = service.apply(
                payload.plan,
                actor=actor,
                approve_material_authority_changes=(
                    payload.approve_material_authority_changes
                ),
                compatibility_window_seconds=payload.compatibility_window_seconds,
            )
            return result.model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except LegacyProjectMigrationApprovalRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "migration_authority_approval_required",
                    "message": str(exc),
                },
            ) from exc
        except LegacyProjectMigrationPlanStale as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "migration_plan_stale",
                    "message": str(exc),
                },
            ) from exc
        except LegacyProjectMigrationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "migration_blocked",
                    "message": str(exc),
                },
            ) from exc
        except LegacyProjectMigrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/projects/{project_id}/legacy-migration/status")
    async def status(project_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = admin(request)
            items = service.status(project_id, actor=actor)
            return {
                "items": [item.model_dump(mode="json") for item in items],
                "count": len(items),
            }
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.get("/api/legacy-migration/path-compatibility")
    async def path_compatibility(
        path: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
            mapping = service.resolve_legacy_path(path, actor=actor)
            return {
                "resolved": mapping is not None,
                "mapping": (
                    mapping.model_dump(mode="json")
                    if mapping is not None
                    else None
                ),
            }
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    return router
