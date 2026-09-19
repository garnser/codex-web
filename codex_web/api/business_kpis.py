from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.business_kpis import (
    BUSINESS_KPI_TEMPLATES,
    BusinessKPIDefinitionCreate,
    BusinessKPIDefinitionUpdate,
    BusinessKPITargetBindingCreate,
    BusinessKPITargetKind,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.business_context import (
    BusinessContextError,
    BusinessContextNotFoundError,
)
from codex_web.services.business_kpis import (
    BusinessKPIConflictError,
    BusinessKPIError,
    BusinessKPINotFoundError,
    BusinessKPIService,
    BusinessKPIValidationError,
)
from codex_web.services.goals import GoalNotFoundError
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.metrics import MetricError, MetricNotFoundError
from codex_web.storage.decisions import DecisionNotFoundError


def _error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (
            BusinessKPINotFoundError,
            BusinessContextNotFoundError,
            GoalNotFoundError,
            DecisionNotFoundError,
            MetricNotFoundError,
        ),
    ):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BusinessKPIConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(
        exc,
        (
            BusinessKPIValidationError,
            BusinessKPIError,
            BusinessContextError,
            MetricError,
            ValueError,
        ),
    ):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_business_kpis_router(
    service: BusinessKPIService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/business-kpis",
        tags=["business-kpis"],
    )

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "business-kpis:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "business-kpis:admin service scope required"
                )
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    @router.get("/templates")
    async def templates() -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in BUSINESS_KPI_TEMPLATES
            ],
            "count": len(BUSINESS_KPI_TEMPLATES),
        }

    @router.get("")
    async def list_kpis(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        rows = service.list(actor=actor)
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("")
    async def create_kpi(
        payload: BusinessKPIDefinitionCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=mutation_actor(request))
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/refresh")
    async def refresh_all(request: Request) -> dict[str, Any]:
        try:
            rows = service.refresh_all(actor=mutation_actor(request))
        except Exception as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.get("/operating-view")
    async def operating_view(
        request: Request,
        at: float | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.operating_view(actor=actor, at=at)
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/operating-snapshots")
    async def capture_operating_snapshot(
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.capture_operating_snapshot(
                actor=mutation_actor(request)
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/operating-snapshots")
    async def operating_snapshots(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.operating_snapshots(actor=actor, limit=limit)
        except Exception as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/bindings")
    async def create_binding(
        payload: BusinessKPITargetBindingCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.bind(payload, actor=mutation_actor(request))
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/bindings")
    async def list_bindings(
        request: Request,
        kpi_id: str | None = None,
        target_kind: BusinessKPITargetKind | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.bindings(
                actor=actor,
                kpi_id=kpi_id,
                target_kind=target_kind,
                target_id=target_id,
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.delete("/bindings/{binding_id}")
    async def delete_binding(
        binding_id: str,
        request: Request,
    ) -> dict[str, bool]:
        try:
            service.unbind(binding_id, actor=mutation_actor(request))
        except Exception as exc:
            raise _error(exc) from exc
        return {"deleted": True}

    @router.get("/targets/{target_kind}/{target_id}/snapshots")
    async def target_snapshots(
        target_kind: BusinessKPITargetKind,
        target_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.target_snapshot(
                target_kind,
                target_id,
                actor=actor,
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.get("/{kpi_id}")
    async def get_kpi(
        kpi_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(kpi_id, actor=actor)
            current = service.operating_view(actor=actor)
            current_item = next(
                (row for row in current.items if row.kpi_id == kpi_id),
                None,
            )
            latest_refresh = service.latest_refresh(kpi_id, actor=actor)
        except Exception as exc:
            raise _error(exc) from exc
        return {
            "item": item.model_dump(mode="json"),
            "current": (
                current_item.model_dump(mode="json")
                if current_item is not None
                else None
            ),
            "latest_refresh": (
                latest_refresh.model_dump(mode="json")
                if latest_refresh is not None
                else None
            ),
        }

    @router.patch("/{kpi_id}")
    async def update_kpi(
        kpi_id: str,
        payload: BusinessKPIDefinitionUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update(
                kpi_id,
                payload,
                actor=mutation_actor(request),
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{kpi_id}/revisions")
    async def kpi_revisions(
        kpi_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.revisions(kpi_id, actor=actor)
        except Exception as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/{kpi_id}/refresh")
    async def refresh_kpi(
        kpi_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.refresh(
                kpi_id,
                actor=mutation_actor(request),
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    return router
