from __future__ import annotations

from typing import Any
import asyncio

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.services.identity import AuthorizationError
from codex_web.services.local_installation_inspector import LocalInstallationInspector
from codex_web.services.artifact_evidence import ArtifactEvidenceError
from codex_web.services.recovery import RecoveryError, RecoveryConflictError
from codex_web.services.releases import ReleaseError
from codex_web.services.upgrades import (
    UpgradeConflictError,
    UpgradeError,
    UpgradePreflightError,
    UpgradeService,
)
from codex_web.upgrades import (
    UpgradeDefinitionMigrationCreate,
    UpgradePlanCreate,
    UpgradeLocalDeploymentQualificationRequest,
    UpgradeServiceDrainRequest,
    UpgradeStepExecute,
    UpgradeUnexecutedCancellationRequest,
)


def build_upgrades_router(service: UpgradeService, *, process_reader=None) -> APIRouter:
    router = APIRouter(prefix="/api/upgrades", tags=["upgrades"])
    inspector = LocalInstallationInspector(service, process_reader=process_reader)

    def admin(request: Request):
        actor = request_actor(request)
        UpgradeService.require_operator(actor)
        return actor

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, (UpgradeConflictError, UpgradePreflightError, RecoveryConflictError)):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, RecoveryError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, UpgradeError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("")
    async def list_plans(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in service.list(actor)
            ]
        }

    @router.post("")
    async def create_plan(
        payload: UpgradePlanCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.create(payload, actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.get("/{plan_id}")
    async def get_plan(
        plan_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.get(plan_id, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except UpgradeError as exc:
            raise error(exc) from exc

    @router.get("/{plan_id}/local-installation")
    async def local_installation(plan_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = admin(request)
            return await asyncio.to_thread(inspector.inspect, plan_id, actor=actor)
        except (ArtifactEvidenceError, ReleaseError) as exc:
            raise HTTPException(status_code=404, detail="installation evidence unavailable") from exc
        except (UpgradeError, AuthorizationError) as exc:
            raise error(exc) from exc

    @router.post("/{plan_id}/preflight")
    async def preflight(
        plan_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.preflight(plan_id, actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/drain")
    async def drain(
        plan_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.start_drain(plan_id, actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/service-drain")
    async def service_drain(
        plan_id: str,
        payload: UpgradeServiceDrainRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.start_service_drain(
                plan_id,
                actor=admin(request),
                service_instance_id=payload.service_instance_id,
                source_revision=payload.source_revision,
                target_revision=payload.target_revision,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/qualify-local-deployment")
    async def qualify_local_deployment(
        plan_id: str,
        payload: UpgradeLocalDeploymentQualificationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            qualification = service.qualify_local_deployment(
                plan_id, actor=admin(request),
                service_instance_id=payload.service_instance_id,
                source_revision=payload.source_revision,
                target_revision=payload.target_revision,
                preflight_evidence_id=payload.preflight_evidence_id,
            )
            return {"qualification": qualification}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/cancel-unexecuted")
    async def cancel_unexecuted(plan_id: str, payload: UpgradeUnexecutedCancellationRequest,
                               request: Request) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(service.cancel_unexecuted, plan_id, payload,
                                           actor=admin(request))
        except (UpgradeError, AuthorizationError) as exc:
            raise error(exc) from exc

    @router.post("/{plan_id}/backup")
    async def backup(
        plan_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.capture_pre_upgrade_backup(
                plan_id,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, RecoveryError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/steps/{step_id}/approval")
    async def step_approval(
        plan_id: str,
        step_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.request_step_approval(
                plan_id,
                step_id,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/steps/{step_id}/execute")
    async def execute_step(
        plan_id: str,
        step_id: str,
        payload: UpgradeStepExecute,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.execute_step(
                plan_id,
                step_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/definition-migrations")
    async def definition_migration(
        plan_id: str,
        payload: UpgradeDefinitionMigrationCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.record_definition_migration(
                plan_id,
                payload,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/verify")
    async def verify(
        plan_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.verify_post_upgrade(
                plan_id,
                actor=admin(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    @router.post("/{plan_id}/rollback")
    async def rollback(
        plan_id: str,
        request: Request,
        reason: str,
    ) -> dict[str, Any]:
        try:
            item = service.mark_rolled_back(
                plan_id,
                actor=admin(request),
                reason=reason,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (UpgradeError, AuthorizationError)):
                raise error(exc) from exc
            raise

    return router
