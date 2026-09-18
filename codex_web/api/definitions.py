from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    DefinitionRollbackRequest,
    DefinitionScope,
)
from codex_web.services.definitions import (
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
    DefinitionRegistryService,
)


class DefinitionValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str = Field(min_length=1)


class DefinitionQuarantineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DefinitionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str = Field(min_length=1)
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
    return HTTPException(
        status_code=422,
        detail={"code": "definition_invalid", "message": str(exc)},
    )


def build_definitions_router(service: DefinitionRegistryService) -> APIRouter:
    router = APIRouter(prefix="/api/definitions", tags=["definitions"])

    @router.get("/schemas")
    async def schemas() -> dict[str, Any]:
        items = service.schemas.metadata()
        return {"items": items, "count": len(items)}

    @router.get("/bootstrap")
    async def bootstrap_status() -> dict[str, Any]:
        return service.bootstrap_status()

    @router.get("/records")
    async def records(
        kind: str | None = None,
        definition_id: str | None = None,
        scope_type: DefinitionScope | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            items = service.list_records(
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
    async def create_draft(payload: DefinitionDraftCreate) -> dict[str, Any]:
        try:
            record = service.create_draft(payload)
        except Exception as exc:
            if isinstance(
                exc,
                (
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
    async def validate(record_id: str, payload: DefinitionValidateRequest) -> dict[str, Any]:
        try:
            record = service.validate(record_id, actor=payload.actor)
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/{record_id}/publish")
    async def publish(record_id: str, payload: DefinitionPublishRequest) -> dict[str, Any]:
        try:
            record = service.publish(record_id, payload)
        except (
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
    ) -> dict[str, Any]:
        try:
            record = service.quarantine(
                record_id,
                actor=payload.actor,
                reason=payload.reason,
            )
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/rollback")
    async def rollback(payload: DefinitionRollbackRequest) -> dict[str, Any]:
        try:
            record = service.rollback(payload)
        except (
            DefinitionError,
            DefinitionNotFoundError,
            DefinitionConflictError,
            DefinitionCompatibilityError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/resolve")
    async def resolve(payload: DefinitionResolveRequest) -> dict[str, Any]:
        try:
            record = service.resolve(
                definition_id=payload.definition_id,
                kind=payload.kind,
                context=payload.context,
            )
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
    async def usage(record_id: str) -> dict[str, Any]:
        try:
            return service.usage(record_id)
        except (DefinitionError, DefinitionNotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    @router.get("/diff")
    async def diff(left: str, right: str) -> dict[str, Any]:
        try:
            return service.diff(left, right)
        except (DefinitionError, DefinitionNotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    @router.get("/export")
    async def export(kind: str | None = None) -> dict[str, Any]:
        return service.export(kind=kind)

    @router.post("/import")
    async def import_definitions(payload: DefinitionImportRequest) -> dict[str, Any]:
        try:
            records = service.import_records(payload.document, actor=payload.actor)
        except (
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
