from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.business_kpis import (
    BUSINESS_KPI_TEMPLATES,
    BusinessKpiDecisionBindingRequest,
    BusinessKpiDefinitionCreate,
    BusinessKpiDefinitionUpdate,
    BusinessKpiGoalBindingRequest,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.business_kpis import (
    BusinessKpiConflictError,
    BusinessKpiError,
    BusinessKpiNotFoundError,
    BusinessKpiService,
    BusinessKpiValidationError,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, BusinessKpiNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BusinessKpiConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (BusinessKpiValidationError, BusinessKpiError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_business_kpis_router(service: BusinessKpiService) -> APIRouter:
    router = APIRouter(prefix="/api/business-kpis", tags=["business-kpis"])

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "business-kpi:admin" not in actor.service_scopes:
                raise AuthorizationError("business-kpi:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    @router.get("/templates")
    async def templates(request: Request) -> dict[str, Any]:
        request_actor(request)
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
        payload: BusinessKpiDefinitionCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=mutation_actor(request))
        except (AuthorizationError, BusinessKpiError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/operating-snapshots")
    async def operating_snapshots(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.operating_snapshots(actor=actor, limit=limit)
        except BusinessKpiError as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/operating-snapshots")
    async def capture_operating_snapshot(
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.operating_snapshot(actor=mutation_actor(request))
        except (AuthorizationError, BusinessKpiError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{kpi_id}")
    async def get_kpi(kpi_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(kpi_id, actor=actor)
        except BusinessKpiError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.patch("/{kpi_id}")
    async def update_kpi(
        kpi_id: str,
        payload: BusinessKpiDefinitionUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update(
                kpi_id,
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessKpiError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{kpi_id}/revisions")
    async def revisions(kpi_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.revisions(kpi_id, actor=actor)
        except BusinessKpiError as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/{kpi_id}/evaluate")
    async def evaluate(
        kpi_id: str,
        request: Request,
        at: float | None = None,
    ) -> dict[str, Any]:
        try:
            item = service.evaluate(
                kpi_id,
                actor=mutation_actor(request),
                at=at,
            )
        except (AuthorizationError, BusinessKpiError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{kpi_id}/observations/{observation_id}/attribution")
    async def attribution(
        kpi_id: str,
        observation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            definition = service.get(kpi_id, actor=actor)
            item = service.attribution(observation_id, actor=actor)
            if item.kpi_id != definition.id:
                raise BusinessKpiNotFoundError(
                    "business KPI observation attribution not found"
                )
        except BusinessKpiError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{kpi_id}/goal-bindings")
    async def bind_goal(
        kpi_id: str,
        payload: BusinessKpiGoalBindingRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.bind_goal(
                kpi_id,
                payload.goal_id,
                actor=mutation_actor(request),
                description=payload.description,
                operator=payload.operator,
                target_value=payload.target_value,
                allow_partial=payload.allow_partial,
            )
        except (AuthorizationError, BusinessKpiError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{kpi_id}/decision-bindings")
    async def bind_decision(
        kpi_id: str,
        payload: BusinessKpiDecisionBindingRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.bind_decision(
                kpi_id,
                payload.decision_id,
                actor=mutation_actor(request),
                summary=payload.summary,
                allow_partial=payload.allow_partial,
            )
        except (AuthorizationError, BusinessKpiError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    return router
