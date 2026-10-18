from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.services.resources import ResourceCatalogError
from codex_web.services.artifact_evidence import ArtifactEvidenceError
from codex_web.services.crypto_keys import CryptoKeyError
from codex_web.services.recovery_executor import RecoveryExecutorBusy, RecoveryExecutorClosed
from codex_web.services.recovery_policy import RecoveryPolicyControl
from codex_web.services.recovery_preservation_operations import PreservationDatabaseSettingDenied

from codex_web.api.identity import request_actor, revalidate_request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.recovery import RecoveryPolicy, RecoveryReconcileRequest
from codex_web.recovery_preservation import StoragePreservationRequest, StoragePreservationReportV1
from codex_web.api.action_intents import _error as action_error
from codex_web.services.action_intents import ActionIntentError
from codex_web.services.action_providers import ActionProviderError
from codex_web.services.identity import AuthenticationError, AuthorizationError, IdentityError, IdentityService
from codex_web.services.recovery import (
    RecoveryConflictError,
    RecoveryError,
    RecoveryService,
)


class RecoveryRollbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_fingerprint: str = Field(min_length=1)


class RecoveryOperatorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    binding_id: str = Field(min_length=1, max_length=200)
    request: RecoveryReconcileRequest


class StorageOperatorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    binding_id: str = Field(min_length=1,max_length=200)
    action: str
    request: StoragePreservationRequest


