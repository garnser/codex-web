from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.artifact_evidence import (
    ArtifactCreate,
    EvidenceCreate,
    EvidenceEvaluationRequest,
    EvidenceRequirementsUpdate,
    InvalidationRequest,
    VerificationCreate,
)
from codex_web.services.artifact_evidence import (
    ArtifactEvidenceConflictError,
    ArtifactEvidenceError,
    ArtifactEvidenceService,
    ArtifactNotFoundError,
    EvidenceNotFoundError,
    VerificationNotFoundError,
)
from codex_web.services.identity import AuthorizationError, IdentityService, TenantIsolationError
from codex_web.services.resources import ResourceNotFoundError


def _error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (
            ArtifactNotFoundError,
            EvidenceNotFoundError,
            VerificationNotFoundError,
            ResourceNotFoundError,
        ),
    ):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ArtifactEvidenceConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ArtifactEvidenceError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_artifact_evidence_router(service: ArtifactEvidenceService) -> APIRouter:
    router = APIRouter(tags=["artifact-evidence"])

    @router.get("/api/artifacts")
    async def list_artifacts(
        request: Request,
        work_item_ref: str | None = None,
        include_inactive: bool = True,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list_artifacts(
                    actor,
                    work_item_ref=work_item_ref,
                    include_inactive=include_inactive,
                )
            ]
        }

    @router.post("/api/artifacts")
    async def create_artifact(payload: ArtifactCreate, request: Request) -> dict[str, Any]:
        try:
            item = service.create_artifact(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    AuthorizationError,
                    TenantIsolationError,
                    ResourceNotFoundError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/artifacts/{artifact_id}/invalidate")
    async def invalidate_artifact(
        artifact_id: str,
        payload: InvalidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.invalidate_artifact(
                artifact_id,
                payload.reason,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/evidence")
    async def list_evidence(
        request: Request,
        work_item_ref: str | None = None,
        include_inactive: bool = True,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list_evidence(
                    actor,
                    work_item_ref=work_item_ref,
                    include_inactive=include_inactive,
                )
            ]
        }

    @router.post("/api/evidence")
    async def create_evidence(payload: EvidenceCreate, request: Request) -> dict[str, Any]:
        try:
            item = service.create_evidence(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    AuthorizationError,
                    TenantIsolationError,
                    ResourceNotFoundError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/evidence/{evidence_id}/invalidate")
    async def invalidate_evidence(
        evidence_id: str,
        payload: InvalidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.invalidate_evidence(
                evidence_id,
                payload.reason,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/verifications")
    async def list_verifications(
        request: Request,
        work_item_ref: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list_verifications(
                    actor,
                    work_item_ref=work_item_ref,
                )
            ]
        }

    @router.post("/api/verifications")
    async def create_verification(
        payload: VerificationCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create_verification(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/evidence-requirements/work-items/{ref:path}")
    async def work_item_requirements(ref: str, request: Request) -> dict[str, Any]:
        try:
            requirements = service.work_item_requirements(
                ref,
                actor=request_actor(request),
            )
            return {
                "work_item_ref": ref,
                "requirements": [
                    item.model_dump(mode="json") for item in requirements
                ],
            }
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.put("/api/evidence-requirements/work-items/{ref:path}")
    async def set_work_item_requirements(
        ref: str,
        payload: EvidenceRequirementsUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            requirements = service.set_work_item_requirements(
                ref,
                payload.requirements,
                actor=request_actor(request),
            )
            return {
                "work_item_ref": ref,
                "requirements": [
                    item.model_dump(mode="json") for item in requirements
                ],
            }
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/evidence-evaluations/work-items/{ref:path}")
    async def evaluate_work_item_evidence(
        ref: str,
        payload: EvidenceEvaluationRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            requirements = (
                payload.requirements
                if payload.requirements
                else service.work_item_requirements(ref, actor=actor)
            )
            evaluation = service.evaluate(
                ref,
                requirements,
                actor=actor,
            )
            return evaluation.model_dump(mode="json")
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/artifact-evidence/expire-retention")
    async def expire_retention(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
            return service.expire_retention()
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    return router
