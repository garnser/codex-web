from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.identity import AuthorizationError
from codex_web.services.skill_security import SkillSecurityBlocked, SkillSecurityError, SkillSecurityService
from codex_web.services.skills import SkillNotFound, SkillService
from codex_web.skill_security import (
    SkillScanRequest,
    SkillSecurityBaselineRequest,
    SkillSecurityOverrideRequest,
    SkillSecurityPolicyUpdate,
)


def build_skill_security_router(security: SkillSecurityService, skills: SkillService) -> APIRouter:
    router = APIRouter(tags=["skills"])

    def view(skill_id: str, record_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        item = next(
            (value for value in skills.revisions(skill_id, actor=actor) if value["recordId"] == record_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Skill revision not found")
        return item

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, SkillNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, SkillSecurityBlocked):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, SkillSecurityError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("/api/skill-security/providers")
    async def providers(request: Request) -> dict[str, Any]:
        items = security.providers.list(tenant_scope=request_actor(request).tenant)
        return {"items": items, "count": len(items)}

    @router.get("/api/skill-security/policy")
    async def get_policy(request: Request) -> dict[str, Any]:
        return {"policy": security.policy(request_actor(request)).model_dump(mode="json")}

    @router.put("/api/skill-security/policy")
    async def put_policy(payload: SkillSecurityPolicyUpdate, request: Request) -> dict[str, Any]:
        try:
            return security.update_policy(payload, actor=request_actor(request))
        except Exception as exc:
            raise error(exc) from exc

    @router.get("/api/skills/{skill_id}/revisions/{record_id}/security")
    async def report(skill_id: str, record_id: str, request: Request) -> dict[str, Any]:
        try:
            return security.report(view(skill_id, record_id, request), actor=request_actor(request))
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/api/skills/{skill_id}/revisions/{record_id}/scan")
    async def scan(skill_id: str, record_id: str, payload: SkillScanRequest, request: Request) -> dict[str, Any]:
        try:
            return security.scan(view(skill_id, record_id, request), payload, actor=request_actor(request))
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/api/skills/{skill_id}/revisions/{record_id}/security/override")
    async def create_override(skill_id: str, record_id: str, payload: SkillSecurityOverrideRequest, request: Request) -> dict[str, Any]:
        try:
            return {"item": security.create_override(view(skill_id, record_id, request), payload, actor=request_actor(request))}
        except Exception as exc:
            raise error(exc) from exc

    @router.put("/api/skills/{skill_id}/revisions/{record_id}/security/baseline")
    async def set_baseline(skill_id: str, record_id: str, payload: SkillSecurityBaselineRequest, request: Request) -> dict[str, Any]:
        try:
            return {"item": security.set_baseline(view(skill_id, record_id, request), payload, actor=request_actor(request))}
        except Exception as exc:
            raise error(exc) from exc

    return router
