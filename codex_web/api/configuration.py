from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationRollbackRequest,
    ConfigurationScope,
)
from codex_web.services.configuration import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationNotFoundError,
    ConfigurationService,
)


class ConfigurationResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    context: ConfigurationContext = Field(default_factory=ConfigurationContext)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ConfigurationNotFoundError):
        return HTTPException(status_code=404, detail={"code": "configuration_not_found", "message": str(exc)})
    if isinstance(exc, ConfigurationConflictError):
        return HTTPException(status_code=409, detail={"code": "configuration_conflict", "message": str(exc)})
    if isinstance(exc, ConfigurationError):
        return HTTPException(status_code=422, detail={"code": "configuration_invalid", "message": str(exc)})
    return HTTPException(status_code=400, detail={"code": "configuration_error", "message": str(exc)})


def build_configuration_router(service: ConfigurationService) -> APIRouter:
    router = APIRouter(prefix="/api/configuration", tags=["configuration"])

    @router.get("/specs")
    async def list_specs() -> dict[str, Any]:
        items = [spec.model_dump(mode="json") for spec in service.list_specs()]
        return {"items": items, "count": len(items)}

    @router.get("/records")
    async def list_records(
        key: str | None = None,
        scope_type: ConfigurationScope | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        items = [
            record.model_dump(mode="json")
            for record in service.list_records(
                key=key,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        ]
        return {"items": items, "count": len(items)}

    @router.post("/drafts")
    async def create_draft(payload: ConfigurationDraftCreate) -> dict[str, Any]:
        try:
            record = service.create_draft(payload)
        except (ConfigurationError, ConfigurationNotFoundError, ValueError) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/{record_id}/validate")
    async def validate_record(record_id: str) -> dict[str, Any]:
        try:
            return service.validate_record(record_id)
        except (ConfigurationError, ConfigurationNotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/publish")
    async def publish_record(
        record_id: str,
        payload: ConfigurationPublishRequest,
    ) -> dict[str, Any]:
        try:
            record = service.publish(record_id, payload)
        except (
            ConfigurationConflictError,
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/rollback")
    async def rollback(payload: ConfigurationRollbackRequest) -> dict[str, Any]:
        try:
            record = service.rollback(payload)
        except (
            ConfigurationConflictError,
            ConfigurationError,
            ConfigurationNotFoundError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"record": record.model_dump(mode="json")}

    @router.post("/resolve")
    async def resolve(payload: ConfigurationResolveRequest) -> dict[str, Any]:
        try:
            effective = service.resolve(payload.key, payload.context)
        except (ConfigurationError, ConfigurationNotFoundError, ValueError) as exc:
            raise _error(exc) from exc
        return {"effective": effective.model_dump(mode="json")}

    @router.get("/{record_id}/impact")
    async def impact(record_id: str) -> dict[str, Any]:
        try:
            return service.impact(record_id)
        except (ConfigurationError, ConfigurationNotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    return router
