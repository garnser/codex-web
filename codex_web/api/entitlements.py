from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.entitlements import (
    CapabilityEntitlementUpdate,
    QuotaPolicyUpdate,
    TenantModeUpdate,
    UsageEventCreate,
    UsageReconciliationBatch,
)
from codex_web.services.entitlements import (
    EntitlementDeniedError,
    EntitlementError,
    EntitlementService,
    QuotaExceededError,
)
from codex_web.services.identity import AuthorizationError, TenantIsolationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, QuotaExceededError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, EntitlementDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (EntitlementError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, TenantIsolationError):
        return HTTPException(status_code=404, detail="resource not found")
    return HTTPException(status_code=400, detail=str(exc))


def build_entitlements_router(service: EntitlementService) -> APIRouter:
    router = APIRouter(prefix="/api/entitlements", tags=["entitlements"])

    @router.get("/status")
    async def status(
        request: Request,
        capability: str,
        metric: str | None = None,
        projected_amount: float = 0.0,
    ) -> dict[str, Any]:
        decision = service.check(
            capability,
            actor=request_actor(request),
            metric=metric,
            projected_amount=projected_amount,
        )
        return decision.model_dump(mode="json")

    @router.get("/mode")
    async def get_mode(request: Request) -> dict[str, str]:
        return {"mode": service.mode(request_actor(request)).value}

    @router.put("/mode")
    async def set_mode(
        payload: TenantModeUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_mode(payload.mode, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (EntitlementError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/capabilities")
    async def capabilities(request: Request) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.capabilities(request_actor(request))
            ]
        }

    @router.put("/capabilities/{capability}")
    async def set_capability(
        capability: str,
        payload: CapabilityEntitlementUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_capability(
                capability,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (EntitlementError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/quotas")
    async def quotas(request: Request) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.quotas(request_actor(request))
            ]
        }

    @router.put("/quotas/{metric}")
    async def set_quota(
        metric: str,
        payload: QuotaPolicyUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_quota(metric, payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (EntitlementError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/usage")
    async def usage(
        request: Request,
        metric: str | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        items = service.usage(actor, metric=metric, start=start, end=end)
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "total": sum(item.amount for item in items),
        }

    @router.post("/usage")
    async def record_usage(
        payload: UsageEventCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = service.record_usage(payload, actor=request_actor(request))
            return result.model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (EntitlementError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.post("/usage/reconcile")
    async def reconcile_usage(
        payload: UsageReconciliationBatch,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.reconcile_usage(
                payload,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (EntitlementError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/usage/export")
    async def export_usage(
        request: Request,
        received_after: float | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        try:
            return service.export_usage(
                request_actor(request),
                received_after=received_after,
                limit=limit,
            ).model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (EntitlementError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    return router
