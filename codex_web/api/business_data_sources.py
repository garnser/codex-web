from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.business_data_sources import (
    BusinessDataSourceCreate,
    BusinessDataSourceStatusUpdate,
    UnsupportedBusinessDataSourceCapability,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.business_data_sources import (
    BusinessDataSourceConflictError,
    BusinessDataSourceError,
    BusinessDataSourceNotFoundError,
    BusinessDataSourceService,
    BusinessDataSourceUnavailableError,
    BusinessDataSourceValidationError,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, BusinessDataSourceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BusinessDataSourceConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, BusinessDataSourceUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(
        exc,
        (
            BusinessDataSourceValidationError,
            BusinessDataSourceError,
            UnsupportedBusinessDataSourceCapability,
            ValueError,
        ),
    ):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_business_data_sources_router(
    service: BusinessDataSourceService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/business-data-sources",
        tags=["business-data-sources"],
    )

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "business-data:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "business-data:admin service scope required"
                )
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    @router.get("")
    async def list_sources(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        rows = service.list(actor=actor)
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("")
    async def create_source(
        payload: BusinessDataSourceCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(
                payload,
                actor=mutation_actor(request),
            )
        except (
            AuthorizationError,
            BusinessDataSourceError,
            UnsupportedBusinessDataSourceCapability,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{source_id}")
    async def get_source(
        source_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(source_id, actor=actor)
        except BusinessDataSourceError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{source_id}/status")
    async def set_status(
        source_id: str,
        payload: BusinessDataSourceStatusUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_status(
                source_id,
                payload.status,
                actor=mutation_actor(request),
            )
        except (
            AuthorizationError,
            BusinessDataSourceError,
            UnsupportedBusinessDataSourceCapability,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{source_id}/sync")
    async def sync_source(
        source_id: str,
        request: Request,
        full_resync: bool = False,
        max_pages: int = Query(default=25, ge=1, le=25),
    ) -> dict[str, Any]:
        try:
            item = await service.sync(
                source_id,
                actor=mutation_actor(request),
                max_pages=max_pages,
                full_resync=full_resync,
            )
        except (
            AuthorizationError,
            BusinessDataSourceError,
            UnsupportedBusinessDataSourceCapability,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{source_id}/events")
    async def ingest_event(
        source_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.ingest_provider_event(
                source_id,
                payload,
                actor=mutation_actor(request),
            )
        except (
            AuthorizationError,
            BusinessDataSourceError,
            UnsupportedBusinessDataSourceCapability,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {
            "item": (
                item.model_dump(mode="json")
                if item is not None
                else None
            )
        }

    @router.get("/{source_id}/drift")
    async def inspect_drift(
        source_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.drift(source_id, actor=actor)
        except BusinessDataSourceError as exc:
            raise _error(exc) from exc
        return {"items": list(rows), "count": len(rows)}

    return router
