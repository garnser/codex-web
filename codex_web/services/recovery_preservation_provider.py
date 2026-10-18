"""Trusted application-registered provider; binding/configuration grants nothing."""
from codex_web.action_providers import (ACTION_PROVIDER_CONTRACT,ActionDefinition,ActionCapability,
    ActionRiskClass,ActionResult,ActionEvidence,ActionVerification)
from codex_web.recovery_preservation import StoragePreservationRequest, CAPTURE, VERIFY, ACTIONS, SOURCE
from codex_web.resources import ResourceType
from codex_web.services.action_providers import ActionRequirementError, ActionProviderError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_preservation_operations import StoragePreservationOperations, fingerprint
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget
from codex_web.storage.storage_preservation import StoragePreservationRefused


class RecoveryPreservationProvider:
    contract_version=ACTION_PROVIDER_CONTRACT.current
    provider_type='recovery-storage'
    provider_instance='canonical-state-preservation'
    observation_operation='recovery-storage-preservation'

    def __init__(self,recovery,configuration,resources,resolve_actor):
        self.recovery,self.resolve_actor=recovery,resolve_actor
        self.operations=StoragePreservationOperations(recovery,configuration,resources,resolve_actor)

    def actions(self):
        return tuple(ActionDefinition(action_id=action,title=('Capture' if action==CAPTURE else 'Verify isolated')+' opaque StateStore preservation',
            description='Whole-StateStore storage integrity only; no repair, live restore or semantic Recovery qualification',
            capabilities=ActionCapability(prepare=True,execute=True,idempotency=True,verification=True,evidence=True),
            required_resource_types=(ResourceType.DATABASE,),required_authority=(action,),risk_class=ActionRiskClass.HIGH,
            timeout_seconds=300,retry_max_attempts=1,filesystem_access='write',expected_evidence=('storage-preservation',))
            for action in (CAPTURE,VERIFY))

    def _input(self,request,binding):
        if request.action_id not in ACTIONS or request.dry_run or request.credential_ref is not None:
            raise RecoveryConflictError('Unsupported storage preservation invocation')
        payload=StoragePreservationRequest.model_validate(request.parameters)
        actor=self.resolve_actor(request.requested_by)
        if ((actor.organization_id,actor.workspace_id)!=(request.organization_id,request.workspace_id)
            or (binding.organization_id,binding.workspace_id)!=(actor.organization_id,actor.workspace_id)
            or binding.provider_type!=self.provider_type or binding.provider_instance!=self.provider_instance
            or request.resource_ids!=(payload.resource_id,) or binding.resource_ids!=(payload.resource_id,)
            or payload.idempotency_key!=request.idempotency_key
            or (request.action_id==VERIFY)!=(payload.artifact_id is not None)):
            raise RecoveryConflictError('Preservation identity/Resource/request binding mismatch')
        return actor,payload

    def inspect_committed(self,request,binding,*,intent_id):
        actor,payload=self._input(request,binding)
        return self.operations.committed(payload,actor,intent_id=intent_id,
            action_id=request.action_id,binding_id=binding.id)

    async def prepare(self,request,*,binding):
        actor,payload=self._input(request,binding)
        self.operations.current(payload,actor,capacity=False)
        # The normal prepare path need not have an intent yet. Observation is
        # performed explicitly by readback with its canonical intent context.
        return {'operation':self.observation_operation,'scope':'whole-StateStore',
                'resource_id':payload.resource_id,'semantic_recovery_qualified':False,
                'next_action':'Explicit canonical current authority/capacity admission; never retry an unknown operation'}

    async def execute(self,request,*,binding,credential=None):
        actor,payload=self._input(request,binding)
        from codex_web.services.recovery_mutation_admission import mutation_guard,mutation_intent_id
        intent_id=mutation_intent_id()
        _,config,_,_=self.operations.current(payload,actor)
        budget=StorageOperationBudget(config['wall_seconds'])
        outer=storage_operation_budget.get()
        if outer is not None:
            budget.deadline=min(budget.deadline,outer.deadline)
        def work():
            token=storage_operation_budget.set(budget)
            try:
                admission=mutation_guard(self,binding,request)
                evidence=self.operations.execute(payload,actor,intent_id=intent_id,
                    action_id=request.action_id,binding_id=binding.id,admission=admission,filesystem=binding.security_policy.filesystem)
                return self._result(evidence,binding=binding)
            except (RecoveryConflictError,StoragePreservationRefused,ActionRequirementError):
                raise
            except Exception as exc:
                raise ActionProviderError('Preservation operation blocked: '+type(exc).__name__) from None
            finally:
                storage_operation_budget.reset(token)
        return await self.recovery._scheduled_executor.run('storage:'+intent_id,work,on_cancel=budget.cancelled.set)

    def _result(self,evidence,*,binding):
        m=evidence.metadata
        return ActionResult(execution_id='preservation-'+m['intent_id'],provider_binding_id=binding.id,
            action_id=m['action_id'],status='succeeded',idempotency_key=m['idempotency_key'],
            external_id=m['operation_id'],started_at=evidence.observed_at,completed_at=evidence.observed_at,
            output={'evidence_id':evidence.id,'artifact_id':evidence.artifact_ids[0],
                    'integrity_status':'verified','semantic_status':m['semantic_status'],
                    'semantic_recovery_qualified':False},
            evidence=(ActionEvidence(evidence_type='storage-preservation',reference=evidence.id,
                observed_at=evidence.observed_at,summary='Storage integrity only; not semantic Recovery PASS',
                metadata={'artifact_id':evidence.artifact_ids[0],'request_sha256':m['request_sha256']}),))

    async def readback(self,request,*,binding,intent_id):
        evidence=await self.recovery._scheduled_executor.run('storage-observe:'+intent_id,
            lambda:self.inspect_committed(request,binding,intent_id=intent_id))
        return self._result(evidence,binding=binding) if evidence is not None else None

    async def verify(self,result,*,binding,credential=None):
        def inspect():
            evidence=self.recovery.evidence.store.load()
            record=next((e for e in evidence.evidence if e.id==result.output.get('evidence_id')),None)
            if record is None:return False
            try:
                actor=self.resolve_actor(record.producer_identity_id)
                payload=StoragePreservationRequest.model_validate_json(record.metadata['request_json'])
                current=self.operations.committed(payload,actor,intent_id=record.metadata['intent_id'],
                    action_id=result.action_id,binding_id=binding.id)
                return current==record and result==self._result(record,binding=binding)
            except (ValueError,KeyError,RecoveryConflictError):
                return False
        valid=await self.recovery._scheduled_executor.run('storage-result-verification:'+result.execution_id,inspect)
        return ActionVerification(verified=valid,evidence=result.evidence if valid else ())

    async def rollback(self,result,*,binding,credential=None):
        raise ActionRequirementError('Preservation does not authorize destructive rollback or source repair')
