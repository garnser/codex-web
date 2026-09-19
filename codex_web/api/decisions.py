from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.decisions import (
    DecisionApprovalRequest,
    DecisionCreate,
    DecisionPostExecutionReviewCreate,
    DecisionStatus,
    DecisionSupersedeRequest,
    DecisionUpdate,
)
from codex_web.identity import (
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.approval_requests import (
    ApprovalEligibilityError,
    ApprovalStateError,
    StaleApprovalTargetError,
)
from codex_web.services.decision_deliberation import (
    DecisionDeliberationError,
    DecisionDeliberationService,
)
from codex_web.services.decisions import (
    DecisionAuthorizationError,
    DecisionError,
    DecisionService,
    DecisionStateError,
    DecisionValidationError,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.approval_requests import (
    ApprovalRequestConflictError,
    ApprovalRequestNotFoundError,
)
from codex_web.storage.decisions import (
    DecisionConflictError,
    DecisionNotFoundError,
)


def build_decisions_router(
    service: DecisionService,
    deliberation: DecisionDeliberationService,
) -> APIRouter:
    router = APIRouter(prefix="/api/decisions", tags=["decisions"])

    def serialize(item) -> dict[str, Any]:
        return item.model_dump(mode="json")

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, (DecisionNotFoundError, ApprovalRequestNotFoundError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(
            exc,
            (
                DecisionConflictError,
                DecisionStateError,
                ApprovalRequestConflictError,
                ApprovalStateError,
                StaleApprovalTargetError,
            ),
        ):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(
            exc,
            (
                DecisionAuthorizationError,
                ApprovalEligibilityError,
                AuthorizationError,
            ),
        ):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(
            exc,
            (
                DecisionValidationError,
                DecisionDeliberationError,
                DecisionError,
                ValueError,
            ),
        ):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def writer(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("decisions:write", "decisions:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="decisions:write or decisions:admin service scope required",
                )
        return actor

    def approval_finalizer(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "decisions:admin" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="decisions:admin service scope required",
                )
            return actor
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise HTTPException(
                status_code=403,
                detail="administrator role required to finalize Decision approval",
            )
        try:
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    @router.get("")
    async def list_decisions(
        request: Request,
        status: DecisionStatus | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        rows = list(service.list(actor=actor))
        if status is not None:
            rows = [item for item in rows if item.status == status]
        if project_id is not None:
            rows = [item for item in rows if item.project_id == project_id]
        return {
            "items": [serialize(item) for item in rows],
            "count": len(rows),
        }

    @router.post("")
    async def create_decision(
        payload: DecisionCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = await service.create(payload, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": serialize(item)}

    @router.get("/{decision_id}")
    async def get_decision(
        decision_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(decision_id, actor=actor)
            approval = (
                service.approvals.get(item.approval_request_id, actor=actor)
                if item.approval_request_id
                else None
            )
            usage = deliberation.model_gateway.decision_usage(
                item.id,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "item": serialize(item),
            "approval_request": serialize(approval) if approval else None,
            "model_usage": usage.model_dump(mode="json"),
        }

    @router.patch("/{decision_id}")
    async def revise_decision(
        decision_id: str,
        payload: DecisionUpdate,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = await service.revise(
                decision_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": serialize(item)}

    @router.get("/{decision_id}/revisions")
    async def decision_revisions(
        decision_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.revisions(decision_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "items": [serialize(item) for item in rows],
            "count": len(rows),
        }

    @router.get("/{decision_id}/events")
    async def decision_events(
        decision_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.events(decision_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "items": [serialize(item) for item in rows],
            "count": len(rows),
        }

    @router.post("/{decision_id}/deliberate")
    async def deliberate_decision(
        decision_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = await deliberation.deliberate(
                decision_id,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": serialize(item)}

    @router.post("/{decision_id}/approval-request")
    async def request_decision_approval(
        decision_id: str,
        payload: DecisionApprovalRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = await service.request_approval(
                decision_id,
                payload,
                actor=actor,
            )
            approval = service.approvals.get(
                item.approval_request_id or "",
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "item": serialize(item),
            "approval_request": serialize(approval),
        }

    @router.post("/{decision_id}/approval/finalize")
    async def finalize_decision_approval(
        decision_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = approval_finalizer(request)
        try:
            item = await service.finalize_approval(
                decision_id,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": serialize(item)}

    @router.post("/{decision_id}/supersede")
    async def supersede_decision(
        decision_id: str,
        payload: DecisionSupersedeRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = await service.supersede(
                decision_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": serialize(item)}

    @router.post("/{decision_id}/reviews")
    async def post_execution_review(
        decision_id: str,
        payload: DecisionPostExecutionReviewCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = writer(request)
        try:
            item = await service.add_post_execution_review(
                decision_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"item": serialize(item)}

    return router
