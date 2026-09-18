from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublishRequest,
    DefinitionRecord,
    DefinitionRollbackRequest,
    DefinitionScope,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.definitions import (
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
    DefinitionRegistryService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService


class DefinitionDraftApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    definition_schema_version: str = Field(min_length=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    payload: dict[str, Any]
    # Accepted only for compatibility with the original API shape. Attribution
    # is always overwritten with the authenticated actor.
    actor: str | None = None
    reason: str | None = None
    effective_from: float | None = None
    effective_until: float | None = None
    min_engine_version: str | None = None
    max_engine_version: str | None = None


class DefinitionValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str | None = None


class DefinitionPublishApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str | None = None
    reason: str | None = None
    expected_active_revision: int | None = None
    approval_metadata: dict[str, str] = Field(default_factory=dict)


class DefinitionQuarantineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str | None = None
    reason: str = Field(min_length=1)


class DefinitionRollbackApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    target_revision: int = Field(ge=1)
    actor: str | None = None
    reason: str | None = None
    expected_active_revision: int | None = None


class DefinitionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str | None = None
    document: dict[str, Any]


class DefinitionResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    context: DefinitionContext = Field(default_factory=DefinitionContext)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, DefinitionNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "definition_not_found", "message": str(exc)},
        )
    if isinstance(exc, DefinitionConflictError):
        return HTTPException(
            status_code=409,
            detail={"code": "definition_conflict", "message": str(exc)},
        )
    if isinstance(exc, DefinitionCompatibilityError):
        return HTTPException(
            status_code=409,
            detail={"code": "definition_incompatible", "message": str(exc)},
        )
    return HTTPException(
        status_code=422,
        detail={"code": "definition_invalid", "message": str(exc)},
    )


