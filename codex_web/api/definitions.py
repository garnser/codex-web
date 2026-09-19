from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    DefinitionRecord,
    DefinitionRollbackRequest,
    DefinitionScope,
)
from codex_web.identity import (
    AuthenticationAssurance,
    AuthenticationActor,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.definitions import (
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
    DefinitionRegistryService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService


class DefinitionDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    definition_schema_version: str = Field(min_length=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    payload: dict[str, Any]
    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str | None = None
    effective_from: float | None = None
    effective_until: float | None = None
    min_engine_version: str | None = None
    max_engine_version: str | None = None


class DefinitionValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str | None = None  # legacy compatibility; authenticated actor wins.


class DefinitionPublishHttpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str | None = None
    expected_active_revision: int | None = None
    approval_metadata: dict[str, str] = Field(default_factory=dict)
    publication_approval_id: str | None = None


class DefinitionPublicationApprovalHttpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1)


class DefinitionRollbackHttpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    target_revision: int = Field(ge=1)
    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str | None = None
    expected_active_revision: int | None = None


class DefinitionQuarantineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    reason: str = Field(min_length=1)


class DefinitionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str | None = None  # legacy compatibility; authenticated actor wins.
    document: dict[str, Any]


class DefinitionResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    context: DefinitionContext = Field(default_factory=DefinitionContext)


def _error(exc: Exception) -> HTTPException:
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
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(
        status_code=422,
        detail={"code": "definition_invalid", "message": str(exc)},
    )


def build_definitions_router(
    service: DefinitionRegistryService,
    projects: ProjectService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/definitions", tags=["definitions"])

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

    def record_visible(record: DefinitionRecord, actor: AuthenticationActor) -> bool:
        if record.scope_type == DefinitionScope.GLOBAL:
            return True
        if record.scope_type == DefinitionScope.ORGANIZATION:
            return record.scope_id == actor.organization_id
        if record.scope_type == DefinitionScope.WORKSPACE:
            return record.scope_id == actor.workspace_id
        if record.scope_type == DefinitionScope.PROJECT:
            return project_visible(record.scope_id, actor)
        return False

    def require_visible(record: DefinitionRecord, actor: AuthenticationActor) -> None:
        if not record_visible(record, actor):
            raise HTTPException(status_code=404, detail="definition record not found")

    def require_mutation_actor(
        actor: AuthenticationActor,
        *,
        scope_type: DefinitionScope,
        scope_id: str | None,
    ) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            required = (
                "definitions:global-admin"
                if scope_type == DefinitionScope.GLOBAL
                else "definitions:admin"
            )
            if required not in actor.service_scopes:
                raise AuthorizationError(f"{required} service scope required")
        else:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            if (
                scope_type == DefinitionScope.GLOBAL
                and actor.assurance != AuthenticationAssurance.LOCAL_TRUSTED
            ):
                raise AuthorizationError(
                    "global definition mutation requires local-trusted platform context"
                )

        if scope_type == DefinitionScope.GLOBAL:
            if scope_id is not None:
                raise AuthorizationError("global definition scope cannot specify scope_id")
            return
        if scope_type == DefinitionScope.ORGANIZATION:
            if scope_id != actor.organization_id:
                raise AuthorizationError("cross-tenant organization definition denied")
            return
        if scope_type == DefinitionScope.WORKSPACE:
            if scope_id != actor.workspace_id:
                raise AuthorizationError("cross-tenant workspace definition denied")
            return
        if scope_type == DefinitionScope.PROJECT:
            if projects is None:
                raise AuthorizationError(
                    "project definition mutation requires canonical ProjectService"
                )
            if not project_visible(scope_id, actor):
                raise AuthorizationError("cross-tenant project definition denied")
            return
        raise AuthorizationError("unsupported definition scope")

    def require_approval_actor(
        actor: AuthenticationActor,
        *,
        scope_type: DefinitionScope,
        scope_id: str | None,
    ) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            required = (
                "definitions:global-approve"
                if scope_type == DefinitionScope.GLOBAL
                else "definitions:approve"
            )
            if required not in actor.service_scopes:
                raise AuthorizationError(f"{required} service scope required")
        else:
            if not actor.has_role(MembershipRole.OWNER, MembershipRole.APPROVER):
                raise AuthorizationError(
                    "definition publication approval requires approver role"
                )
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
            if (
                scope_type == DefinitionScope.GLOBAL
                and actor.assurance != AuthenticationAssurance.LOCAL_TRUSTED
            ):
                raise AuthorizationError(
                    "global definition approval requires local-trusted platform context"
                )
        if scope_type == DefinitionScope.GLOBAL:
            return
        if scope_type == DefinitionScope.ORGANIZATION:
            if scope_id != actor.organization_id:
                raise AuthorizationError(
                    "cross-tenant organization definition approval denied"
                )
            return
        if scope_type == DefinitionScope.WORKSPACE:
            if scope_id != actor.workspace_id:
                raise AuthorizationError(
                    "cross-tenant workspace definition approval denied"
                )
            return
        if scope_type == DefinitionScope.PROJECT:
            if not project_visible(scope_id, actor):
                raise AuthorizationError(
                    "cross-tenant project definition approval denied"
                )
            return
        raise AuthorizationError("unsupported definition approval scope")

    def tenant_context(
        context: DefinitionContext,
        actor: AuthenticationActor,
    ) -> DefinitionContext:
        if context.organization_id not in {None, actor.organization_id}:
            raise HTTPException(status_code=403, detail="cross-tenant definition context denied")
        if context.workspace_id not in {None, actor.workspace_id}:
            raise HTTPException(status_code=403, detail="cross-tenant definition context denied")
        if context.project_id is not None and not project_visible(context.project_id, actor):
            raise HTTPException(status_code=403, detail="cross-tenant project definition context denied")
        return context.model_copy(
            update={
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
            }
        )

    def visible_usage(items: list[dict[str, Any]], actor: AuthenticationActor) -> list[dict[str, Any]]:
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
            if organization_id is None and workspace_id is None and project_id is None:
                continue
            visible.append(item)
        return visible

    def visible_records(actor: AuthenticationActor, **filters: Any) -> list[DefinitionRecord]:
        return [
            item
            for item in service.list_records(**filters)
            if record_visible(item, actor)
        ]

    @router.get("/schemas")
    async def schemas(request: Request) -> dict[str, Any]:
        authenticated(request)
        items = service.schemas.metadata()
        return {"items": items, "count": len(items)}

    @router.get("/bootstrap")
    async def bootstrap_status(request: Request) -> dict[str, Any]:
        actor = authenticated(request)
        status = service.bootstrap_status()
        records = visible_records(actor)
        active = [item for item in records if item.lifecycle.value == "published"]
        return {
            **status,
            "records": len(records),
            "active": len(active),
        }

    @router.get("/records")
    async def records(
        request: Request,
        kind: str | None = None,
        definition_id: str | None = None,
        scope_type: DefinitionScope | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            items = visible_records(
                actor,
                kind=kind,
                definition_id=definition_id,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        except (DefinitionError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @router.post("/drafts")
    async def create_draft(payload: DefinitionDraftRequest, request: Request) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            require_mutation_actor(
                actor,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
            )
            record = service.create_draft(
                DefinitionDraftCreate(
                    **payload.model_dump(exclude={"actor"}, mode="python"),
                    actor=actor.identity_id,
                )
            )
        except Exception as exc:
            if isinstance(
                exc,
                (
                    DefinitionError,
                    DefinitionNotFoundError,
                    DefinitionConflictError,
                    DefinitionCompatibilityError,
                    AuthorizationError,
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
        actor = authenticated(request)
        try:
            existing = service.get_record(record_id)
            require_visible(existing, actor)
            require_mutation_actor(
                actor,
                scope_type=existing.scope_type,
                scope_id=existing.scope_id,
            )
            record = service.validate(record_id, actor=actor.identity_id)
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            AuthorizationError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.get("/{record_id}/publication-preflight")
    async def publication_preflight(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            existing = service.get_record(record_id)
            require_visible(existing, actor)
            assessment = service.publication_preflight(record_id)
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {
            "assessment": assessment.model_dump(mode="json"),
            "approvals": [
                item.model_dump(mode="json")
                for item in existing.publication_approvals
                if item.fingerprint == assessment.fingerprint
            ],
        }

    @router.post("/{record_id}/publication-approvals")
    async def approve_publication(
        record_id: str,
        payload: DefinitionPublicationApprovalHttpRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            existing = service.get_record(record_id)
            require_visible(existing, actor)
            require_approval_actor(
                actor,
                scope_type=existing.scope_type,
                scope_id=existing.scope_id,
            )
            approval = service.record_publication_approval(
                record_id,
                actor=actor.identity_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                reason=payload.reason,
            )
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            AuthorizationError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"approval": approval.model_dump(mode="json")}

    @router.post("/{record_id}/publish")
    async def publish(
        record_id: str,
        payload: DefinitionPublishHttpRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            existing = service.get_record(record_id)
            require_visible(existing, actor)
            require_mutation_actor(
                actor,
                scope_type=existing.scope_type,
                scope_id=existing.scope_id,
            )
            record = service.publish(
                record_id,
                DefinitionPublishRequest(
                    **payload.model_dump(exclude={"actor"}, mode="python"),
                    actor=actor.identity_id,
                ),
            )
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            AuthorizationError,
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
        actor = authenticated(request)
        try:
            existing = service.get_record(record_id)
            require_visible(existing, actor)
            require_mutation_actor(
                actor,
                scope_type=existing.scope_type,
                scope_id=existing.scope_id,
            )
            record = service.quarantine(
                record_id,
                actor=actor.identity_id,
                reason=payload.reason,
            )
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            AuthorizationError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/rollback")
    async def rollback(
        payload: DefinitionRollbackHttpRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            require_mutation_actor(
                actor,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
            )
            record = service.rollback(
                DefinitionRollbackRequest(
                    **payload.model_dump(exclude={"actor"}, mode="python"),
                    actor=actor.identity_id,
                )
            )
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            AuthorizationError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/resolve")
    async def resolve(payload: DefinitionResolveRequest, request: Request) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            context = tenant_context(payload.context, actor)
            record = service.resolve(
                definition_id=payload.definition_id,
                kind=payload.kind,
                context=context,
            )
            require_visible(record, actor)
        except (
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
        actor = authenticated(request)
        try:
            record = service.get_record(record_id)
            require_visible(record, actor)
            result = service.usage(record_id)
            items = visible_usage(result.get("items", []), actor)
            return {
                **result,
                "items": items,
                "count": len(items),
            }
        except (DefinitionError, DefinitionNotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    @router.get("/diff")
    async def diff(left: str, right: str, request: Request) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            left_record = service.get_record(left)
            right_record = service.get_record(right)
            require_visible(left_record, actor)
            require_visible(right_record, actor)
            return service.diff(left, right)
        except (DefinitionError, DefinitionNotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    @router.get("/export")
    async def export(request: Request, kind: str | None = None) -> dict[str, Any]:
        actor = authenticated(request)
        records = visible_records(actor, kind=kind)
        return {
            "format": "codex-web-definitions",
            "version": "1.0",
            "records": [record.model_dump(mode="json") for record in records],
        }

    @router.post("/import")
    async def import_definitions(
        payload: DefinitionImportRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = authenticated(request)
        try:
            raw_records = payload.document.get("records", [])
            for raw in raw_records:
                source = DefinitionRecord.model_validate(raw)
                require_mutation_actor(
                    actor,
                    scope_type=source.scope_type,
                    scope_id=source.scope_id,
                )
            records = service.import_records(
                payload.document,
                actor=actor.identity_id,
            )
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            AuthorizationError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {
            "items": [record.model_dump(mode="json") for record in records],
            "count": len(records),
        }

    return router
