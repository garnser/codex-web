from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.definitions import (
    DefinitionLifecycle,
    DefinitionReference,
    DefinitionScope,
)
from codex_web.services.agent_profiles import (
    AgentProfileConflict,
    AgentProfileError,
    AgentProfileNotFound,
)
from codex_web.services.definitions import (
    DefinitionApprovalRequiredError,
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
)
from codex_web.services.identity import AuthorizationError
from codex_web.services.skills import (
    SkillCompatibilityError,
    SkillError,
    SkillNotFoundError,
    SkillService,
)
from codex_web.skills import (
    MAX_SKILL_CONTEXT_BYTES,
    SkillBundle,
    SkillDefinition,
    SkillPromotionRequest,
)


class SkillDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    definition: SkillDefinition
    reason: str = Field(min_length=1, max_length=1000)
    scope_type: DefinitionScope = DefinitionScope.WORKSPACE
    scope_id: str | None = None
    derived_from_record_id: str | None = None


class SkillMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    expected_active_revision: int | None = Field(default=None, ge=1)


class SkillAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class SkillImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: SkillBundle
    reason: str = Field(min_length=1, max_length=1000)


class SkillPromotionHttpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion: SkillPromotionRequest
    reason: str = Field(min_length=1, max_length=1000)


class SkillContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: tuple[DefinitionReference, ...]
    task_text: str = Field(default="", max_length=128_000)
    tags: tuple[str, ...] = ()
    available_worker_capabilities: tuple[str, ...] = ()
    available_provider_capabilities: tuple[str, ...] = ()
    max_bytes: int = Field(
        default=MAX_SKILL_CONTEXT_BYTES,
        ge=1024,
        le=MAX_SKILL_CONTEXT_BYTES,
    )


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (SkillNotFoundError, DefinitionNotFoundError, AgentProfileNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (
            DefinitionConflictError,
            DefinitionApprovalRequiredError,
            AgentProfileConflict,
        ),
    ):
        detail: Any = str(exc)
        if isinstance(exc, DefinitionApprovalRequiredError):
            detail = {
                "code": "definition_publication_approval_required",
                "message": str(exc),
                "recordId": exc.record_id,
                "reasons": list(exc.reasons),
            }
        return HTTPException(status_code=409, detail=detail)
    if isinstance(
        exc,
        (
            SkillCompatibilityError,
            DefinitionCompatibilityError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(
        exc,
        (
            SkillError,
            DefinitionError,
            AgentProfileError,
            ValueError,
        ),
    ):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_skills_router(service: SkillService) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(
        request: Request,
        lifecycle: DefinitionLifecycle | None = None,
        tag: str | None = None,
        capability: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        try:
            items = service.list(
                actor=request_actor(request),
                lifecycle=lifecycle,
                tag=tag,
                capability=capability,
                owner=owner,
            )
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/drafts")
    async def create_draft(
        payload: SkillDraftRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record = service.create_draft(
                skill_id=payload.skill_id,
                definition=payload.definition,
                actor=request_actor(request),
                reason=payload.reason,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                derived_from_record_id=payload.derived_from_record_id,
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/import")
    async def import_bundle(
        payload: SkillImportRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record = service.import_bundle(
                payload.bundle,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/promote")
    async def promote(
        payload: SkillPromotionHttpRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record = service.promote_verified_procedure(
                payload.promotion,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/context")
    async def context(
        payload: SkillContextRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            selection = service.context(
                payload.references,
                actor=request_actor(request),
                task_text=payload.task_text,
                tags=payload.tags,
                available_worker_capabilities=(
                    payload.available_worker_capabilities
                ),
                available_provider_capabilities=(
                    payload.available_provider_capabilities
                ),
                max_bytes=payload.max_bytes,
            )
            return selection.model_dump(mode="json")
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{record_id}")
    async def get_skill(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.get(
                record_id,
                actor=request_actor(request),
            )
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{record_id}/bundle")
    async def export_bundle(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.export_bundle(
                record_id,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/validate")
    async def validate(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record = service.validate(
                record_id,
                actor=request_actor(request),
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/publish")
    async def publish(
        record_id: str,
        payload: SkillMutationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record = service.publish(
                record_id,
                actor=request_actor(request),
                reason=payload.reason,
                expected_active_revision=payload.expected_active_revision,
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/archive")
    async def archive(
        record_id: str,
        payload: SkillMutationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record = service.archive(
                record_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/profiles/{profile_id}/attach")
    async def attach(
        record_id: str,
        profile_id: str,
        payload: SkillAttachmentRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            profile = service.attach(
                record_id,
                profile_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"profile": profile.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/profiles/{profile_id}/detach")
    async def detach(
        record_id: str,
        profile_id: str,
        payload: SkillAttachmentRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            profile = service.detach(
                record_id,
                profile_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"profile": profile.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    return router
