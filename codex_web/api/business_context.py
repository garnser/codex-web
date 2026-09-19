from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.business_context import (
    BusinessEntityCreate,
    BusinessEntityRelationshipCreate,
    BusinessEntityType,
    BusinessEntityUpdate,
    CompanyFactCreate,
    CompanyFactSupersede,
    ExternalRecordRefCreate,
    ExternalRecordRefUpdate,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.business_context import (
    BusinessContextConflictError,
    BusinessContextError,
    BusinessContextNotFoundError,
    BusinessContextService,
    BusinessContextValidationError,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, BusinessContextNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BusinessContextConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (BusinessContextValidationError, BusinessContextError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_business_context_router(
    service: BusinessContextService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/business-context",
        tags=["business-context"],
    )

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "business-data:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "business-data:admin service scope required"
                )
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    @router.get("/entities")
    async def list_entities(
        request: Request,
        entity_type: BusinessEntityType | None = None,
        include_inactive: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.list_entities(
                actor=actor,
                entity_type=entity_type,
                include_inactive=include_inactive,
                limit=limit,
            )
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/entities")
    async def create_entity(
        payload: BusinessEntityCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_entity(
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/entities/{entity_id}")
    async def get_entity(
        entity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get_entity(entity_id, actor=actor)
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.patch("/entities/{entity_id}")
    async def update_entity(
        entity_id: str,
        payload: BusinessEntityUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update_entity(
                entity_id,
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/entities/{entity_id}/context")
    async def entity_context(
        entity_id: str,
        request: Request,
        fact_limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            entity = service.get_entity(entity_id, actor=actor)
            facts = service.list_facts(
                actor=actor,
                business_entity_id=entity_id,
                include_inactive=True,
                limit=fact_limit,
            )
            external = service.list_external_records(
                actor=actor,
                business_entity_id=entity_id,
                include_inactive=True,
                limit=100,
            )
            relationships = service.relationships(entity_id, actor=actor)
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {
            "entity": entity.model_dump(mode="json"),
            "facts": [item.model_dump(mode="json") for item in facts],
            "external_records": [
                item.model_dump(mode="json") for item in external
            ],
            "relationships": [
                item.model_dump(mode="json") for item in relationships
            ],
        }

    @router.get("/external-records")
    async def list_external_records(
        request: Request,
        business_entity_id: str | None = None,
        include_inactive: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.list_external_records(
                actor=actor,
                business_entity_id=business_entity_id,
                include_inactive=include_inactive,
                limit=limit,
            )
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/external-records")
    async def create_external_record(
        payload: ExternalRecordRefCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_external_record(
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/external-records/{ref_id}")
    async def get_external_record(
        ref_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get_external_record(ref_id, actor=actor)
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.patch("/external-records/{ref_id}")
    async def update_external_record(
        ref_id: str,
        payload: ExternalRecordRefUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update_external_record(
                ref_id,
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/facts")
    async def list_facts(
        request: Request,
        business_entity_id: str | None = None,
        key: str | None = None,
        include_inactive: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.list_facts(
                actor=actor,
                business_entity_id=business_entity_id,
                key=key,
                include_inactive=include_inactive,
                limit=limit,
            )
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/facts")
    async def create_fact(
        payload: CompanyFactCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_fact(
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/facts/{fact_id}")
    async def get_fact(
        fact_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get_fact(fact_id, actor=actor)
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/facts/{fact_id}/supersede")
    async def supersede_fact(
        fact_id: str,
        payload: CompanyFactSupersede,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.supersede_fact(
                fact_id,
                payload.replacement,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/entities/{entity_id}/facts/current")
    async def current_fact(
        entity_id: str,
        request: Request,
        key: str = Query(min_length=1),
        at: float | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.resolve_fact(
                entity_id,
                key,
                actor=actor,
                at=at,
            )
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/relationships")
    async def create_relationship(
        payload: BusinessEntityRelationshipCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_relationship(
                payload,
                actor=mutation_actor(request),
            )
        except (AuthorizationError, BusinessContextError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/entities/{entity_id}/relationships")
    async def relationships(
        entity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.relationships(entity_id, actor=actor)
        except BusinessContextError as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    return router
