from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.metrics import (
    MetricDefinitionCreate,
    MetricDefinitionUpdate,
    MetricObservationCreate,
    MetricSnapshotRequest,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.metrics import (
    MetricConflictError,
    MetricError,
    MetricNotFoundError,
    MetricService,
    MetricValidationError,
)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, MetricNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MetricConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (MetricValidationError, MetricError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_metrics_router(service: MetricService) -> APIRouter:
    router = APIRouter(prefix="/api/metrics", tags=["metrics"])

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "metrics:admin" not in actor.service_scopes:
                raise AuthorizationError("metrics:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    def require_project_scope(metric_id: str, project_id: str | None, *, actor) -> None:
        if project_id is None:
            return  # Preserve the explicitly workspace-wide API for existing consumers.
        item = service.get_definition(metric_id, scope=actor.tenant)
        if item.project_id != project_id:
            raise MetricNotFoundError("Metric not found")

    @router.get("")
    async def list_metrics(
        request: Request,
        project_id: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        rows = service.list_definitions(
            scope=actor.tenant,
            project_id=project_id,
            resource_id=resource_id,
        )
        return {
            "items": [
                {
                    "definition": row.model_dump(mode="json"),
                    "current": service.evaluate(
                        row.id,
                        scope=actor.tenant,
                    ).model_dump(mode="json"),
                }
                for row in rows
            ],
            "count": len(rows),
        }

    @router.post("")
    async def create_metric(
        payload: MetricDefinitionCreate,
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            if project_id is not None and payload.project_id != project_id:
                raise MetricValidationError(
                    "Metric Project must match the requested Project"
                )
            item = service.create_definition(
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{metric_id}")
    async def get_metric(
        metric_id: str, request: Request, project_id: str | None = None
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            require_project_scope(metric_id, project_id, actor=actor)
            item = service.get_definition(metric_id, scope=actor.tenant)
            current = service.evaluate(metric_id, scope=actor.tenant)
        except (MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "definition": item.model_dump(mode="json"),
            "current": current.model_dump(mode="json"),
        }

    @router.patch("/{metric_id}")
    async def update_metric(
        metric_id: str,
        payload: MetricDefinitionUpdate,
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            require_project_scope(metric_id, project_id, actor=actor)
            if (
                project_id is not None
                and "project_id" in payload.model_fields_set
                and payload.project_id != project_id
            ):
                raise MetricValidationError(
                    "Metric Project must match the requested Project"
                )
            item = service.update_definition(
                metric_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{metric_id}/revisions")
    async def metric_revisions(
        metric_id: str, request: Request, project_id: str | None = None
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            require_project_scope(metric_id, project_id, actor=actor)
            rows = service.revisions(metric_id, scope=actor.tenant)
        except (MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [row.model_dump(mode="json") for row in rows],
            "count": len(rows),
        }

    @router.post("/{metric_id}/observations")
    async def ingest_observation(
        metric_id: str,
        payload: MetricObservationCreate,
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            require_project_scope(metric_id, project_id, actor=actor)
            item = service.ingest(
                metric_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{metric_id}/observations")
    async def metric_history(
        metric_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        start_at: float | None = None,
        end_at: float | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            require_project_scope(metric_id, project_id, actor=actor)
            rows = service.history(
                metric_id,
                scope=actor.tenant,
                limit=limit,
                start_at=start_at,
                end_at=end_at,
            )
        except (MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [row.model_dump(mode="json") for row in rows],
            "count": len(rows),
        }

    @router.get("/{metric_id}/current")
    async def current_metric(
        metric_id: str,
        request: Request,
        at: float | None = None,
        window_start: float | None = None,
        window_end: float | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            require_project_scope(metric_id, project_id, actor=actor)
            item = service.evaluate(
                metric_id,
                scope=actor.tenant,
                at=at,
                window_start=window_start,
                window_end=window_end,
            )
        except (MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{metric_id}/snapshots")
    async def capture_snapshot(
        metric_id: str,
        payload: MetricSnapshotRequest,
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            require_project_scope(metric_id, project_id, actor=actor)
            item = service.capture_snapshot(
                metric_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (AuthorizationError, MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/{metric_id}/snapshots")
    async def list_snapshots(
        metric_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            require_project_scope(metric_id, project_id, actor=actor)
            rows = service.snapshots(
                metric_id,
                scope=actor.tenant,
                limit=limit,
            )
        except (MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [row.model_dump(mode="json") for row in rows],
            "count": len(rows),
        }

    @router.get("/{metric_id}/snapshots/{snapshot_id}")
    async def get_snapshot(
        metric_id: str,
        snapshot_id: str,
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            require_project_scope(metric_id, project_id, actor=actor)
            item = service.get_snapshot(
                metric_id,
                snapshot_id,
                scope=actor.tenant,
            )
        except (MetricError, ValueError) as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    return router
