from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.automation_execution import AutomationExecutionService
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.automation_runs import AutomationRunNotFoundError


class AutomationLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    work_item_ref: str | None = None
    repository_resource_id: str | None = None
    read_only_repository_resource_ids: tuple[str, ...] = ()


def build_automation_execution_router(
    service: AutomationExecutionService,
) -> APIRouter:
    router = APIRouter(prefix="/api/automation-runs", tags=["automations"])

    def require_operator(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "automation:execute" not in actor.service_scopes:
                raise AuthorizationError("automation:execute service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.post("/{run_id}/launch")
    async def launch(
        run_id: str,
        payload: AutomationLaunchRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = require_operator(request)
            run = await service.launch(
                run_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                work_item_ref=payload.work_item_ref,
                repository_resource_id=payload.repository_resource_id,
                read_only_repository_resource_ids=(
                    payload.read_only_repository_resource_ids
                ),
            )
            return {"run": run.model_dump(mode="json")}
        except AutomationRunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Automation run not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
