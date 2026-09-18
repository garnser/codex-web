from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.work_graph import (
    WorkGraphConflictError,
    WorkGraphCycleError,
    WorkGraphError,
    WorkGraphNotFoundError,
    WorkGraphScopeError,
    WorkGraphService,
)
from codex_web.work_graph import WorkGraphEdgeCreate, WorkGraphRelation


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (WorkGraphNotFoundError, ProjectNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (WorkGraphConflictError, WorkGraphCycleError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AuthorizationError, WorkGraphScopeError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (WorkGraphError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_work_graph_router(
    service: WorkGraphService,
    projects: ProjectService,
) -> APIRouter:
    router = APIRouter(prefix="/api/work-graph", tags=["work-graph"])

    def require_project(project_id: str, request: Request) -> None:
        try:
            projects.get(project_id, request_actor(request).tenant)
        except ProjectNotFoundError as exc:
            raise _error(exc) from exc

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "work-graph:admin" not in actor.service_scopes:
                raise AuthorizationError("work-graph:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/projects/{project_id}")
    async def project_snapshot(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        require_project(project_id, request)
        snapshot = service.snapshot(
            project_id,
            scope=request_actor(request).tenant,
        )
        return {"graph": snapshot.model_dump(mode="json")}

    @router.get("/readiness")
    async def readiness(
        request: Request,
        ref: str = Query(min_length=1),
    ) -> dict[str, Any]:
        try:
            result = service.readiness(
                ref,
                scope=request_actor(request).tenant,
            )
        except (WorkGraphError, ValueError) as exc:
            raise _error(exc) from exc
        return {"readiness": result.model_dump(mode="json")}

    @router.get("/traverse")
    async def traverse(
        request: Request,
        ref: str = Query(min_length=1),
        relation: WorkGraphRelation | None = None,
        direction: str = Query(default="downstream", pattern="^(downstream|upstream)$"),
    ) -> dict[str, Any]:
        try:
            refs = service.traverse(
                ref,
                scope=request_actor(request).tenant,
                relation=relation,
                direction=direction,
            )
        except (WorkGraphError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "ref": ref,
            "relation": relation.value if relation is not None else None,
            "direction": direction,
            "refs": list(refs),
        }

    @router.get("/events")
    async def events(
        request: Request,
        project_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        if project_id is not None:
            require_project(project_id, request)
        rows = service.events(
            scope=request_actor(request).tenant,
            project_id=project_id,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/edges")
    async def add_edge(
        payload: WorkGraphEdgeCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            edge = service.add_edge(
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (
            AuthorizationError,
            WorkGraphError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"edge": edge.model_dump(mode="json")}

    @router.delete("/edges/{edge_id}")
    async def remove_edge(
        edge_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            edge = service.remove_edge(
                edge_id,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (
            AuthorizationError,
            WorkGraphError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"edge": edge.model_dump(mode="json")}

    return router
