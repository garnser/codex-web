from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationRecord,
    ConfigurationResetRequest,
    ConfigurationRollbackRequest,
    ConfigurationScope,
    FeatureTargeting,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.configuration import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationNotFoundError,
    ConfigurationService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.resources import ResourceCatalogService, ResourceNotFoundError


class ConfigurationDraftApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    value: Any
    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str | None = None
    feature_targeting: FeatureTargeting | None = None
    force_disabled: bool = False


class ConfigurationPublishApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str | None = None
    expected_active_revision: int | None = None


class ConfigurationRollbackApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    target_revision: int = Field(ge=1)
    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str | None = None
    expected_active_revision: int | None = None


class ConfigurationResetApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    reason: str | None = None
    expected_active_revision: int | None = None


class ConfigurationResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    context: ConfigurationContext = Field(default_factory=ConfigurationContext)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ConfigurationNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "configuration_not_found", "message": str(exc)},
        )
    if isinstance(exc, ConfigurationConflictError):
        return HTTPException(
            status_code=409,
            detail={"code": "configuration_conflict", "message": str(exc)},
        )
    if isinstance(exc, ConfigurationError):
        return HTTPException(
            status_code=422,
            detail={"code": "configuration_invalid", "message": str(exc)},
        )
    return HTTPException(
        status_code=400,
        detail={"code": "configuration_error", "message": str(exc)},
    )


