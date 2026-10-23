from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.services.identity import (
    AuthorizationError,
    IdentityError,
    TenantIsolationError,
)
from codex_web.services.project_readiness import ProjectReadinessService
from codex_web.models import SandboxMode


def build_project_readiness_router(
    service: ProjectReadinessService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/projects/{project_id}/readiness",
        tags=["projects", "readiness"],
    )

    @router.get("")
    async def readiness(
        project_id: str,
        request: Request,
        execution_profile_id: str | None = Query(default=None, min_length=1, max_length=128),
        sandbox: SandboxMode | None = None,
    ) -> dict[str, Any]:
        if sandbox is not None and execution_profile_id is None:
            raise HTTPException(status_code=422, detail="Execution profile is required for contextual sandbox readiness")
        try:
            value = await service.evaluate_with_refresh(
                project_id,
                actor=request_actor(request),
                record=True,
                **({"execution_profile_id": execution_profile_id, "sandbox": sandbox}
                   if execution_profile_id is not None else {}),
            )
        except (AuthorizationError, TenantIsolationError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except IdentityError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(
                status_code=404,
                detail="Project not found",
            ) from exc
        return {
            **value.model_dump(mode="json"),
            "blockerCount": len(value.blockers),
            "warningCount": len(value.warnings),
        }

    return router
