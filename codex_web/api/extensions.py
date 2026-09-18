from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.extension_packages import (
    ExtensionPackageCatalogError,
    ExtensionPackageNotFoundError,
    LocalExtensionPackageCatalog,
)
from codex_web.extensions import (
    ExtensionConfigureRequest,
    ExtensionGrantRequest,
    ExtensionGrantRevokeRequest,
    ExtensionHealthReport,
    ExtensionInstallRequest,
    ExtensionLifecycleRequest,
    ExtensionPackageInstallRequest,
    ExtensionPackageUpgradeRequest,
    ExtensionRemoveRequest,
    ExtensionUpgradeRequest,
)
from codex_web.services.extensions import (
    ExtensionAuthorizationError,
    ExtensionCompatibilityError,
    ExtensionConflictError,
    ExtensionError,
    ExtensionIntegrityError,
    ExtensionNotFoundError,
    ExtensionService,
)
from codex_web.services.identity import AuthorizationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ExtensionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ExtensionAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (ExtensionConflictError, ExtensionCompatibilityError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ExtensionIntegrityError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ExtensionError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_extensions_router(
    service: ExtensionService,
    package_catalog: LocalExtensionPackageCatalog | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/extensions", tags=["extensions"])

    @router.get("")
    async def list_extensions(request: Request) -> dict[str, Any]:
        items = service.list(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/packages")
    async def discover_packages(request: Request) -> dict[str, Any]:
        if package_catalog is None:
            raise HTTPException(
                status_code=503,
                detail="extension package catalog is unavailable",
            )
        try:
            service._require_admin_role(request_actor(request))
            discovery = package_catalog.discover()
            return {
                "items": [item.metadata() for item in discovery.candidates],
                "errors": [item.metadata() for item in discovery.errors],
            }
        except AuthorizationError as exc:
            raise _error(exc) from exc

    @router.post("/packages/{package_ref}/install")
    async def install_package(
        package_ref: str,
        payload: ExtensionPackageInstallRequest,
        request: Request,
    ) -> dict[str, Any]:
        if package_catalog is None:
            raise HTTPException(
                status_code=503,
                detail="extension package catalog is unavailable",
            )
        actor = request_actor(request)
        try:
            service._require_admin(actor)
            candidate = package_catalog.get(package_ref)
            item = service.install_verified_package(
                manifest=candidate.manifest,
                verification=candidate.verification,
                deployment_mode=payload.deployment_mode,
                actor=actor,
                package_ref=candidate.package_ref,
            )
            return {"item": item.model_dump(mode="json")}
        except ExtensionPackageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExtensionPackageCatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExtensionError,
                    AuthorizationError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("")
    async def install_extension(
        payload: ExtensionInstallRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.install(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExtensionError,
                    AuthorizationError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/events")
    async def extension_events(request: Request) -> dict[str, Any]:
        try:
            items = service.events(request_actor(request))
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.get("/{installation_id}")
    async def get_extension(
        installation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.get(installation_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.put("/{installation_id}/configuration")
    async def configure_extension(
        installation_id: str,
        payload: ExtensionConfigureRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.configure(
                installation_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExtensionError,
                    AuthorizationError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/{installation_id}/grants")
    async def list_grants(
        installation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            items = service.grants(
                installation_id,
                request_actor(request),
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/grants")
    async def grant_capabilities(
        installation_id: str,
        payload: ExtensionGrantRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            items = service.grant(
                installation_id,
                payload,
                actor=request_actor(request),
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExtensionError,
                    AuthorizationError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/grants/{grant_id}/revoke")
    async def revoke_capability(
        installation_id: str,
        grant_id: str,
        payload: ExtensionGrantRevokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.revoke_grant(
                installation_id,
                grant_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/enable")
    async def enable_extension(
        installation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.enable(
                installation_id,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/disable")
    async def disable_extension(
        installation_id: str,
        payload: ExtensionLifecycleRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.disable(
                installation_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/quarantine")
    async def quarantine_extension(
        installation_id: str,
        payload: ExtensionLifecycleRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.quarantine(
                installation_id,
                actor=request_actor(request),
                reason=payload.reason or "operator quarantine",
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/clear-quarantine")
    async def clear_quarantine(
        installation_id: str,
        payload: ExtensionLifecycleRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.clear_quarantine(
                installation_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/health")
    async def report_health(
        installation_id: str,
        payload: ExtensionHealthReport,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.report_health(
                installation_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError)):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/packages/{package_ref}/upgrade")
    async def upgrade_extension_package(
        installation_id: str,
        package_ref: str,
        payload: ExtensionPackageUpgradeRequest,
        request: Request,
    ) -> dict[str, Any]:
        if package_catalog is None:
            raise HTTPException(
                status_code=503,
                detail="extension package catalog is unavailable",
            )
        actor = request_actor(request)
        try:
            service._require_admin(actor)
            candidate = package_catalog.get(package_ref)
            item = service.upgrade_verified_package(
                installation_id,
                manifest=candidate.manifest,
                verification=candidate.verification,
                package_ref=candidate.package_ref,
                migration_evidence_id=payload.migration_evidence_id,
                actor=actor,
            )
            return {"item": item.model_dump(mode="json")}
        except ExtensionPackageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExtensionPackageCatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExtensionError,
                    AuthorizationError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/upgrade")
    async def upgrade_extension(
        installation_id: str,
        payload: ExtensionUpgradeRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.upgrade(
                installation_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ExtensionError,
                    AuthorizationError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/{installation_id}/remove")
    async def remove_extension(
        installation_id: str,
        payload: ExtensionRemoveRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.remove(
                installation_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ExtensionError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    return router
