from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.services.crypto_keys import CryptoKeyError
from codex_web.services.recovery_policy import RecoveryPolicyControl

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.recovery import RecoveryPolicy
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.recovery import (
    RecoveryConflictError,
    RecoveryError,
    RecoveryService,
)


class RecoveryRollbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_fingerprint: str = Field(min_length=1)


def build_recovery_router(service: RecoveryService, projects=None) -> APIRouter:
    router = APIRouter(prefix="/api/recovery", tags=["recovery"],
                      dependencies=[Depends(project_record_context_parameter)])
    control = RecoveryPolicyControl(service)

    def context(request: Request):
        ProjectRecordScope.from_request(request, request_actor(request), projects)

    router.dependencies.append(Depends(context))

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "recovery:admin" not in actor.service_scopes:
                raise AuthorizationError("recovery:admin service scope required")
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, RecoveryConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, RecoveryError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        state = service.store.load()
        try:
            control.require_scope(state, actor)
        except (RecoveryError, AuthorizationError, CryptoKeyError) as exc:
            raise error(exc) from exc
        try:
            admin(request)
            can_configure = True
        except AuthorizationError:
            can_configure = False
        backups = [
            item.model_dump(mode="json")
            for item in sorted(
                (
                    item
                    for item in state.backups.values()
                    if item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        ]
        verifications = [
            item.model_dump(mode="json")
            for item in sorted(
                (
                    item
                    for item in state.verifications.values()
                    if item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ),
                key=lambda item: (item.verified_at, item.id),
                reverse=True,
            )
        ]
        schedules = service.scheduler.store.load().schedules if service.scheduler else {}
        schedule_states = []
        for kind, record_id in [('backup', state.backup_schedule_id), ('verification', state.verification_schedule_id)]:
            record = schedules.get(record_id)
            if record is None or (record.tenant_id, record.workspace_id) != (actor.organization_id, actor.workspace_id):
                schedule_states.append({'kind': kind, 'status': 'unavailable'})
            else:
                schedule_states.append({'kind': kind, 'id': record.id, 'status': record.status.value,
                    'revision': record.revision, 'next_run_at': record.next_run_at,
                    'interval_seconds': record.recurrence.interval_seconds if record.recurrence else None})
        return {
            'policy_control': {
                'schema_version': '1.0', 'can_configure': can_configure,
                'scope': 'shared_workspace', 'organization_id': actor.organization_id, 'workspace_id': actor.workspace_id,
                'expected_fingerprint': state.policy.fingerprint() if state.policy else 'none',
                'schema': RecoveryPolicy.model_json_schema(),
                'destinations': [{'id': name} for name in sorted(service.destinations)],
                'history': [row.model_dump(mode='json') for row in reversed(state.policy_changes[-100:])],
                'history_truncated': len(state.policy_changes) > 100,
                'rollback_supported': True,
                'scheduler_available': service.scheduler is not None, 'schedules': schedule_states,
                'source': 'canonical RecoveryState',
            },
            "policy": (
                state.policy.model_dump(mode="json")
                if state.policy is not None
                else None
            ),
            "health": service.health(actor=actor).model_dump(mode="json"),
            "backups": backups,
            "verifications": verifications,
            "backup_schedule_id": state.backup_schedule_id,
            "verification_schedule_id": state.verification_schedule_id,
        }

    @router.put("/policy")
    async def configure(
        payload: RecoveryPolicy,
        request: Request,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        try:
            policy = service.configure(payload, actor=admin(request), expected_fingerprint=expected_fingerprint)
            return {"policy": policy.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (RecoveryError, AuthorizationError, CryptoKeyError)):
                raise error(exc) from exc
            raise

    @router.post('/policy/preview')
    async def preview_policy(payload: RecoveryPolicy, request: Request):
        try:
            return control.preview(payload, actor=admin(request))
        except (RecoveryError, AuthorizationError, CryptoKeyError) as exc:
            raise error(exc) from exc

    @router.post('/policy/rollback/{revision_id}')
    async def rollback_policy(revision_id: str, payload: RecoveryRollbackRequest, request: Request):
        try:
            policy = control.rollback(revision_id, actor=admin(request), expected_fingerprint=payload.expected_fingerprint)
            return {'policy': policy.model_dump(mode='json')}
        except (RecoveryError, AuthorizationError, CryptoKeyError) as exc:
            raise error(exc) from exc

    @router.post("/backups")
    async def backup(request: Request) -> dict[str, Any]:
        try:
            item = service.create_backup(actor=admin(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (RecoveryError, AuthorizationError, CryptoKeyError)):
                raise error(exc) from exc
            raise

    @router.post("/backups/{backup_id}/verify")
    async def verify(
        backup_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.verify_restore(
                backup_id,
                actor=admin(request),
                publish_evidence=True,
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (RecoveryError, AuthorizationError, CryptoKeyError)):
                raise error(exc) from exc
            raise

    return router
