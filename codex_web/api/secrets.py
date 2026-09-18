from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
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


def _metadata(reference) -> dict[str, Any]:
    return {
        **reference.model_dump(mode="json"),
        "status": reference.status().value,
    }


def _broker_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SecretNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (SecretUseDeniedError, SecretRevealDeniedError, IdentityError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SecretBrokerError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_secrets_router(broker: SecretBroker) -> APIRouter:
    router = APIRouter(tags=["secrets"])

    def sensitive_admin(request: Request):
        actor = request_actor(request)
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/api/secrets")
    async def list_secrets(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {"items": [_metadata(item) for item in broker.list(actor)]}

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
