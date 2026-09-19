from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.api.identity import request_actor

from codex_web.models import ApprovalDecision
from codex_web.services.approvals import ApprovalService


def build_approvals_router(service: ApprovalService) -> APIRouter:
    router = APIRouter(tags=["approvals"])

    @router.get("/api/approvals")
    async def approvals() -> list[dict[str, Any]]:
        return service.list()

    @router.post("/api/approvals/{request_id}")
    async def decide_approval(
        request_id: str,
        payload: ApprovalDecision,
        request: Request,
    ) -> dict[str, bool]:
        return await service.decide(
            request_id,
            payload.decision,
            actor=request_actor(request),
        )

    return router
