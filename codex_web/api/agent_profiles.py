from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.agent_profiles import (
    AgentProfileCreate,
    AgentProfileLifecycle,
    AgentProfileLifecycleChange,
    AgentProfileUpdate,
)
from codex_web.api.identity import request_actor
from codex_web.services.agent_profiles import (
    AgentProfileAccessDenied,
    AgentProfileConflict,
    AgentProfileError,
    AgentProfileNotFound,
    AgentProfileService,
)
from codex_web.services.identity import AuthorizationError


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AgentProfileNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AgentProfileAccessDenied):
        return HTTPException(
            status_code=403,
            detail={
                "code": "agent_profile_access_denied",
                "message": str(exc),
                "decision": (
                    exc.decision.model_dump(mode="json")
                    if exc.decision is not None
                    else None
                ),
            },
        )
    if isinstance(exc, AgentProfileConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AgentProfileError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_agent_profiles_router(
    service: AgentProfileService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-profiles",
        tags=["agent-profiles"],
    )

    @router.get("")
    async def list_profiles(
        request: Request,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        try:
            items = service.list(
                actor=request_actor(request),
                include_archived=include_archived,
            )
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in items
                ]
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/picker")
    async def picker(request: Request) -> dict[str, Any]:
        try:
            items = service.list(actor=request_actor(request))
            return {
                "items": [
                    {
                        "id": item.profile_id,
                        "revision": item.revision,
                        "name": item.name,
                        "avatarRef": item.avatar_ref,
                        "description": item.description,
                        "lifecycle": item.lifecycle.value,
                        "roleId": item.role_id,
                        "executionProfileId": item.execution_profile_id,
                        "modelClass": item.model_policy.model_class,
                    }
                    for item in items
                    if item.lifecycle
                    == AgentProfileLifecycle.ACTIVE
                ]
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("")
    async def create_profile(
        payload: AgentProfileCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{profile_id}")
    async def get_profile(
        profile_id: str,
        request: Request,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            item = service.get(
                profile_id,
                actor=request_actor(request),
                revision=revision,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{profile_id}/executions")
    async def executions(
        profile_id: str,
        request: Request,
        limit: int = 20,
    ) -> dict[str, Any]:
        try:
            return service.execution_history(
                profile_id,
                actor=request_actor(request),
                limit=limit,
            )
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{profile_id}/audit")
    async def audit(
        profile_id: str,
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            items = service.revisions(
                profile_id,
                actor=request_actor(request),
            )
            bounded = list(reversed(items))[
                : max(1, min(int(limit), 100))
            ]
            return {
                "items": [
                    {
                        "profileId": item.profile_id,
                        "revision": item.revision,
                        "recordId": item.record_id,
                        "lifecycle": item.lifecycle.value,
                        "updatedBy": item.updated_by,
                        "updatedAt": item.updated_at,
                        "reason": item.change_reason,
                    }
                    for item in bounded
                ],
                "count": len(items),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{profile_id}/revisions")
    async def revisions(
        profile_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            items = service.revisions(
                profile_id,
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

    @router.patch("/{profile_id}")
    async def update_profile(
        profile_id: str,
        payload: AgentProfileUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update(
                profile_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    async def change(
        profile_id: str,
        lifecycle: AgentProfileLifecycle,
        payload: AgentProfileLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.lifecycle(
                profile_id,
                lifecycle,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{profile_id}/disable")
    async def disable(
        profile_id: str,
        payload: AgentProfileLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change(
            profile_id,
            AgentProfileLifecycle.DISABLED,
            payload,
            request,
        )

    @router.post("/{profile_id}/archive")
    async def archive(
        profile_id: str,
        payload: AgentProfileLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change(
            profile_id,
            AgentProfileLifecycle.ARCHIVED,
            payload,
            request,
        )

    @router.post("/{profile_id}/restore")
    async def restore(
        profile_id: str,
        payload: AgentProfileLifecycleChange,
        request: Request,
    ) -> dict[str, Any]:
        return await change(
            profile_id,
            AgentProfileLifecycle.ACTIVE,
            payload,
            request,
        )

    @router.get("/{profile_id}/access")
    async def access(
        profile_id: str,
        request: Request,
        project_id: str | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            actor = request_actor(request)
            profile = service.get(
                profile_id,
                actor=actor,
                revision=revision,
                require_visible=False,
            )
            decision = service.access_decision(
                profile,
                actor=actor,
                project_id=project_id,
            )
            return {
                "decision": decision.model_dump(mode="json")
            }
        except Exception as exc:
            raise _error(exc) from exc

    return router
