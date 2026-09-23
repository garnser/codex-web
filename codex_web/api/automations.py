from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.automation_definitions import (
    AutomationDefinitionService,
    AutomationScheduleMaterializationError,
    AutomationScheduleMaterializer,
)
from codex_web.services.automation_runs import (
    AutomationRunService,
    AutomationTriggerAdmissionBridge,
)
from codex_web.services.definitions import (
    DefinitionConflictError,
    DefinitionNotFoundError,
)
from codex_web.services.identity import AuthorizationError, IdentityService


class AutomationManualRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=500)
    project_id: str | None = None


class AutomationScheduleReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, DefinitionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DefinitionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AutomationScheduleMaterializationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_automations_router(
    definitions: AutomationDefinitionService,
    runs: AutomationRunService,
    triggers: AutomationTriggerAdmissionBridge,
    schedules: AutomationScheduleMaterializer,
) -> APIRouter:
    router = APIRouter(prefix="/api/automations", tags=["automations"])

    def require_schedule_admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "scheduler:admin" not in actor.service_scopes:
                raise AuthorizationError("scheduler:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("")
    async def list_automations(
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            items = definitions.list_effective(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=project_id,
            )
            return {
                "items": [
                    {
                        "id": automation_id,
                        "definition": definition.model_dump(mode="json"),
                        "definitionRef": reference.model_dump(mode="json"),
                    }
                    for automation_id, definition, reference in items
                ]
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{automation_id}")
    async def get_automation(
        automation_id: str,
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            definition, reference = definitions.resolve(
                automation_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=project_id,
            )
            return {
                "id": automation_id,
                "definition": definition.model_dump(mode="json"),
                "definitionRef": reference.model_dump(mode="json"),
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/{automation_id}/runs")
    async def list_runs(
        automation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in runs.store.list(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    automation_id=automation_id,
                )
            ]
        }

    @router.post("/{automation_id}/runs/manual")
    async def manual_run(
        automation_id: str,
        payload: AutomationManualRunRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            result = triggers.manual(
                automation_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=payload.project_id,
                actor_id=actor.identity_id,
                idempotency_key=payload.idempotency_key,
            )
            return {
                "run": result.run.model_dump(mode="json"),
                "inserted": result.inserted,
                "launchAllowed": result.launch_allowed,
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/{automation_id}/schedule/reconcile")
    async def reconcile_schedule(
        automation_id: str,
        payload: AutomationScheduleReconcileRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = require_schedule_admin(request)
            schedule = schedules.reconcile(
                automation_id,
                actor_id=actor.identity_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=payload.project_id,
            )
            return {
                "schedule": (
                    schedule.model_dump(mode="json")
                    if schedule is not None
                    else None
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    return router
