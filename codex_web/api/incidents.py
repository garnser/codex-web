from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.incidents import (
    IncidentActionRequest,
    IncidentDetect,
    IncidentEvidenceAttach,
    IncidentHandoff,
    IncidentPostmortemCreate,
    IncidentResolve,
    IncidentStatus,
    IncidentTriage,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.incidents import (
    IncidentConflictError,
    IncidentError,
    IncidentResolutionError,
    IncidentService,
)


def build_incidents_router(service: IncidentService) -> APIRouter:
    router = APIRouter(prefix="/api/incidents", tags=["incidents"])

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "incident:admin" not in actor.service_scopes:
                raise AuthorizationError("incident:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, (IncidentConflictError, IncidentResolutionError)):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, IncidentError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("")
    async def list_incidents(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list(actor)
            ]
        }

    @router.post("")
    async def detect_incident(
        payload: IncidentDetect,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.detect(payload, actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.get("/{incident_id}")
    async def get_incident(
        incident_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.get(incident_id, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except IncidentError as exc:
            raise error(exc) from exc

    @router.get("/{incident_id}/reasoning-budget")
    async def reasoning_budget(
        incident_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.reasoning_budget(
                incident_id,
                actor=request_actor(request),
            )
            return {"budget": item.model_dump(mode="json")}
        except IncidentError as exc:
            raise error(exc) from exc

    @router.post("/{incident_id}/triage")
    async def triage(
        incident_id: str,
        payload: IncidentTriage,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.triage(
                incident_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{incident_id}/handoff")
    async def handoff(
        incident_id: str,
        payload: IncidentHandoff,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.handoff(
                incident_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{incident_id}/status/{status}")
    async def transition(
        incident_id: str,
        status: IncidentStatus,
        request: Request,
        summary: str = Query(min_length=1, max_length=4000),
    ) -> dict[str, Any]:
        try:
            item = await service.transition(
                incident_id,
                status,
                actor=admin(request),
                summary=summary,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{incident_id}/actions")
    async def queue_action(
        incident_id: str,
        payload: IncidentActionRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.queue_action(
                incident_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{incident_id}/evidence")
    async def attach_evidence(
        incident_id: str,
        payload: IncidentEvidenceAttach,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.attach_evidence(
                incident_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{incident_id}/resolve")
    async def resolve(
        incident_id: str,
        payload: IncidentResolve,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.resolve(
                incident_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{incident_id}/postmortem")
    async def postmortem(
        incident_id: str,
        payload: IncidentPostmortemCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            incident, item = await service.create_postmortem(
                incident_id,
                payload,
                actor=admin(request),
            )
            return {
                "item": item.model_dump(mode="json"),
                "incident": incident.model_dump(mode="json"),
            }
        except Exception as exc:
            if isinstance(exc, (IncidentError, AuthorizationError)):
                raise error(exc) from exc
            raise

    return router
