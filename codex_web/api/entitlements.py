from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.services.entitlement_control import EntitlementAdministration

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.entitlements import (
    CapabilityEntitlementUpdate,
    EntitlementChangePreview,
    EntitlementControlUpdate,
    QuotaPolicyUpdate,
    TenantModeUpdate,
    UsageEventCreate,
    UsageReconciliationBatch,
)
from codex_web.services.entitlements import (
    EntitlementDeniedError,
    EntitlementConflictError,
    EntitlementError,
    EntitlementService,
    QuotaExceededError,
    UsageIdempotencyConflictError,
)
from codex_web.services.identity import AuthorizationError, IdentityService, TenantIsolationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, QuotaExceededError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, EntitlementDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (UsageIdempotencyConflictError, EntitlementConflictError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (EntitlementError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, TenantIsolationError):
        return HTTPException(status_code=404, detail="resource not found")
    return HTTPException(status_code=400, detail=str(exc))


def build_entitlements_router(service: EntitlementService, projects=None) -> APIRouter:
    router = APIRouter(prefix="/api/entitlements", tags=["entitlements"],
                      dependencies=[Depends(project_record_context_parameter)])
    administration = EntitlementAdministration(service)

    def context(request: Request):
        ProjectRecordScope.from_request(request, request_actor(request), projects)

    router.dependencies.append(Depends(context))

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind != PrincipalKind.SERVICE:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/administration")
    async def administration_snapshot(request: Request):
        result = administration.snapshot(request_actor(request))
        try:
            mutation_actor(request)
        except AuthorizationError as exc:
            result.update(can_manage=False, denial_reason=str(exc))
        return result

    @router.post("/administration/preview")
    async def administration_preview(payload: EntitlementChangePreview, request: Request):
        try:
            return administration.preview(payload, actor=mutation_actor(request))
        except (EntitlementError, AuthorizationError, ValueError) as exc:
            raise _error(exc) from exc

    @router.put("/control")
    async def control(payload: EntitlementControlUpdate, request: Request, expected_revision: str):
        try:
            item = administration.set_control(payload, actor=request_actor(request), expected_revision=expected_revision)
            return {"item": item.model_dump(mode="json")}
        except (EntitlementError, AuthorizationError, ValueError) as exc:
            raise _error(exc) from exc

    @router.delete("/quotas/{metric}")
    async def retire_quota(metric: str, request: Request, expected_revision: str):
        try:
            administration.retire_quota(metric, actor=mutation_actor(request), expected_revision=expected_revision)
            return {"retired": True}
        except (EntitlementError, AuthorizationError, ValueError) as exc:
            raise _error(exc) from exc

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
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        try:
            item = service.set_mode(payload.mode, actor=mutation_actor(request), expected_revision=expected_revision)
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
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        try:
            item = service.set_capability(
                capability,
                payload,
                actor=mutation_actor(request), expected_revision=expected_revision,
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
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        try:
            item = service.set_quota(metric, payload, actor=mutation_actor(request), expected_revision=expected_revision)
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
            result = service.record_usage(payload, actor=mutation_actor(request))
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
                actor=mutation_actor(request),
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
