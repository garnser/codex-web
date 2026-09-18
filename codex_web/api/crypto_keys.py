from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.crypto import KeyManifestEntry, ManagedKeyCreate
from codex_web.services.crypto_keys import (
    CryptoDecryptError,
    CryptoKeyConflictError,
    CryptoKeyError,
    CryptoKeyNotFoundError,
    CryptoKeyService,
)
from codex_web.services.identity import AuthorizationError, TenantIsolationError


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class ManifestValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: tuple[KeyManifestEntry, ...]


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CryptoKeyNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (CryptoKeyConflictError, CryptoDecryptError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (CryptoKeyError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_crypto_keys_router(service: CryptoKeyService) -> APIRouter:
    router = APIRouter(prefix="/api/crypto", tags=["crypto"])

    @router.get("/keys")
    async def list_keys(request: Request) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list_keys(request_actor(request))
            ]
        }

    @router.post("/keys")
    async def create_key(payload: ManagedKeyCreate, request: Request) -> dict[str, Any]:
        try:
            item = service.create_key(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (CryptoKeyError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/keys/{key_id}")
    async def get_key(key_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get_key(key_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (CryptoKeyError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/keys/{key_id}/rotate")
    async def rotate_key(key_id: str, request: Request) -> dict[str, Any]:
        try:
            result = service.rotate(key_id, actor=request_actor(request))
            return result.model_dump(mode="json")
        except Exception as exc:
            if isinstance(
                exc,
                (CryptoKeyError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/keys/{key_id}/revoke")
    async def revoke_key(
        key_id: str,
        payload: RevokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.revoke_key(
                key_id,
                payload.reason,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (CryptoKeyError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/keys/{key_id}/versions/{version}/revoke")
    async def revoke_version(
        key_id: str,
        version: int,
        payload: RevokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.revoke_version(
                key_id,
                version,
                payload.reason,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (CryptoKeyError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/backend-health")
    async def backend_health(request: Request) -> dict[str, bool]:
        try:
            return service.backend_health(request_actor(request))
        except Exception as exc:
            if isinstance(exc, (CryptoKeyError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.get("/manifest")
    async def manifest(request: Request) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.manifest(request_actor(request))
            ]
        }

    @router.post("/manifest/validate")
    async def validate_manifest(
        payload: ManifestValidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.validate_manifest(
                payload.entries,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except Exception as exc:
            if isinstance(
                exc,
                (CryptoKeyError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/events")
    async def events(request: Request) -> dict[str, Any]:
        try:
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in service.events(request_actor(request))
                ]
            }
        except Exception as exc:
            if isinstance(exc, (CryptoKeyError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    return router
