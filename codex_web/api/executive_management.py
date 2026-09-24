from __future__ import annotations

import asyncio

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.executive_roles import (
    ExecutiveActivationCreate,
    ExecutiveActivationStatus,
)
from codex_web.identity import (
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.executive_management import (
    ExecutiveAuthorityError,
    ExecutiveConsultationError,
    ExecutiveContextError,
    ExecutiveManagementError,
    ExecutiveManagementService,
    ExecutiveMaterializationError,
    ExecutiveSelectionError,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.executive_activations import (
    ExecutiveActivationConflictError,
    ExecutiveActivationNotFoundError,
)


def build_executive_management_router(
    service: ExecutiveManagementService,
    *,
    context_timeout_seconds: float = 5.0,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/executive",
        tags=["executive-management"],
    )

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, ExecutiveActivationNotFoundError):
            return HTTPException(
                status_code=404,
                detail="Executive activation not found",
            )
        if isinstance(
            exc,
            (
                ExecutiveActivationConflictError,
                ExecutiveConsultationError,
            ),
        ):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ExecutiveAuthorityError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(
            exc,
            (
                ExecutiveSelectionError,
                ExecutiveContextError,
                ExecutiveMaterializationError,
                ExecutiveManagementError,
                ValueError,
            ),
        ):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def consultant(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("executive:consult", "executive:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="executive:consult or executive:admin service scope required",
                )
        return actor

    def materializer(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("executive:materialize", "executive:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "executive:materialize or executive:admin "
                        "service scope required"
                    ),
                )
            return actor
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise HTTPException(
                status_code=403,
                detail=(
                    "administrator role required to materialize "
                    "Executive proposals"
                ),
            )
        try:
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    @router.get("/roles")
    async def roles(
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            catalog, reference = service.roles.resolve(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=project_id,
            )
        except Exception as exc:
            raise error(exc) from exc
        return {
            "catalog": catalog.model_dump(mode="json"),
            "definition": reference.model_dump(mode="json"),
        }

    @router.get("/activations")
    async def activations(
        request: Request,
        status: ExecutiveActivationStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        rows = list(service.list(actor=actor))
        if status is not None:
            rows = [item for item in rows if item.status == status]
        rows = rows[:limit]
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/activations")
    async def create_activation(
        payload: ExecutiveActivationCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = consultant(request)
        try:
            prepared = await asyncio.wait_for(
                asyncio.to_thread(
                    service.prepare,
                    payload,
                    actor=actor,
                ),
                timeout=max(0.001, float(context_timeout_seconds)),
            )
            item = service.persist_prepared(prepared, actor=actor)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "executive_context_timeout",
                    "message": (
                        "Executive activation context was not available within "
                        "the bounded request window; no activation was persisted"
                    ),
                    "retryable": True,
                },
            ) from exc
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/activations/{activation_id}")
    async def get_activation(
        activation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(activation_id, actor=actor)
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/activations/{activation_id}/revisions")
    async def activation_revisions(
        activation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.revisions(activation_id, actor=actor)
        except Exception as exc:
            raise error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("/activations/{activation_id}/consult")
    async def consult(
        activation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = consultant(request)
        try:
            item = await service.consult(
                activation_id,
                actor=actor,
            )
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post(
        "/activations/{activation_id}/proposals/{proposal_id}/materialize"
    )
    async def materialize(
        activation_id: str,
        proposal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = materializer(request)
        try:
            item = await service.materialize(
                activation_id,
                proposal_id,
                actor=actor,
            )
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    return router
