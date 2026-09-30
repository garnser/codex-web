from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.crypto import KeyManifestEntry, ManagedKeyCreate
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.crypto_keys import (
    CryptoDecryptError,
    CryptoKeyConflictError,
    CryptoKeyImpactUnavailableError,
    CryptoKeyError,
    CryptoKeyNotFoundError,
    CryptoKeyService,
)
from codex_web.services.identity import AuthorizationError, IdentityService, TenantIsolationError


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class ManifestValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: tuple[KeyManifestEntry, ...]


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CryptoKeyImpactUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, CryptoKeyNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (CryptoKeyConflictError, CryptoDecryptError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (CryptoKeyError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_crypto_keys_router(service: CryptoKeyService, projects=None, resources=None) -> APIRouter:
    router = APIRouter(prefix="/api/crypto", tags=["crypto"],
                      dependencies=[Depends(project_record_context_parameter)])

    def context(request: Request):
        actor = request_actor(request)
        try:
            service._require_admin(actor)
        except AuthorizationError as exc:
            raise _error(exc) from exc
        return actor, ProjectRecordScope.from_request(request, actor, projects, resources)

    router.dependencies.append(Depends(context))

    def visible(key, view):
        return ((key.scope.project_id is None or view.includes('project', key.scope.project_id))
                and (key.scope.resource_id is None or view.includes('resource', key.scope.resource_id)))

    def scoped_key(key_id, request):
        actor, view = context(request)
        key = service.get_key(key_id, actor)
        if not visible(key, view):
            raise HTTPException(status_code=404, detail='Key not found in Project')
        return key

    def mutation_actor(request: Request):
        actor, _ = context(request)
        if request.path_params.get('key_id'):
            scoped_key(request.path_params['key_id'], request)
        if actor.principal_kind != PrincipalKind.SERVICE:
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/keys")
    async def list_keys(request: Request) -> dict[str, Any]:
        actor, view = context(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list_keys(actor) if visible(item, view)
            ], 'project_id': view.project_id, 'scope_type': 'workspace',
        }

    @router.post("/keys")
    async def create_key(payload: ManagedKeyCreate, request: Request) -> dict[str, Any]:
        try:
            _, view = context(request)
            if payload.project_id is not None:
                view.require('project', payload.project_id)
            if payload.resource_id is not None:
                view.require('resource', payload.resource_id)
            item = service.create_key(payload, actor=mutation_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (CryptoKeyError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/keys/{key_id}")
    async def get_key(key_id: str, request: Request) -> dict[str, Any]:
        try:
            item = scoped_key(key_id, request)
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (CryptoKeyError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get('/keys/{key_id}/usage')
    async def key_usage(key_id: str, request: Request, version: int | None = None):
        try:
            scoped_key(key_id, request)
            return service.usage(key_id, request_actor(request), version=version)
        except (CryptoKeyError, AuthorizationError, TenantIsolationError) as exc:
            raise _error(exc) from exc

    @router.post("/keys/{key_id}/rotate")
    async def rotate_key(key_id: str, request: Request) -> dict[str, Any]:
        try:
            result = service.rotate(key_id, actor=mutation_actor(request))
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
                actor=mutation_actor(request),
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
                actor=mutation_actor(request),
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
        actor, view = context(request)
        allowed = {item.id for item in service.list_keys(actor) if visible(item, view)}
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.manifest(request_actor(request))
                if item.key_id in allowed
            ]
        }

    @router.post("/manifest/validate")
    async def validate_manifest(
        payload: ManifestValidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            _, view = context(request)
            if view.project_id is not None:
                for entry in payload.entries:
                    scoped_key(entry.key_id, request)
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
            actor, view = context(request)
            allowed = {item.id for item in service.list_keys(actor) if visible(item, view)}
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in service.events(actor) if view.project_id is None or item.key_id in allowed
                ]
            }
        except Exception as exc:
            if isinstance(exc, (CryptoKeyError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    return router
