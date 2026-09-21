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
from codex_web.services.definitions import (
    DefinitionApprovalRequiredError,
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
)
from codex_web.services.identity import AuthorizationError
from codex_web.services.skills import (
    SkillConflictError,
    SkillError,
    SkillNotFoundError,
    SkillService,
)
from codex_web.skills import SkillBundle, SkillDefinition


class SkillDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=120)
    skill: SkillDefinition
    scope_type: DefinitionScope = DefinitionScope.WORKSPACE
    scope_id: str | None = None
    reason: str | None = Field(default=None, max_length=1000)


class SkillRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: SkillDefinition
    reason: str = Field(min_length=1, max_length=1000)


class SkillPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)
    expected_active_revision: int | None = Field(default=None, ge=1)


class SkillArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class SkillRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class SkillImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: SkillBundle
    scope_type: DefinitionScope = DefinitionScope.WORKSPACE
    scope_id: str | None = None
    source_reference: str | None = Field(default=None, max_length=1000)


class SkillPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=64_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=4000)
    tags: tuple[str, ...] = Field(default=(), max_length=32)
    source_reference: str | None = Field(default=None, max_length=1000)


class SkillAttachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class SkillContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: tuple[DefinitionReference, ...] = Field(
        min_length=1,
        max_length=32,
    )
    relevance_tags: tuple[str, ...] = Field(default=(), max_length=32)
    asset_paths: tuple[str, ...] = Field(default=(), max_length=32)
    max_characters: int = Field(default=24_000, ge=1000, le=64_000)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (SkillNotFoundError, DefinitionNotFoundError)):
        return HTTPException(
            status_code=404,
            detail={"code": "skill_not_found", "message": str(exc)},
        )
    if isinstance(
        exc,
        (
            SkillConflictError,
            DefinitionConflictError,
            DefinitionApprovalRequiredError,
        ),
    ):
        detail: dict[str, Any] = {
            "code": "skill_conflict",
            "message": str(exc),
        }
        if isinstance(exc, DefinitionApprovalRequiredError):
            detail["record_id"] = exc.record_id
            detail["reasons"] = list(exc.reasons)
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, DefinitionCompatibilityError):
        return HTTPException(
            status_code=409,
            detail={"code": "skill_incompatible", "message": str(exc)},
        )
    if isinstance(exc, (SkillError, DefinitionError, ValueError)):
        return HTTPException(
            status_code=422,
            detail={"code": "skill_invalid", "message": str(exc)},
        )
    return HTTPException(status_code=400, detail=str(exc))


def build_skills_router(service: SkillService) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(
        request: Request,
        query: str | None = None,
        tag: str | None = None,
        capability: str | None = None,
        owner_identity_id: str | None = None,
        lifecycle: DefinitionLifecycle | None = None,
        include_revisions: bool = False,
    ) -> dict[str, Any]:
        try:
            items = service.list(
                actor=request_actor(request),
                query=query,
                tag=tag,
                capability=capability,
                owner_identity_id=owner_identity_id,
                lifecycle=lifecycle,
                include_revisions=include_revisions,
            )
            return {
                "items": [
                    {
                        "record": item.model_dump(mode="json"),
                        "skill": service.definition(item).model_dump(
                            mode="json"
                        ),
                    }
                    for item in items
                ],
                "count": len(items),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/drafts")
    async def create_draft(
        payload: SkillDraftRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_draft(
                skill_id=payload.skill_id,
                skill=payload.skill,
                actor=request_actor(request),
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                reason=payload.reason,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/import")
    async def import_bundle(
        payload: SkillImportRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.import_bundle(
                payload.bundle,
                actor=request_actor(request),
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                source_reference=payload.source_reference,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/promote-verified-procedure")
    async def promote_verified_procedure(
        payload: SkillPromoteRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.promote_verified_procedure(
                skill_id=payload.skill_id,
                name=payload.name,
                body=payload.body,
                evidence_refs=payload.evidence_refs,
                actor=request_actor(request),
                description=payload.description,
                tags=payload.tags,
                source_reference=payload.source_reference,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/context/preview")
    async def preview_context(
        payload: SkillContextRequest,
        request: Request,
    ) -> dict[str, Any]:
        # Resolve each reference through visible Skill records before producing
        # context so a caller cannot use the preview API to inspect a foreign
        # tenant Definition record.
        actor = request_actor(request)
        try:
            for reference in payload.references:
                service.get(reference.record_id, actor=actor)
            items = service.context_for(
                payload.references,
                relevance_tags=payload.relevance_tags,
                asset_paths=payload.asset_paths,
                max_characters=payload.max_characters,
            )
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in items
                ],
                "characters": sum(item.characters for item in items),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/definitions/{skill_id}/revisions")
    async def revisions(
        skill_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            items = service.revisions(
                skill_id,
                actor=request_actor(request),
            )
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in items
                ],
                "count": len(items),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/definitions/{skill_id}/rollback")
    async def rollback(
        skill_id: str,
        payload: SkillRollbackRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.rollback(
                skill_id=skill_id,
                target_revision=payload.target_revision,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/profiles/{profile_id}/{record_id}/attach")
    async def attach(
        profile_id: str,
        record_id: str,
        payload: SkillAttachRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            profile = service.attach(
                profile_id,
                record_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"profile": profile.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/profiles/{profile_id}/{skill_id}/detach")
    async def detach(
        profile_id: str,
        skill_id: str,
        payload: SkillAttachRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            profile = service.detach(
                profile_id,
                skill_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"profile": profile.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{record_id}")
    async def get_skill(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.get(
                record_id,
                actor=request_actor(request),
            )
            return {
                "record": item.model_dump(mode="json"),
                "skill": service.definition(item).model_dump(mode="json"),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/revisions")
    async def revise(
        record_id: str,
        payload: SkillRevisionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.revise(
                record_id,
                skill=payload.skill,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/publish")
    async def publish(
        record_id: str,
        payload: SkillPublishRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.publish(
                record_id,
                actor=request_actor(request),
                reason=payload.reason,
                expected_active_revision=payload.expected_active_revision,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{record_id}/archive")
    async def archive(
        record_id: str,
        payload: SkillArchiveRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.archive(
                record_id,
                actor=request_actor(request),
                reason=payload.reason,
            )
            return {"record": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{record_id}/export")
    async def export_bundle(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            bundle = service.export_bundle(
                record_id,
                actor=request_actor(request),
            )
            return {"bundle": bundle.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{record_id}/usage")
    async def usage(
        record_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.usage(
                record_id,
                actor=request_actor(request),
            )
        except Exception as exc:
            raise _error(exc) from exc

    return router
