from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalDecisionSubmit,
    ApprovalRequestCreate,
)
from codex_web.identity import (
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.approval_requests import (
    ApprovalEligibilityError,
    ApprovalRequestService,
    ApprovalStateError,
    StaleApprovalTargetError,
)
from codex_web.services.identity import (
    AuthorizationError,
    IdentityError,
    IdentityService,
)
from codex_web.storage.approval_requests import (
    ApprovalRequestConflictError,
    ApprovalRequestNotFoundError,
)


class ApprovalSupersedeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    replacement_request_id: str = Field(min_length=1, max_length=500)


def build_approval_requests_router(service: ApprovalRequestService) -> APIRouter:
    router = APIRouter(
        prefix="/api/approval-requests",
        tags=["approval-requests"],
    )

    def serialize(item) -> dict[str, Any]:
        return item.model_dump(mode="json")

    def translate_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ApprovalRequestNotFoundError):
            return HTTPException(status_code=404, detail="approval request not found")
        if isinstance(exc, ApprovalEligibilityError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(
            exc,
            (
                ApprovalRequestConflictError,
                ApprovalStateError,
                StaleApprovalTargetError,
            ),
        ):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (IdentityError, AuthorizationError)):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def require_requester(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("approvals:request", "approvals:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="approvals:request or approvals:admin service scope required",
                )
        return actor

    def require_consumer(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("approvals:consume", "approvals:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="approvals:consume or approvals:admin service scope required",
                )
            return actor
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise HTTPException(
                status_code=403,
                detail="administrator role required to consume approval via API",
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
    async def list_requests(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "approval_requests": [
                serialize(item)
                for item in service.list(actor)
            ]
        }

    @router.post("", status_code=201)
    async def create_request(
        payload: ApprovalRequestCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = require_requester(request)
        try:
            item = await service.create(payload, requester=actor)
        except Exception as exc:
            raise translate_error(exc) from exc
        return {"approval_request": serialize(item)}

    @router.get("/{request_id}")
    async def get_request(
        request_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(request_id, actor=actor)
        except Exception as exc:
            raise translate_error(exc) from exc
        return {"approval_request": serialize(item)}

    @router.post("/{request_id}/decisions")
    async def submit_decision(
        request_id: str,
        payload: ApprovalDecisionSubmit,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.decide(
                request_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        return {"approval_request": serialize(item)}

    @router.post("/{request_id}/cancel")
    async def cancel_request(
        request_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.cancel(request_id, actor=actor)
        except Exception as exc:
            raise translate_error(exc) from exc
        return {"approval_request": serialize(item)}

    @router.post("/{request_id}/supersede")
    async def supersede_request(
        request_id: str,
        payload: ApprovalSupersedeRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.supersede(
                request_id,
                replacement_request_id=payload.replacement_request_id,
                actor=actor,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        return {"approval_request": serialize(item)}

    @router.post("/{request_id}/consume")
    async def consume_request(
        request_id: str,
        payload: ApprovalConsumeRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = require_consumer(request)
        try:
            item = await service.consume(
                request_id,
                payload,
                actor=actor,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        return {"approval_request": serialize(item)}

    return router
