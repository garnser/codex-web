from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.automation_outcomes import (
    AutomationOutcomeReconciliationService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.automation_runs import AutomationRunNotFoundError


def build_automation_outcomes_router(
    service: AutomationOutcomeReconciliationService,
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

    @router.post("/{run_id}/reconcile")
    async def reconcile(run_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = require_operator(request)
            run = service.reconcile(run_id, actor=actor)
            return {"run": run.model_dump(mode="json")}
        except AutomationRunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Automation run not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