def build_recovery_router(service: RecoveryService, projects=None, *, actions=None, identity=None) -> APIRouter:
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
        if isinstance(exc, PreservationDatabaseSettingDenied):
            return HTTPException(status_code=409, detail={
                'code': exc.code, 'message': exc.message,
                'required_setting': 'data_directory', 'capacity_verified': False})
        if isinstance(exc, AuthenticationError):
            return HTTPException(status_code=401, detail='Current operator authentication required')
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
        except (RecoveryError, IdentityError, CryptoKeyError) as exc:
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
            "scheduled_work": {
                "source": "canonical RecoveryState",
                "firings": [item.model_dump(mode="json") for item in sorted(
                    (item for item in state.scheduled_firings.values()
                     if (item.organization_id, item.workspace_id) == (actor.organization_id, actor.workspace_id)),
                    key=lambda item: (item.admitted_at, item.event_id), reverse=True)],
                "reconciliations": [item.model_dump(mode="json") for item in state.reconciliations.values()
                    if (item.original.organization_id, item.original.workspace_id) == (actor.organization_id, actor.workspace_id)],
                "reconciliation_action": "recovery.scheduled-firing.reconcile",
                "reconciliation_availability": "Not reported here; inspect admitted canonical provider binding and current authority. Source declaration grants no execution permission.",
                "local_executor": service._scheduled_executor.status(),
                "operator_control": actions.describe(actor) if actions else {'registered': False, 'bindings': []},
            },
            "backups": backups,
            "verifications": verifications,
            "backup_schedule_id": state.backup_schedule_id,
            "verification_schedule_id": state.verification_schedule_id,
        }

    @router.get("/scheduled-firings/{event_id}/reconciliation-preview")
    async def reconciliation_preview(event_id: str, request: Request) -> dict[str, Any]:
        from codex_web.services.recovery_reconciliation import RecoveryReconciliationService
        actor = admin(request)
        try:
            if actions is not None and identity is not None:
                with actions.request_context(lambda: revalidate_request_actor(request, identity)):
                    return actions.provider.reconcile.preview(event_id, actor=actor)
            return RecoveryReconciliationService(service, lambda identity: admin(request)).preview(event_id,actor=actor)
        except (RecoveryError, IdentityError, CryptoKeyError) as exc:
            raise error(exc) from exc

    def operator_actions():
        if actions is None or identity is None:
            raise HTTPException(status_code=503, detail='Recovery operator integration unavailable')
        return actions

    @router.get('/storage-preservation/status')
    async def storage_status(request: Request):
        operator=operator_actions()
        try:
            return operator.describe_storage(admin(request))
        except (RecoveryError,IdentityError,ValueError,LookupError,ResourceCatalogError) as exc:
            raise error(exc) from exc

    @router.post('/storage-preservation/preview')
    async def storage_preview(payload: StorageOperatorRequest,request: Request):
        operator=operator_actions()
        try:
            with operator.request_context(lambda:revalidate_request_actor(request,identity)):
                return await operator.preview_storage(payload.action,payload.binding_id,payload.request,actor=admin(request))
        except (ActionIntentError,ActionProviderError) as exc:
            raise action_error(exc) from exc
        except (RecoveryError,IdentityError,CryptoKeyError,ValueError,ResourceCatalogError,ArtifactEvidenceError) as exc:
            raise error(exc) from exc

    @router.post('/storage-preservation/operations')
    async def storage_submit(payload: StorageOperatorRequest,request: Request):
        operator=operator_actions()
        try:
            with operator.request_context(lambda:revalidate_request_actor(request,identity)):
                item=await operator.submit_storage(payload.action,payload.binding_id,payload.request,actor=admin(request))
                return {'item':item.model_dump(mode='json'),
                    'next_action':'Read canonical intent/receipt. Unknown outcome is observe-only, never replay. Storage success is not semantic Recovery qualification.'}
        except (ActionIntentError,ActionProviderError) as exc:
            raise action_error(exc) from exc
        except (RecoveryError,IdentityError,CryptoKeyError,ValueError,ResourceCatalogError,ArtifactEvidenceError) as exc:
            raise error(exc) from exc

    @router.post('/storage-preservation/operations/{operation_id}/intents/{intent_id}/observe')
    async def storage_observe(operation_id: str,intent_id: str,request: Request):
        operator=operator_actions()
        try:
            with operator.request_context(lambda:revalidate_request_actor(request,identity)):
                item=await operator.observe_storage(operation_id,intent_id,actor=admin(request))
                return {'item':item.model_dump(mode='json')}
        except (ActionIntentError,ActionProviderError) as exc:
            raise action_error(exc) from exc
        except (RecoveryError,IdentityError,CryptoKeyError,ResourceCatalogError,ArtifactEvidenceError) as exc:
            raise error(exc) from exc

    @router.get('/storage-preservation/receipts/{intent_id}')
    async def storage_receipt(intent_id: str,request: Request):
        operator=operator_actions()
        try:
            with operator.request_context(lambda:revalidate_request_actor(request,identity)):
                actor=admin(request)
                intent=operator.intents.get(intent_id,actor)
                if operator.storage_provider is None:
                    raise RecoveryConflictError('Preservation runtime unavailable')
                binding=operator.intents.execution.registry.binding(intent.binding_id,actor)
                record=await service._scheduled_executor.run('storage-receipt:'+intent.id,
                    lambda:operator.storage_provider.inspect_committed(intent.request,binding,intent_id=intent.id))
                if record is None:
                    return {'schema_version':'1.0','outcome':'unknown','next_action':'Inspect retained intent/custody; never execute replay'}
                return {'schema_version':'1.0','outcome':'storage_verified','evidence_id':record.id,
                    'artifact_ids':list(record.artifact_ids),'report':StoragePreservationReportV1.model_validate_json(record.metadata['report_json']).model_dump(mode='json'),
                    'semantic_recovery_qualified':False}
        except (ActionIntentError,ActionProviderError) as exc:
            raise action_error(exc) from exc
        except (RecoveryError,IdentityError,CryptoKeyError,ValueError,ResourceCatalogError,ArtifactEvidenceError) as exc:
            raise error(exc) from exc

    @router.post('/scheduled-firings/{event_id}/reconcile')
    async def reconcile_firing(event_id: str, payload: RecoveryOperatorRequest, request: Request):
        operator = operator_actions()
        try:
            with operator.request_context(lambda: revalidate_request_actor(request, identity)):
                item = await operator.submit(event_id, payload.binding_id, payload.request, actor=admin(request))
                return {'item': item.model_dump(mode='json'),
                        'next_action': 'Inspect canonical ActionIntent/receipt; unknown requires read-only reconciliation, never effect replay'}
        except (ActionIntentError, ActionProviderError) as exc:
            raise action_error(exc) from exc
        except (RecoveryError, IdentityError, CryptoKeyError) as exc:
            raise error(exc) from exc

    @router.post('/scheduled-firings/{event_id}/reconciliation-intents/{intent_id}/observe')
    async def observe_firing(event_id: str, intent_id: str, request: Request):
        operator = operator_actions()
        try:
            with operator.request_context(lambda: revalidate_request_actor(request, identity)):
                item = await operator.observe(event_id, intent_id, actor=admin(request))
                return {'item': item.model_dump(mode='json')}
        except (ActionIntentError, ActionProviderError) as exc:
            raise action_error(exc) from exc
        except (RecoveryError, IdentityError, CryptoKeyError) as exc:
            raise error(exc) from exc

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

    def manual_authenticate(request):
        admin(request)
        if identity is None:
            raise HTTPException(status_code=503, detail='Current manual Recovery identity boundary unavailable')
        return lambda: revalidate_request_actor(request, identity)

    @router.post("/backups")
    async def backup(request: Request) -> dict[str, Any]:
        try:
            item = await service.run_manual(lambda actor: service.create_backup(actor=actor),
                authenticate=manual_authenticate(request), identity=identity)
            return {"item": item.model_dump(mode="json")}
        except (RecoveryExecutorBusy, RecoveryExecutorClosed) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, (RecoveryError, IdentityError, CryptoKeyError)):
                raise error(exc) from exc
            raise

    @router.post("/backups/{backup_id}/verify")
    async def verify(
        backup_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.run_manual(lambda actor: service.verify_restore(
                backup_id, actor=actor, publish_evidence=True),
                authenticate=manual_authenticate(request), identity=identity)
            return {"item": item.model_dump(mode="json")}
        except (RecoveryExecutorBusy, RecoveryExecutorClosed) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, (RecoveryError, IdentityError, CryptoKeyError)):
                raise error(exc) from exc
            raise

    return router
