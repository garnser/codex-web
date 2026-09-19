from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.data_governance import DataClassification
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.organizational_memory import (
    KnowledgeCreate,
    KnowledgeInvalidate,
    KnowledgeLifecycle,
    KnowledgeObjectType,
    KnowledgeQuery,
    KnowledgeRevise,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.organizational_memory import (
    KnowledgeAuthorizationError,
    KnowledgeValidationError,
    OrganizationalMemoryError,
    OrganizationalMemoryService,
)
from codex_web.storage.organizational_memory import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
)


def build_organizational_memory_router(
    service: OrganizationalMemoryService,
) -> APIRouter:
    router = APIRouter(prefix="/api/memory", tags=["organizational-memory"])

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, KnowledgeNotFoundError):
            return HTTPException(
                status_code=404,
                detail="organizational memory record not found",
            )
        if isinstance(exc, KnowledgeAuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, KnowledgeConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(
            exc,
            (
                KnowledgeValidationError,
                OrganizationalMemoryError,
                ValueError,
            ),
        ):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("memory:read", "memory:write", "memory:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="memory:read, memory:write or memory:admin service scope required",
                )
        return actor

    def writer(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("memory:write", "memory:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="memory:write or memory:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "memory:admin" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="memory:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    def view(item) -> dict[str, Any]:
        payload = item.model_dump(mode="json")
        if (
            item.classification == DataClassification.SECRET
            or item.lifecycle
            in {KnowledgeLifecycle.REDACTED, KnowledgeLifecycle.DELETED}
        ):
            payload["title"] = "[restricted]"
            payload["summary"] = "[restricted]"
            payload["content"] = None
            payload["canonical_refs"] = []
            payload["tags"] = []
        return payload

    @router.get("")
    async def list_memory(
        request: Request,
        project_id: str | None = None,
        object_type: KnowledgeObjectType | None = None,
        include_inactive: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            rows = list(
                service.list(
                    actor=actor,
                    project_id=project_id,
                    include_inactive=include_inactive,
                )
            )
        except Exception as exc:
            raise translate(exc) from exc
        if object_type is not None:
            rows = [item for item in rows if item.object_type == object_type]
        rows = rows[:limit]
        return {
            "items": [view(item) for item in rows],
            "count": len(rows),
        }

    @router.post("")
    async def create_memory(
        payload: KnowledgeCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = service.create(payload, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": view(item)}

    @router.get("/index")
    async def retrieval_index_status(
        request: Request,
    ) -> dict[str, Any]:
        reader(request)
        return {
            "status": service.retrieval_status().model_dump(mode="json")
        }

    @router.post("/index/rebuild")
    async def rebuild_retrieval_index(
        request: Request,
    ) -> dict[str, Any]:
        actor = admin(request)
        try:
            status = service.rebuild_retrieval_index(actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"status": status.model_dump(mode="json")}

    @router.post("/search")
    async def search_memory(
        payload: KnowledgeQuery,
        request: Request,
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            result = service.search(payload, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return result.model_dump(mode="json")

    @router.get("/retrievals/{retrieval_id}")
    async def retrieval_run(
        retrieval_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            item = service.retrieval_run(retrieval_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{knowledge_id}")
    async def get_memory(
        knowledge_id: str,
        request: Request,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            item = service.get(
                knowledge_id,
                actor=actor,
                include_inactive=include_inactive,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": view(item)}

    @router.post("/{knowledge_id}/revise")
    async def revise_memory(
        knowledge_id: str,
        payload: KnowledgeRevise,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = service.revise(
                knowledge_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": view(item)}

    @router.post("/{knowledge_id}/invalidate")
    async def invalidate_memory(
        knowledge_id: str,
        payload: KnowledgeInvalidate,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = service.invalidate(
                knowledge_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": view(item)}

    @router.get("/{knowledge_id}/versions")
    async def memory_versions(
        knowledge_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            rows = service.versions(knowledge_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "items": [view(item) for item in rows],
            "count": len(rows),
        }

    @router.get("/{knowledge_id}/relationships")
    async def memory_relationships(
        knowledge_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = reader(request)
        try:
            rows = service.relationships(
                knowledge_id,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    return router
