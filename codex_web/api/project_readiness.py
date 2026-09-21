from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.identity import (
    AuthorizationError,
    IdentityError,
    TenantIsolationError,
)
from codex_web.services.project_readiness import ProjectReadinessService


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
    ) -> dict[str, Any]:
        try:
            value = service.evaluate(
                project_id,
                actor=request_actor(request),
                record=True,
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
