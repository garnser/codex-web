from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.definitions import (
    DefinitionApprovalRequiredError,
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
)
from codex_web.services.identity import AuthorizationError
from codex_web.services.skills import (
    SkillConflict,
    SkillError,
    SkillNotFound,
    SkillService,
)
from codex_web.skills import (
    SkillBundleImport,
    SkillLifecycle,
    SkillLifecycleChange,
    SkillPromotionRequest,
    SkillPublish,
    SkillRollback,
    SkillUpdate,
    SkillCreate,
)
from codex_web.skill_catalog import ThreadSkillAssignmentsUpdate
from codex_web.api.thread_scope import thread_scope_dependency
from codex_web.services.thread_scope import ThreadScopeService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (SkillNotFound, DefinitionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (
            SkillConflict,
            DefinitionConflictError,
            DefinitionApprovalRequiredError,
        ),
    ):
        detail: Any = str(exc)
        if isinstance(exc, DefinitionApprovalRequiredError):
            detail = {
                "code": "skill_publication_approval_required",
                "message": str(exc),
                "recordId": exc.record_id,
                "reasons": list(exc.reasons),
            }
        return HTTPException(status_code=409, detail=detail)
    if isinstance(
        exc,
        (
            SkillError,
            DefinitionError,
            DefinitionCompatibilityError,
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
        search: str | None = None,
        tag: str | None = None,
        category: str | None = None,
        source_id: str | None = None,
        owner_identity_id: str | None = None,
        lifecycle: SkillLifecycle | None = None,
        include_drafts: bool = True,
        limit: int = 100,
        cursor: int = 0,
    ) -> dict[str, Any]:
        try:
            limit = max(1, min(limit, 200))
            cursor = max(0, cursor)
            items = service.list(
                    actor=request_actor(request),
                    search=search,
                    tag=tag,
                    category=category,
                    source_id=source_id,
                    owner_identity_id=owner_identity_id,
                    lifecycle=lifecycle,
                    include_drafts=include_drafts,
                )
            page = items[cursor:cursor + limit]
            return {
                "items": page,
                "count": len(items),
                "nextCursor": cursor + limit if cursor + limit < len(items) else None,
                "categories": sorted({category for item in items for category in item["skill"].get("categories", [])}),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("")
    async def create_skill(
        payload: SkillCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {"item": service.create(payload, actor=request_actor(request))}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/import")
    async def import_skill(
        payload: SkillBundleImport,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.import_bundle(
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/promote-verified")
    async def promote_verified(
        payload: SkillPromotionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.promote_verified(
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{skill_id}")
    async def get_skill(
        skill_id: str,
        request: Request,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.get(
                    skill_id,
                    actor=request_actor(request),
                    revision=revision,
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{skill_id}/revisions")
    async def revisions(
        skill_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            items = service.revisions(
                skill_id,
                actor=request_actor(request),
            )
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _error(exc) from exc

    @router.patch("/{skill_id}")
    async def update_skill(
        skill_id: str,
        payload: SkillUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.update(
                    skill_id,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{skill_id}/revisions/{record_id}/publish")
    async def publish_skill(
        skill_id: str,
        record_id: str,
        payload: SkillPublish,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.publish(
                    skill_id,
                    record_id,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    async def change_lifecycle(
        skill_id: str,
        lifecycle: SkillLifecycle,
        payload: SkillLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.lifecycle(
                    skill_id,
                    lifecycle,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{skill_id}/archive")
    async def archive(
        skill_id: str,
        payload: SkillLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change_lifecycle(
            skill_id,
            SkillLifecycle.ARCHIVED,
            payload,
            request,
        )

    @router.post("/{skill_id}/restore")
    async def restore(
        skill_id: str,
        payload: SkillLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change_lifecycle(
            skill_id,
            SkillLifecycle.ACTIVE,
            payload,
            request,
        )

    @router.post("/{skill_id}/rollback")
    async def rollback(
        skill_id: str,
        payload: SkillRollback,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "item": service.rollback(
                    skill_id,
                    payload,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{skill_id}/usage")
    async def usage(
        skill_id: str,
        request: Request,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            return service.usage(
                skill_id,
                actor=request_actor(request),
                revision=revision,
            )
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{skill_id}/export")
    async def export_skill(
        skill_id: str,
        request: Request,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            return service.export_bundle(
                skill_id,
                actor=request_actor(request),
                revision=revision,
            )
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{skill_id}/profiles/{profile_id}")
    async def attach_profile(
        skill_id: str,
        profile_id: str,
        request: Request,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            return {
                "profile": service.attach_profile(
                    skill_id,
                    profile_id,
                    actor=request_actor(request),
                    revision=revision,
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.delete("/{skill_id}/profiles/{profile_id}")
    async def detach_profile(
        skill_id: str,
        profile_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return {
                "profile": service.detach_profile(
                    skill_id,
                    profile_id,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    return router


def build_thread_skills_router(service: SkillService, scope: ThreadScopeService) -> APIRouter:
    router = APIRouter(tags=["skills"], dependencies=[Depends(thread_scope_dependency(scope))])

    @router.get("/api/threads/{thread_id}/skills")
    async def get_thread_skills(thread_id: str, request: Request) -> dict[str, Any]:
        try:
            return service.thread_assignments(thread_id, actor=request_actor(request))
        except Exception as exc:
            raise _error(exc) from exc

    @router.put("/api/threads/{thread_id}/skills")
    async def update_thread_skills(
        thread_id: str,
        payload: ThreadSkillAssignmentsUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.set_thread_assignments(
                thread_id, payload.skill_refs, actor=request_actor(request)
            )
        except Exception as exc:
            raise _error(exc) from exc

    return router
