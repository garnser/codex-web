from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.resources import (
    ProjectResourceBindCreate,
    RelationshipCreate,
    ResourceCreate,
    ResourceLifecycle,
    ResourceRelationshipType,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.resources import (
    ResourceAmbiguousError,
    ResourceCatalogError,
    ResourceCatalogService,
    ResourceConflictError,
    ResourceNotFoundError,
)


class LegacyResourceMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    repositories: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ResourceNotFoundError, ProjectNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ResourceAmbiguousError, ResourceConflictError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ResourceCatalogError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_resources_router(
    service: ResourceCatalogService,
    projects: ProjectService,
) -> APIRouter:
    router = APIRouter(tags=["resources"])

    @router.get("/api/resources")
    async def list_resources(
        request: Request,
        resource_type: ResourceType | None = None,
        lifecycle: ResourceLifecycle | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list(
                    actor,
                    resource_type=resource_type,
                    lifecycle=lifecycle,
                )
            ]
        }

    @router.post("/api/resources")
    async def create_resource(
        payload: ResourceCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    # Static resource suffixes must precede the resource-id catch-all.
    @router.get("/api/resources/resolve")
    async def resolve_resource(
        value: str,
        request: Request,
        expected_type: ResourceType | None = None,
        alias_namespace: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        try:
            item = service.resolve(
                value,
                actor=request_actor(request),
                expected_type=expected_type,
                alias_namespace=alias_namespace,
                provider=provider,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/api/resources/relationships")
    async def create_relationship(
        payload: RelationshipCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.add_relationship(
                from_resource_id=payload.from_resource_id,
                to_resource_id=payload.to_resource_id,
                relationship_type=payload.relationship_type,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.post("/api/resources/project-bindings")
    async def bind_project(
        payload: ProjectResourceBindCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            project = projects.get(payload.project_id, actor.tenant)
            binding = service.bind_project(
                project=project,
                resource_id=payload.resource_id,
                actor=actor,
                purpose=payload.purpose,
            )
            return {"item": binding.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ResourceCatalogError,
                    ProjectNotFoundError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/resources/migrate-legacy")
    async def migrate_legacy(
        payload: LegacyResourceMigrationRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            project = projects.get(payload.project_id, actor.tenant)
            items = service.migrate_legacy_strings(
                project=project,
                actor=actor,
                repositories=payload.repositories,
                environments=payload.environments,
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ResourceCatalogError,
                    ProjectNotFoundError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/projects/{project_id}/resources")
    async def project_resources(project_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            project = projects.get(project_id, actor.tenant)
            items = service.project_resources(project, actor=actor)
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ResourceCatalogError,
                    ProjectNotFoundError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/resources/{resource_id}/relationships")
    async def resource_relationships(
        resource_id: str,
        request: Request,
        direction: str = "both",
    ) -> dict[str, Any]:
        try:
            items = service.relationships(
                resource_id,
                actor=request_actor(request),
                direction=direction,
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/api/resources/{resource_id}/traverse")
    async def traverse_resource(
        resource_id: str,
        request: Request,
        direction: str = "outgoing",
        relationship_type: list[ResourceRelationshipType] | None = None,
        max_depth: int = 10,
    ) -> dict[str, Any]:
        try:
            items = service.traverse(
                resource_id,
                actor=request_actor(request),
                direction=direction,
                relationship_types=set(relationship_type) if relationship_type else None,
                max_depth=max_depth,
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/api/resources/{resource_id}")
    async def get_resource(resource_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get(resource_id, request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    @router.patch("/api/resources/{resource_id}")
    async def update_resource(
        resource_id: str,
        payload: ResourceUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.update(
                resource_id,
                payload,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ResourceCatalogError, AuthorizationError, TenantIsolationError)):
                raise _error(exc) from exc
            raise

    return router
