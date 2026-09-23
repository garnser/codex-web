from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.automation_definitions import AutomationDefinition
from codex_web.definitions import DefinitionScope
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
    DefinitionApprovalRequiredError,
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionNotFoundError,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService


class AutomationManualRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=500)
    project_id: str | None = None


class AutomationScheduleReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None


class AutomationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition: AutomationDefinition
    project_id: str | None = None
    reason: str | None = None
    derived_from_record_id: str | None = None


class AutomationPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = None
    expected_active_revision: int | None = Field(default=None, ge=1)
    approval_metadata: dict[str, str] = Field(default_factory=dict)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, DefinitionApprovalRequiredError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "definition_approval_required",
                "message": str(exc),
                "record_id": exc.record_id,
                "reasons": list(exc.reasons),
            },
        )
    if isinstance(exc, DefinitionCompatibilityError):
        return HTTPException(status_code=409, detail=str(exc))
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
    projects: ProjectService | None = None,
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

    def require_definition_admin(request: Request, project_id: str | None = None):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "definitions:admin" not in actor.service_scopes:
                raise AuthorizationError("definitions:admin service scope required")
        else:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        if project_id:
            if projects is None:
                raise AuthorizationError(
                    "Project-scoped Automation editing requires canonical ProjectService"
                )
            try:
                projects.get(project_id, actor.tenant)
            except ProjectNotFoundError as exc:
                raise AuthorizationError("cross-tenant Project Automation edit denied") from exc
        return actor

    def editable_record(record_id: str, request: Request):
        record = definitions.registry.get_record(record_id)
        if record.kind != "automation":
            raise DefinitionConflictError("Definition record is not an Automation")
        if record.scope_type == DefinitionScope.WORKSPACE:
            if record.scope_id != request_actor(request).workspace_id:
                raise AuthorizationError("cross-tenant Automation draft denied")
            project_id = None
        elif record.scope_type == DefinitionScope.PROJECT:
            project_id = record.scope_id
        else:
            raise AuthorizationError(
                "first-class Automation editing supports workspace/Project scope only"
            )
        actor = require_definition_admin(request, project_id)
        return record, actor, project_id

    @router.post("/drafts")
    async def create_automation_draft(
        payload: AutomationDraftRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = require_definition_admin(request, payload.project_id)
            scope_type = (
                DefinitionScope.PROJECT
                if payload.project_id
                else DefinitionScope.WORKSPACE
            )
            scope_id = payload.project_id or actor.workspace_id
            record = definitions.create_draft(
                payload.definition.name.lower().replace(" ", "-"),
                payload.definition,
                actor_id=actor.identity_id,
                scope_type=scope_type,
                scope_id=scope_id,
                reason=payload.reason,
                derived_from_record_id=payload.derived_from_record_id,
            )
            return {"record": record.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/drafts/{record_id}/publish")
    async def publish_automation_draft(
        record_id: str,
        payload: AutomationPublishRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            record, actor, project_id = editable_record(record_id, request)
            published = definitions.publish_draft(
                record.record_id,
                actor_id=actor.identity_id,
                reason=payload.reason,
                expected_active_revision=payload.expected_active_revision,
                approval_metadata=payload.approval_metadata,
            )
            schedule = None
            schedule_error = None
            try:
                schedule = schedules.reconcile(
                    published.definition_id,
                    actor_id=actor.identity_id,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                )
            except AutomationScheduleMaterializationError as exc:
                # Publication already succeeded; report schedule repair separately
                # rather than pretending the versioned Definition mutation failed.
                schedule_error = str(exc)
            return {
                "record": published.model_dump(mode="json"),
                "schedule": (
                    schedule.model_dump(mode="json")
                    if schedule is not None
                    else None
                ),
                "scheduleError": schedule_error,
            }
        except Exception as exc:
            raise _error(exc) from exc

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