def build_configuration_router(
    service: ConfigurationService,
    projects: ProjectService | None = None,
    resources: ResourceCatalogService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/configuration", tags=["configuration"])

    def authenticated(request: Request) -> AuthenticationActor:
        return request_actor(request)

    def project_visible(project_id: str | None, actor: AuthenticationActor) -> bool:
        if not project_id or projects is None:
            return False
        try:
            projects.get(project_id, actor.tenant)
        except ProjectNotFoundError:
            return False
        return True

    def resource_visible(resource_id: str | None, actor: AuthenticationActor) -> bool:
        if not resource_id or resources is None:
            return False
        try:
            resources.get(resource_id, actor)
        except ResourceNotFoundError:
            return False
        return True

    def record_visible(record: ConfigurationRecord, actor: AuthenticationActor) -> bool:
        if record.scope_type in {ConfigurationScope.DEPLOYMENT, ConfigurationScope.GLOBAL}:
            return True
        if record.scope_type == ConfigurationScope.ORGANIZATION:
            return record.scope_id == actor.organization_id
        if record.scope_type == ConfigurationScope.WORKSPACE:
            return record.scope_id == actor.workspace_id
        if record.scope_type == ConfigurationScope.PROJECT:
            return project_visible(record.scope_id, actor)
        if record.scope_type == ConfigurationScope.RESOURCE:
            return resource_visible(record.scope_id, actor)
        return False

    def require_visible(record: ConfigurationRecord, actor: AuthenticationActor) -> None:
        if not record_visible(record, actor):
            raise HTTPException(status_code=404, detail="configuration record not found")

    def require_scope(
        actor: AuthenticationActor,
        *,
        scope_type: ConfigurationScope,
        scope_id: str | None,
        mutation: bool,
    ) -> str | None:
        platform_scope = scope_type in {
            ConfigurationScope.DEPLOYMENT,
            ConfigurationScope.GLOBAL,
        }

        if mutation:
            if actor.principal_kind == PrincipalKind.SERVICE:
                required = (
                    "configuration:global-admin"
                    if platform_scope
                    else "configuration:admin"
                )
                if required not in actor.service_scopes:
                    raise AuthorizationError(f"{required} service scope required")
            else:
                IdentityService.require_admin(actor)
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
                if platform_scope and actor.assurance != AuthenticationAssurance.LOCAL_TRUSTED:
                    raise AuthorizationError(
                        "deployment/global configuration mutation requires local-trusted platform context"
                    )

        if platform_scope:
            if scope_id not in {None, ""}:
                raise AuthorizationError(
                    f"{scope_type.value} configuration cannot specify scope_id"
                )
            return None
        if scope_type == ConfigurationScope.ORGANIZATION:
            if scope_id not in {None, actor.organization_id}:
                raise AuthorizationError("cross-tenant organization configuration denied")
            return actor.organization_id
        if scope_type == ConfigurationScope.WORKSPACE:
            if scope_id not in {None, actor.workspace_id}:
                raise AuthorizationError("cross-tenant workspace configuration denied")
            return actor.workspace_id
        if scope_type == ConfigurationScope.PROJECT:
            if projects is None:
                raise AuthorizationError(
                    "project configuration requires canonical ProjectService"
                )
            if not project_visible(scope_id, actor):
                raise AuthorizationError("cross-tenant project configuration denied")
            return scope_id
        if scope_type == ConfigurationScope.RESOURCE:
            if resources is None:
                raise AuthorizationError(
                    "resource configuration requires canonical ResourceCatalogService"
                )
            if not resource_visible(scope_id, actor):
                raise AuthorizationError("cross-tenant resource configuration denied")
            return scope_id
        raise AuthorizationError("unsupported configuration scope")

    def tenant_context(
        context: ConfigurationContext,
        actor: AuthenticationActor,
    ) -> ConfigurationContext:
        if context.organization_id not in {None, actor.organization_id}:
            raise HTTPException(
                status_code=403,
                detail="cross-tenant configuration context denied",
            )
        if context.workspace_id not in {None, actor.workspace_id}:
            raise HTTPException(
                status_code=403,
                detail="cross-tenant configuration context denied",
            )
        if context.project_id is not None and not project_visible(context.project_id, actor):
            raise HTTPException(
                status_code=403,
                detail="cross-tenant project configuration context denied",
            )
        if context.resource_id is not None and not resource_visible(context.resource_id, actor):
            raise HTTPException(
                status_code=403,
                detail="cross-tenant resource configuration context denied",
            )
        return context.model_copy(
            update={
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
            }
        )

    @router.get("/specs")
    async def list_specs(request: Request) -> dict[str, Any]:
        authenticated(request)
        items = [spec.model_dump(mode="json") for spec in service.list_specs()]
        return {"items": items, "count": len(items)}

    @router.get("/records")
    async def list_records(
        request: Request,
        key: str | None = None,
        scope_type: ConfigurationScope | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        normalized_scope_id = scope_id
        try:
            if scope_type is not None:
                normalized_scope_id = require_scope(
                    actor,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    mutation=False,
                )
            records = service.list_records(
                key=key,
                scope_type=scope_type,
                scope_id=normalized_scope_id,
            )
        except (AuthorizationError, ConfigurationError, ValueError) as exc:
            raise _error(exc) from exc
        items = [
            record.model_dump(mode="json")
            for record in records
            if record_visible(record, actor)
        ]
        return {"items": items, "count": len(items)}

    @router.post("/drafts")
    async def create_draft(
        payload: ConfigurationDraftApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            scope_id = require_scope(
                actor,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                mutation=True,
            )
            record = service.create_draft(
                ConfigurationDraftCreate(
                    key=payload.key,
                    scope_type=payload.scope_type,
                    scope_id=scope_id,
                    value=payload.value,
                    actor=actor.identity_id,
                    reason=payload.reason,
                    feature_targeting=payload.feature_targeting,
                    force_disabled=payload.force_disabled,
                )
            )
        except (
            AuthorizationError,
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/{record_id}/validate")
    async def validate_record(record_id: str, request: Request) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            require_visible(service.get_record(record_id), actor)
            return service.validate_record(record_id)
        except (
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/publish")
    async def publish_record(
        record_id: str,
        payload: ConfigurationPublishApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            selected = service.get_record(record_id)
            require_visible(selected, actor)
            require_scope(
                actor,
                scope_type=selected.scope_type,
                scope_id=selected.scope_id,
                mutation=True,
            )
            record = service.publish(
                record_id,
                ConfigurationPublishRequest(
                    actor=actor.identity_id,
                    reason=payload.reason,
                    expected_active_revision=payload.expected_active_revision,
                ),
            )
        except (
            AuthorizationError,
            ConfigurationConflictError,
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/rollback")
    async def rollback(
        payload: ConfigurationRollbackApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            scope_id = require_scope(
                actor,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                mutation=True,
            )
            record = service.rollback(
                ConfigurationRollbackRequest(
                    key=payload.key,
                    scope_type=payload.scope_type,
                    scope_id=scope_id,
                    target_revision=payload.target_revision,
                    actor=actor.identity_id,
                    reason=payload.reason,
                    expected_active_revision=payload.expected_active_revision,
                )
            )
        except (
            AuthorizationError,
            ConfigurationConflictError,
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/reset")
    async def reset_override(
        payload: ConfigurationResetApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            scope_id = require_scope(
                actor,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                mutation=True,
            )
            record = service.reset_override(
                ConfigurationResetRequest(
                    key=payload.key,
                    scope_type=payload.scope_type,
                    scope_id=scope_id,
                    actor=actor.identity_id,
                    reason=payload.reason,
                    expected_active_revision=payload.expected_active_revision,
                )
            )
        except (
            AuthorizationError,
            ConfigurationConflictError,
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/resolve")
    async def resolve(
        payload: ConfigurationResolveRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        context = tenant_context(payload.context, actor)
        try:
            effective = service.resolve(payload.key, context)
        except (ConfigurationError, ConfigurationNotFoundError, ValueError) as exc:
            raise _error(exc) from exc
        return {"effective": effective.model_dump(mode="json")}

    @router.get("/{record_id}/impact")
    async def impact(record_id: str, request: Request) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            require_visible(service.get_record(record_id), actor)
            result = service.impact(record_id)
            result["more_specific_overrides"] = [
                item
                for item in result.get("more_specific_overrides", [])
                if record_visible(service.get_record(item["record_id"]), actor)
            ]
            return result
        except (
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc

    return router
