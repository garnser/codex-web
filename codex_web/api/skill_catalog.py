from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.identity import AuthorizationError
from codex_web.services.skill_catalog import (
    SkillCatalogService,
    SkillSourceConflict,
    SkillSourceNotFound,
)
from codex_web.skill_catalog import (
    SkillSourceCreate,
    SkillSourceHealthReport,
    SkillSourceSyncRequest,
    SkillSourceUpdate,
    UiSkillsDiscoveryRequest,
    UiSkillsImportRequest,
)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SkillSourceNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SkillSourceConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def build_skill_catalog_router(service: SkillCatalogService) -> APIRouter:
    router = APIRouter(prefix="/api/skill-sources", tags=["skills"])

    @router.get("")
    async def list_sources(request: Request) -> dict[str, Any]:
        items = service.list(actor=request_actor(request))
        return {"items": items, "count": len(items)}

    @router.post("")
    async def create_source(
        payload: SkillSourceCreate, request: Request
    ) -> dict[str, Any]:
        try:
            return {"item": service.create(payload, actor=request_actor(request))}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{source_id}")
    async def get_source(source_id: str, request: Request) -> dict[str, Any]:
        try:
            return {
                "item": service.get(source_id, actor=request_actor(request)).model_dump(
                    mode="json"
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.patch("/{source_id}")
    async def update_source(
        source_id: str, payload: SkillSourceUpdate, request: Request
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.update(source_id, payload, actor=request_actor(request))
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{source_id}/sync")
    async def sync_source(
        source_id: str, payload: SkillSourceSyncRequest, request: Request
    ) -> dict[str, Any]:
        try:
            return service.sync(source_id, payload, actor=request_actor(request))
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{source_id}/discovery")
    async def source_discovery(source_id: str, request: Request) -> dict[str, Any]:
        try:
            return service.discovery(source_id, actor=request_actor(request))
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{source_id}/discover")
    async def discover_ui_skills(
        source_id: str,
        payload: UiSkillsDiscoveryRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.discover_ui_skills(
                source_id, payload, actor=request_actor(request)
            )
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{source_id}/health")
    async def record_source_health(
        source_id: str,
        payload: SkillSourceHealthReport,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.record_health(
                source_id, payload, actor=request_actor(request)
            )
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{source_id}/import")
    async def import_ui_skills(
        source_id: str,
        payload: UiSkillsImportRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.import_ui_skills(
                source_id, payload, actor=request_actor(request)
            )
        except Exception as exc:
            raise _error(exc) from exc

    return router
