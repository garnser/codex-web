from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from codex_web.models import ProjectCreate, TaskSourceConfiguration
from codex_web.services.projects import (
    InvalidProjectPathError,
    LastProjectDeletionError,
    ProjectNotFoundError,
    ProjectService,
)


def build_projects_router(service: ProjectService) -> APIRouter:
    router = APIRouter(tags=["projects"])

    @router.get("/api/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return [project.model_dump() for project in service.list()]

    @router.post("/api/projects")
    async def create_project(payload: ProjectCreate) -> dict[str, Any]:
        try:
            return service.create(payload).model_dump()
        except InvalidProjectPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/projects/{project_id}/task-source")
    async def set_project_task_source(
        project_id: str,
        payload: TaskSourceConfiguration,
    ) -> dict[str, Any]:
        try:
            return service.set_authoritative_task_source(project_id, payload).model_dump()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/projects/{project_id}/task-source")
    async def clear_project_task_source(project_id: str) -> dict[str, Any]:
        try:
            return service.set_authoritative_task_source(project_id, None).model_dump()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str) -> dict[str, bool]:
        try:
            service.delete(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastProjectDeletionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return router
