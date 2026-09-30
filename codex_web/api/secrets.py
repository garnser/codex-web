from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from codex_web.api.identity import request_actor
from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.services.projects import ProjectService
from codex_web.identity import AuthenticationAssurance
from codex_web.secrets import SecretCreate, SecretRotate
from codex_web.services.identity import IdentityError, IdentityService, identity_http_error
from codex_web.services.secrets import (
    SecretBroker,
    SecretBrokerError,
    SecretNotFoundError,
    SecretRevealDeniedError,
    SecretUseDeniedError,
)


class SecretMetadataRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Framework validation includes rejected input by default. A
                # malformed write-only credential must never be echoed back.
                raise HTTPException(status_code=422, detail='Invalid secret request. Review the metadata and re-enter the write-only value.') from None
        return safe_handler


def _metadata(reference, actor=None, broker=None) -> dict[str, Any]:
    return {
        **reference.model_dump(mode="json"),
        "status": reference.status().value,
        "scope_type": "workspace",
        "use_allowed": broker._can_use(actor, reference) if actor is not None else None,
        "reveal_api_available": False,
    }


def _broker_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SecretNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (SecretUseDeniedError, SecretRevealDeniedError, IdentityError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SecretBrokerError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_secrets_router(broker: SecretBroker, projects: ProjectService | None = None) -> APIRouter:
    router = APIRouter(tags=["secrets"], route_class=SecretMetadataRoute,
                      dependencies=[Depends(project_record_context_parameter)])

    def context(request):
        actor = request_actor(request)
        view = ProjectRecordScope.from_request(request, actor, projects)
        return actor, view

    def sensitive_admin(request: Request):
        actor, _ = context(request)
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/api/secrets")
    async def list_secrets(request: Request) -> dict[str, Any]:
        actor, view = context(request)
        items = [_metadata(item, actor, broker) for item in broker.list(actor)]
        result = {"items": items, "project_id": view.project_id, "scope_type": "workspace",
                  "broken_references": [], "impact_available": False}
        if view.project_id is not None and broker.usage_service is not None:
            try:
                index = broker.usage_service.index(view.project_id, actor)
                visible = {item['id']: item for item in items}
                for item in items:
                    references = index.get(item['id'], [])
                    item['project_reference_count'] = sum(row['visible'] and row['project_id'] == view.project_id for row in references)
                    item['shared_reference_count'] = sum(row['scope'] == 'shared_workspace' for row in references)
                result['broken_references'] = [
                    {'secret_id': secret_id, 'status': visible[secret_id]['status'] if secret_id in visible else 'missing_or_unavailable',
                     'consumers': [row for row in rows if row['visible']][:100]}
                    for secret_id, rows in index.items()
                    if any(row['visible'] for row in rows)
                    and (secret_id not in visible or visible[secret_id]['status'] != 'active')
                ][:100]
                result['impact_available'] = True
            except ValueError:
                result['impact_error'] = 'Consumer impact is unavailable; retry before a lifecycle change.'
        return result

    @router.get('/api/secrets/{secret_id}/usage')
    async def secret_usage(secret_id: str, project_id: str, request: Request):
        actor, _ = context(request)
        try:
            broker.metadata(secret_id, actor=actor)
        except (SecretBrokerError, IdentityError):
            raise HTTPException(status_code=404, detail='Secret reference not found') from None
        if broker.usage_service is None:
            raise HTTPException(status_code=503, detail='Secret consumer impact is unavailable')
        try:
            return broker.usage_service.snapshot(secret_id, project_id, actor)
        except ValueError:
            raise HTTPException(status_code=503, detail='Secret consumer impact exceeds its bounded scan or is unavailable') from None

    @router.post("/api/secrets")
    async def create_secret(payload: SecretCreate, request: Request) -> dict[str, Any]:
        try:
            actor = sensitive_admin(request)
            reference = broker.create(payload, actor=actor)
            return {"item": _metadata(reference)}
        except Exception as exc:
            if isinstance(exc, (SecretBrokerError, IdentityError)):
                raise _broker_error(exc) from exc
            raise

    @router.post("/api/secrets/{secret_id}/rotate")
    async def rotate_secret(
        secret_id: str,
        payload: SecretRotate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = sensitive_admin(request)
            reference = broker.rotate(secret_id, payload, actor=actor)
            return {"item": _metadata(reference)}
        except Exception as exc:
            if isinstance(exc, (SecretBrokerError, IdentityError)):
                raise _broker_error(exc) from exc
            raise

    @router.delete("/api/secrets/{secret_id}")
    async def revoke_secret(secret_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = sensitive_admin(request)
            reference = broker.revoke(
                secret_id,
                actor=actor,
                reason=f"revoked-by:{actor.identity_id}",
            )
            return {"item": _metadata(reference)}
        except Exception as exc:
            if isinstance(exc, (SecretBrokerError, IdentityError)):
                raise _broker_error(exc) from exc
            raise

    @router.get("/api/secrets/audit")
    async def secret_audit(request: Request) -> dict[str, Any]:
        try:
            actor = sensitive_admin(request)
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in broker.audit(actor)
                ]
            }
        except Exception as exc:
            if isinstance(exc, (SecretBrokerError, IdentityError)):
                raise _broker_error(exc) from exc
            raise

    return router
