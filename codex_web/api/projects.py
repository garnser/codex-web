from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.models import (
    ProjectCreate,
    ProjectRepositorySelectionUpdate,
    TaskSourceConfiguration,
)
from codex_web.services.identity import IdentityError, IdentityService, identity_http_error
from codex_web.services.projects import (
    InvalidProjectPathError,
    LastProjectDeletionError,
    ProjectNotFoundError,
    ProjectService,
)


def build_projects_router(
    service: ProjectService,
    fresh_bootstrap: Any | None = None,
) -> APIRouter:
    router = APIRouter(tags=["projects"])

    @router.get("/api/projects")
    async def list_projects(request: Request) -> list[dict[str, Any]]:
        scope = request.state.tenant_scope
        return [project.model_dump() for project in service.list(scope)]

    @router.post("/api/projects")
    async def create_project(payload: ProjectCreate, request: Request) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
            project = service.create(payload, request.state.tenant_scope)
            result = project.model_dump()
            if fresh_bootstrap is not None:
                result["freshBootstrap"] = fresh_bootstrap.bootstrap(
                    project.id,
                    actor=request.state.identity_actor,
                )
            return result
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except InvalidProjectPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/projects/{project_id}/fresh-bootstrap")
    async def fresh_project_bootstrap(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if fresh_bootstrap is None:
            raise HTTPException(
                status_code=503,
                detail="fresh Project bootstrap is unavailable",
            )
        try:
            IdentityService.require_admin(request.state.identity_actor)
            service.get(project_id, request.state.tenant_scope)
            return fresh_bootstrap.bootstrap(
                project_id,
                actor=request.state.identity_actor,
            )
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/api/projects/{project_id}/repository-selection-policy")
    async def set_repository_selection_policy(
        project_id: str,
        payload: ProjectRepositorySelectionUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
            return service.set_repository_selection_policy(
                project_id,
                payload,
                request.state.tenant_scope,
            ).model_dump()
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/api/projects/{project_id}/task-source")
    async def set_project_task_source(
        project_id: str,
        payload: TaskSourceConfiguration,
        request: Request,
    ) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
            return service.set_authoritative_task_source(
                project_id, payload, request.state.tenant_scope
            ).model_dump()
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/projects/{project_id}/task-source")
    async def clear_project_task_source(project_id: str, request: Request) -> dict[str, Any]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
            return service.set_authoritative_task_source(
                project_id, None, request.state.tenant_scope
            ).model_dump()
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, request: Request) -> dict[str, bool]:
        try:
            IdentityService.require_admin(request.state.identity_actor)
            service.delete(project_id, request.state.tenant_scope)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastProjectDeletionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return router
