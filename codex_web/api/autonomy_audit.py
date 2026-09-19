from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.autonomy_audit import (
    AutonomyReliabilityPolicy,
    AutonomySafetySignalCreate,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.autonomy_audit import (
    AutonomyAuditError,
    AutonomyAuditIntegrityError,
    AutonomyAuditService,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def build_autonomy_audit_router(service: AutonomyAuditService) -> APIRouter:
    router = APIRouter(prefix="/api/autonomy/audit", tags=["autonomy-audit"])

    def reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not {"autonomy:read", "autonomy:audit", "autonomy:admin"}.intersection(
                actor.service_scopes
            ):
                raise HTTPException(
                    status_code=403,
                    detail="autonomy read/audit scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "autonomy:audit" not in actor.service_scopes and "autonomy:admin" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="autonomy:audit or autonomy:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    @router.get("")
    async def list_records(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        actor = reader(request)
        rows = service.list_records(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            limit=limit,
        )
        return {
            "items": [service.public_record(item) for item in rows],
            "count": len(rows),
        }

    @router.get("/metrics")
    async def metrics(request: Request) -> dict[str, Any]:
        actor = reader(request)
        return service.metrics(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        ).model_dump(mode="json")

    @router.get("/checkpoints")
    async def checkpoints(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = reader(request)
        rows = service.list_checkpoints(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.get("/signals")
    async def signals(
        request: Request,
        active_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        actor = reader(request)
        rows = service.list_signals(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            active_only=active_only,
        )
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.get("/{record_id}")
    async def get_record(record_id: str, request: Request) -> dict[str, Any]:
        actor = reader(request)
        try:
            item = service.get(
                record_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
        except AutonomyAuditError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"item": service.public_record(item)}

    @router.post("/verify")
    async def verify(
        request: Request,
        publish_evidence: bool = Query(default=False),
    ) -> dict[str, Any]:
        actor = admin(request)
        if publish_evidence:
            try:
                evidence, result = service.publish_integrity_evidence(actor=actor)
            except AutonomyAuditError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "result": result.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
            }
        result = service.verify(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        return {"result": result.model_dump(mode="json")}

    @router.post("/checkpoints")
    async def checkpoint(request: Request) -> dict[str, Any]:
        actor = admin(request)
        try:
            item = service.checkpoint(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
        except AutonomyAuditIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/{record_id}/redact")
    async def redact(
        record_id: str,
        request: Request,
        reason: str = Query(min_length=1, max_length=4000),
    ) -> dict[str, Any]:
        actor = admin(request)
        try:
            item = service.redact(record_id, reason, actor=actor)
        except AutonomyAuditError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"item": item.model_dump(mode="json")}

    @router.put("/reliability-policy")
    async def reliability_policy(
        payload: AutonomyReliabilityPolicy,
        request: Request,
    ) -> dict[str, Any]:
        actor = admin(request)
        policy = service.set_reliability_policy(
            payload,
            actor=actor,
        )
        return {"policy": policy.model_dump(mode="json")}

    @router.post("/metrics/evidence")
    async def reliability_evidence(request: Request) -> dict[str, Any]:
        actor = admin(request)
        try:
            evidence, metrics = service.publish_reliability_evidence(
                actor=actor,
            )
        except AutonomyAuditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "evidence": evidence.model_dump(mode="json"),
            "metrics": metrics.model_dump(mode="json"),
        }

    @router.post("/signals")
    async def add_signal(
        payload: AutonomySafetySignalCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = admin(request)
        item = service.add_signal(payload, actor=actor)
        return {"item": item.model_dump(mode="json")}

    @router.post("/signals/{signal_id}/clear")
    async def clear_signal(signal_id: str, request: Request) -> dict[str, Any]:
        admin(request)
        try:
            item = service.clear_signal(signal_id)
        except AutonomyAuditError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"item": item.model_dump(mode="json")}

    return router