def build_definitions_router(
    service: DefinitionRegistryService,
    project_service: ProjectService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/definitions", tags=["definitions"])

    def admin_actor(request: Request, *, mutation: bool = False):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "definitions:admin" not in actor.service_scopes:
                raise AuthorizationError("definitions:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        if mutation:
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    def project_visible(project_id: str | None, actor) -> bool:
        if not project_id or project_service is None:
            return False
        try:
            project_service.get(project_id, actor.tenant)
            return True
        except ProjectNotFoundError:
            return False

    def normalized_scope(
        scope_type: DefinitionScope,
        scope_id: str | None,
        actor,
        *,
        mutation: bool,
    ) -> str | None:
        if scope_type == DefinitionScope.GLOBAL:
            if (
                mutation
                and actor.principal_kind != PrincipalKind.SERVICE
                and actor.assurance != AuthenticationAssurance.LOCAL_TRUSTED
            ):
                raise AuthorizationError(
                    "platform-global definition mutation requires local-trusted "
                    "human assurance or definitions:admin service identity"
                )
            return None
        if scope_type == DefinitionScope.ORGANIZATION:
            if scope_id is not None and scope_id != actor.organization_id:
                raise AuthorizationError("cross-organization definition scope denied")
            return actor.organization_id
        if scope_type == DefinitionScope.WORKSPACE:
            if scope_id is not None and scope_id != actor.workspace_id:
                raise AuthorizationError("cross-workspace definition scope denied")
            return actor.workspace_id
        if not scope_id or not project_visible(scope_id, actor):
            raise AuthorizationError("project definition scope denied")
        return scope_id

    def record_visible(record: DefinitionRecord, actor) -> bool:
        if record.scope_type == DefinitionScope.GLOBAL:
            return True
        if record.scope_type == DefinitionScope.ORGANIZATION:
            return record.scope_id == actor.organization_id
        if record.scope_type == DefinitionScope.WORKSPACE:
            return record.scope_id == actor.workspace_id
        return project_visible(record.scope_id, actor)

    def require_record(record_id: str, actor, *, mutation: bool) -> DefinitionRecord:
        record = service.get_record(record_id)
        if not record_visible(record, actor):
            raise DefinitionNotFoundError(f"definition record not found: {record_id}")
        normalized_scope(
            record.scope_type,
            record.scope_id,
            actor,
            mutation=mutation,
        )
        return record

    def tenant_context(context: DefinitionContext, actor) -> DefinitionContext:
        if (
            context.organization_id is not None
            and context.organization_id != actor.organization_id
        ):
            raise AuthorizationError("cross-organization definition context denied")
        if (
            context.workspace_id is not None
            and context.workspace_id != actor.workspace_id
        ):
            raise AuthorizationError("cross-workspace definition context denied")
        if context.project_id is not None and not project_visible(context.project_id, actor):
            raise AuthorizationError("project definition context denied")
        return context.model_copy(
            update={
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
            }
        )

    def visible_usage(items: list[dict[str, Any]], actor) -> list[dict[str, Any]]:
        visible: list[dict[str, Any]] = []
        for item in items:
            organization_id = item.get("organization_id")
            workspace_id = item.get("workspace_id")
            project_id = item.get("project_id")
            if organization_id is not None and organization_id != actor.organization_id:
                continue
            if workspace_id is not None and workspace_id != actor.workspace_id:
                continue
            if project_id is not None and not project_visible(str(project_id), actor):
                continue
            # Usage without any canonical scope cannot be proven tenant-safe.
            if organization_id is None and workspace_id is None and project_id is None:
                continue
            visible.append(item)
        return visible

    @router.get("/schemas")
    async def schemas(request: Request) -> dict[str, Any]:
        try:
            admin_actor(request)
            items = service.schemas.metadata()
            return {"items": items, "count": len(items)}
        except AuthorizationError as exc:
            raise _error(exc) from exc

    @router.get("/bootstrap")
    async def bootstrap_status(request: Request) -> dict[str, Any]:
        try:
            actor = admin_actor(request)
            status = service.bootstrap_status()
            records = [
                item for item in service.list_records() if record_visible(item, actor)
            ]
            return {
                **status,
                "records": len(records),
                "active": sum(
                    item.lifecycle == DefinitionLifecycle.PUBLISHED for item in records
                ),
            }
        except AuthorizationError as exc:
            raise _error(exc) from exc

    @router.get("/records")
    async def records(
        request: Request,
        kind: str | None = None,
        definition_id: str | None = None,
        scope_type: DefinitionScope | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request)
            normalized_scope_id = scope_id
            if scope_type is not None:
                normalized_scope_id = normalized_scope(
                    scope_type,
                    scope_id,
                    actor,
                    mutation=False,
                )
            items = service.list_records(
                kind=kind,
                definition_id=definition_id,
                scope_type=scope_type,
                scope_id=normalized_scope_id,
            )
            items = [item for item in items if record_visible(item, actor)]
        except (AuthorizationError, DefinitionError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @router.post("/drafts")
    async def create_draft(
        payload: DefinitionDraftApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request, mutation=True)
            scope_id = normalized_scope(
                payload.scope_type,
                payload.scope_id,
                actor,
                mutation=True,
            )
            record = service.create_draft(
                DefinitionDraftCreate(
                    definition_id=payload.definition_id,
                    kind=payload.kind,
                    definition_schema_version=payload.definition_schema_version,
                    scope_type=payload.scope_type,
                    scope_id=scope_id,
                    payload=payload.payload,
                    actor=actor.identity_id,
                    reason=payload.reason,
                    effective_from=payload.effective_from,
                    effective_until=payload.effective_until,
                    min_engine_version=payload.min_engine_version,
                    max_engine_version=payload.max_engine_version,
                )
            )
        except Exception as exc:
            if isinstance(
                exc,
                (
                    AuthorizationError,
                    DefinitionError,
                    DefinitionNotFoundError,
                    DefinitionConflictError,
                    DefinitionCompatibilityError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise
        return {"record": record.model_dump(mode="json")}

    @router.post("/{record_id}/validate")
    async def validate(
        record_id: str,
        payload: DefinitionValidateRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request, mutation=True)
            require_record(record_id, actor, mutation=True)
            record = service.validate(record_id, actor=actor.identity_id)
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/{record_id}/publish")
    async def publish(
        record_id: str,
        payload: DefinitionPublishApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request, mutation=True)
            require_record(record_id, actor, mutation=True)
            record = service.publish(
                record_id,
                DefinitionPublishRequest(
                    actor=actor.identity_id,
                    reason=payload.reason,
                    expected_active_revision=payload.expected_active_revision,
                    approval_metadata=payload.approval_metadata,
                ),
            )
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/{record_id}/quarantine")
    async def quarantine(
        record_id: str,
        payload: DefinitionQuarantineRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request, mutation=True)
            require_record(record_id, actor, mutation=True)
            record = service.quarantine(
                record_id,
                actor=actor.identity_id,
                reason=payload.reason,
            )
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/rollback")
    async def rollback(
        payload: DefinitionRollbackApiRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request, mutation=True)
            scope_id = normalized_scope(
                payload.scope_type,
                payload.scope_id,
                actor,
                mutation=True,
            )
            record = service.rollback(
                DefinitionRollbackRequest(
                    definition_id=payload.definition_id,
                    kind=payload.kind,
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
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/resolve")
    async def resolve(
        payload: DefinitionResolveRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request)
            context = tenant_context(payload.context, actor)
            record = service.resolve(
                definition_id=payload.definition_id,
                kind=payload.kind,
                context=context,
            )
            if not record_visible(record, actor):
                raise DefinitionNotFoundError(
                    f"no active definition for {payload.kind}:{payload.definition_id}"
                )
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.get("/{record_id}/usage")
    async def usage(record_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = admin_actor(request)
            require_record(record_id, actor, mutation=False)
            result = service.usage(record_id)
            items = visible_usage(result.get("items", []), actor)
            return {**result, "items": items, "count": len(items)}
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc

    @router.get("/diff")
    async def diff(left: str, right: str, request: Request) -> dict[str, Any]:
        try:
            actor = admin_actor(request)
            require_record(left, actor, mutation=False)
            require_record(right, actor, mutation=False)
            return service.diff(left, right)
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc

    @router.get("/export")
    async def export(request: Request, kind: str | None = None) -> dict[str, Any]:
        try:
            actor = admin_actor(request)
            items = [
                item
                for item in service.list_records(kind=kind)
                if record_visible(item, actor)
            ]
            return {
                "format": "codex-web-definitions",
                "version": "1.0",
                "records": [item.model_dump(mode="json") for item in items],
            }
        except AuthorizationError as exc:
            raise _error(exc) from exc

    @router.post("/import")
    async def import_definitions(
        payload: DefinitionImportRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin_actor(request, mutation=True)
            raw_records = payload.document.get("records", [])
            for raw in raw_records:
                source = DefinitionRecord.model_validate(raw)
                normalized_scope(
                    source.scope_type,
                    source.scope_id,
                    actor,
                    mutation=True,
                )
            records = service.import_records(
                payload.document,
                actor=actor.identity_id,
            )
        except (
            AuthorizationError,
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {
            "items": [record.model_dump(mode="json") for record in records],
            "count": len(records),
        }

    return router
