from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.company_operations import (
    CompanyOperationsError,
    CompanyOperationsNotFoundError,
    CompanyOperationsService,
    CompanyOperationsValidationError,
)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CompanyOperationsNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (CompanyOperationsValidationError, CompanyOperationsError, ValueError),
    ):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_company_operations_router(
    service: CompanyOperationsService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/company-operations",
        tags=["company-operations"],
    )

    @router.get("/overview")
    async def overview(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.overview(actor=actor)
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/explain/executive/{activation_id}")
    async def explain_executive(
        activation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.explain_executive_activation(
                activation_id,
                actor=actor,
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/explain/action-intent/{intent_id}")
    async def explain_action_intent(
        intent_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.explain_action_intent(
                intent_id,
                actor=actor,
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    return router
