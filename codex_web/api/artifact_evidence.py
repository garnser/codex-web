from __future__ import annotations

import tempfile
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from codex_web.api.identity import request_actor
from codex_web.artifact_content import (
    ArtifactContentAccessError,
    ArtifactContentError,
    ArtifactContentNotFoundError,
    ArtifactContentRange,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
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
            ArtifactContentNotFoundError,
        ),
    ):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AuthorizationError, TenantIsolationError, ArtifactContentAccessError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ArtifactEvidenceConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ArtifactEvidenceError, ArtifactContentError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_artifact_evidence_router(service: ArtifactEvidenceService) -> APIRouter:
    router = APIRouter(tags=["artifact-evidence"])

    def require_artifact_evidence_admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "artifact-evidence:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "artifact-evidence:admin service scope required"
                )
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

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

    @router.put("/api/artifacts/{artifact_id}/content")
    async def put_artifact_content(
        artifact_id: str,
        request: Request,
        backend_id: str | None = None,
        media_type: str | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
                async for chunk in request.stream():
                    if chunk:
                        body.write(chunk)
                body.seek(0)

                def chunks() -> Iterator[bytes]:
                    while True:
                        chunk = body.read(65536)
                        if not chunk:
                            break
                        yield chunk

                item = service.attach_artifact_content(
                    artifact_id,
                    chunks(),
                    actor=request_actor(request),
                    backend_id=backend_id,
                    media_type=media_type or request.headers.get("content-type"),
                    expected_sha256=expected_sha256,
                )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    ArtifactContentError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/artifacts/{artifact_id}/content")
    async def get_artifact_content(
        artifact_id: str,
        request: Request,
        start: int | None = None,
        end_exclusive: int | None = None,
    ):
        try:
            byte_range = None
            if start is not None or end_exclusive is not None:
                if start is None or end_exclusive is None:
                    raise ArtifactContentError(
                        "both start and end_exclusive are required for a range read"
                    )
                byte_range = ArtifactContentRange(
                    start=start,
                    end_exclusive=end_exclusive,
                )
            pointer, stream = service.open_artifact_content(
                artifact_id,
                actor=request_actor(request),
                byte_range=byte_range,
            )
            headers = {
                "Accept-Ranges": "bytes",
                "X-Content-Backend": pointer.backend_id,
            }
            if byte_range is None:
                headers["Content-Length"] = str(pointer.size_bytes)
            else:
                length = max(
                    0,
                    min(byte_range.end_exclusive, pointer.size_bytes)
                    - byte_range.start,
                )
                headers["Content-Length"] = str(length)
                headers["Content-Range"] = (
                    f"bytes {byte_range.start}-"
                    f"{max(byte_range.start, byte_range.start + length - 1)}/"
                    f"{pointer.size_bytes}"
                )
            return StreamingResponse(
                stream,
                media_type=pointer.media_type or "application/octet-stream",
                headers=headers,
                status_code=206 if byte_range is not None else 200,
            )
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    ArtifactContentError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/artifacts/{artifact_id}/content/verify")
    async def verify_artifact_content(
        artifact_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.verify_artifact_content(
                artifact_id,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    ArtifactContentError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/artifacts/{artifact_id}/content/migrate")
    async def migrate_artifact_content(
        artifact_id: str,
        request: Request,
        target_backend_id: str,
        delete_source: bool = True,
    ) -> dict[str, Any]:
        try:
            item = service.migrate_artifact_content(
                artifact_id,
                target_backend_id,
                actor=request_actor(request),
                delete_source=delete_source,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    ArtifactContentError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.delete("/api/artifacts/{artifact_id}/content")
    async def delete_artifact_content(
        artifact_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.delete_artifact_content(
                artifact_id,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(
                exc,
                (
                    ArtifactEvidenceError,
                    ArtifactContentError,
                    AuthorizationError,
                    TenantIsolationError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/artifact-content/backends")
    async def artifact_content_backends(request: Request) -> dict[str, Any]:
        request_actor(request)
        if service.content is None:
            return {"items": []}
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.content.health()
            ]
        }

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
            actor = require_artifact_evidence_admin(request)
            requirements = service.set_work_item_requirements(
                ref,
                payload.requirements,
                actor=actor,
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

    @router.post("/api/artifact-evidence/governance/sync")
    async def sync_governance(request: Request) -> dict[str, Any]:
        try:
            actor = require_artifact_evidence_admin(request)
            return service.sync_governance_records(actor=actor)
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/artifact-evidence/expire-retention")
    async def expire_retention(request: Request) -> dict[str, Any]:
        try:
            require_artifact_evidence_admin(request)
            return service.expire_retention()
        except Exception as exc:
            if isinstance(
                exc,
                (ArtifactEvidenceError, AuthorizationError, TenantIsolationError),
            ):
                raise _error(exc) from exc
            raise

    return router
