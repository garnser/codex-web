from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.scheduler import ScheduleCreate
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.scheduler import ScheduleConflictError, ScheduleNotFoundError


def build_scheduler_router(service: SchedulerService) -> APIRouter:
    router = APIRouter(prefix="/api/schedules", tags=["schedules"])

    def require_reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("scheduler:read", "scheduler:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="scheduler:read or scheduler:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    def require_admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "scheduler:admin" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="scheduler:admin service scope required",
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

    def get_or_404(schedule_id: str):
        try:
            return service.get(schedule_id)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc

    @router.get("")
    async def list_schedules(request: Request) -> dict[str, Any]:
        require_reader(request)
        return {
            "schedules": [
                item.model_dump(mode="json")
                for item in service.list()
            ]
        }

    @router.post("", status_code=201)
    async def create_schedule(
        payload: ScheduleCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = require_admin(request)
        schedule = service.create(payload, actor_id=actor.identity_id)
        return {"schedule": schedule.model_dump(mode="json")}

    @router.get("/{schedule_id}")
    async def get_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        require_reader(request)
        return {"schedule": get_or_404(schedule_id).model_dump(mode="json")}

    def transition(operation, schedule_id: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            schedule = operation(schedule_id, actor_id=actor.identity_id)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        except ScheduleConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"schedule": schedule.model_dump(mode="json")}

    @router.post("/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        return transition(service.pause, schedule_id, request)

    @router.post("/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        return transition(service.resume, schedule_id, request)

    @router.post("/{schedule_id}/cancel")
    async def cancel_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        return transition(service.cancel, schedule_id, request)

    return router
