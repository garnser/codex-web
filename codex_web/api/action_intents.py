from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.action_intents import (
    ActionInboxCreate,
    ActionIntentCancelRequest,
    ActionIntentClaimRequest,
    ActionIntentCreate,
    ActionIntentExecuteRequest,
    ActionIntentReconcileRequest,
    ActionIntentRenewRequest,
    ActionIntentRetryRequest,
    ActionIntentRollbackRequest,
    ActionIntentStatus,
)
from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.action_intents import (
    ActionIntentConflictError,
    ActionIntentError,
    ActionIntentLeaseError,
    ActionIntentNotFoundError,
    ActionIntentService,
    ActionIntentUnsafeRetryError,
)
from codex_web.services.action_providers import ActionProviderError
from codex_web.services.entitlements import EntitlementDeniedError, QuotaExceededError
from codex_web.services.identity import AuthorizationError, IdentityService, TenantIsolationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ActionIntentNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, QuotaExceededError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, EntitlementDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (ActionIntentConflictError, ActionIntentLeaseError, ActionIntentUnsafeRetryError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ActionProviderError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ActionIntentError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_action_intents_router(service: ActionIntentService) -> APIRouter:
    router = APIRouter(tags=["action-intents"])

    def require_action_intent_admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "action-intent:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "action-intent:admin service scope required"
                )
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/api/action-intents")
    async def list_action_intents(
        request: Request,
        work_item_ref: str | None = None,
        status: ActionIntentStatus | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list(
                    actor,
                    work_item_ref=work_item_ref,
                    status=status,
                )
            ]
        }

    @router.post("/api/action-intents")
    async def create_action_intent(
        payload: ActionIntentCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ActionIntentError,
                    ActionProviderError,
                    AuthorizationError,
                    TenantIsolationError,
                    EntitlementDeniedError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/claim")
    async def claim_next(
        payload: ActionIntentClaimRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.claim(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json") if item else None}
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/recover-stale")
    async def recover_stale(request: Request) -> dict[str, Any]:
        try:
            actor = require_action_intent_admin(request)
            return {
                "intent_ids": service.recover_stale_claims(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                )
            }
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/inbox")
    async def ingest_callback(
        payload: ActionInboxCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.ingest_callback(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/action-intents/{intent_id}")
    async def get_action_intent(intent_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get(intent_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ActionIntentError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.get("/api/action-intents/{intent_id}/history")
    async def action_intent_history(intent_id: str, request: Request) -> dict[str, Any]:
        try:
            return service.history(intent_id, request_actor(request))
        except Exception as exc:
            if isinstance(exc, (ActionIntentError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/claim")
    async def claim_action_intent(
        intent_id: str,
        payload: ActionIntentClaimRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.claim(
                payload,
                actor=request_actor(request),
                intent_id=intent_id,
            )
            if item is None:
                raise ActionIntentConflictError("action intent is not claimable")
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/renew")
    async def renew_claim(
        intent_id: str,
        payload: ActionIntentRenewRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.renew_claim(
                intent_id,
                payload.worker_id,
                payload.lease_seconds,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/execute")
    async def execute_claimed(
        intent_id: str,
        payload: ActionIntentExecuteRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.execute_claimed(
                intent_id,
                payload.worker_id,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ActionIntentError,
                    ActionProviderError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/retry")
    async def retry_action(
        intent_id: str,
        payload: ActionIntentRetryRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.retry(intent_id, payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/cancel")
    async def cancel_action(
        intent_id: str,
        payload: ActionIntentCancelRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.cancel(
                intent_id,
                payload.reason,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ActionIntentError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/reconcile")
    async def reconcile_action(
        intent_id: str,
        payload: ActionIntentReconcileRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.reconcile(
                intent_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ActionIntentError,
                    ActionProviderError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/action-intents/{intent_id}/rollback")
    async def rollback_action(
        intent_id: str,
        payload: ActionIntentRollbackRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.rollback(
                intent_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ActionIntentError,
                    ActionProviderError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    return router
