"""Request-scoped adapter to the existing provider and ActionIntent engine.

No actor/worker credentials are accepted in action parameters, no binding/grant
is created, and no provider effect is retried from an existing intent.
"""
import asyncio
import hashlib
from codex_web.storage.storage_preservation import canonical
from contextlib import contextmanager
from contextvars import ContextVar

from codex_web.action_intents import (ActionDecisionOutcome, ActionDecisionSnapshot,
    ActionIntentClaimRequest, ActionIntentCreate, ActionIntentReconcileRequest, ActionIntentStatus)
from codex_web.action_providers import ActionRequest
from codex_web.services.resources import ResourceCatalogError
from codex_web.services.identity import AuthenticationError, AuthorizationError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_action_provider import RecoveryActionProvider
from codex_web.services.recovery_reconciliation import require_operator


class RecoveryOperatorActions:
    def __init__(self, recovery, intents, worker_actor, *, configuration=None):
        self.recovery, self.intents, self.worker_actor = recovery, intents, worker_actor
        self._request = ContextVar('recovery_operator_request', default=None)
        self.provider = RecoveryActionProvider(recovery, self.resolve_actor)
        self.intents.execution.registry.register(self.provider)
        self.storage_provider = None
        if configuration is not None:
            from codex_web.services.recovery_preservation_configuration import install_preservation_configuration
            from codex_web.services.recovery_preservation_provider import RecoveryPreservationProvider
            install_preservation_configuration(configuration)
            self.storage_provider = RecoveryPreservationProvider(recovery,configuration,
                intents.execution.resources,self.resolve_actor)
            self.intents.execution.registry.register(self.storage_provider)

    @contextmanager
    def request_context(self, authenticate):
        # ContextVars propagate to asyncio.to_thread but never to another HTTP request.
        token = self._request.set(authenticate)
        try:
            require_operator(authenticate())
            yield
        finally:
            self._request.reset(token)

    def resolve_actor(self, identity_id):
        authenticate = self._request.get()
        if authenticate is None:
            raise AuthenticationError('Recovery action requires a current authenticated operator request')
        actor = authenticate()
        if actor.identity_id != identity_id:
            raise AuthenticationError('Recovery action requester changed')
        require_operator(actor)
        return actor

    def describe(self, actor):
        bindings = self.intents.execution.registry.list_bindings(actor)
        blockers = []
        try:
            require_operator(actor)
        except AuthorizationError:
            blockers.append('Current Recovery administrator authority and MFA are required')
        if self.worker_actor.tenant != actor.tenant:
            blockers.append('No existing ActionIntent worker for this tenant')
        if not any(b.enabled and not b.resource_ids and b.provider_type == 'recovery'
                and b.provider_instance == 'canonical-recovery' for b in bindings):
            blockers.append('Explicit enabled canonical Recovery provider binding required')
        return {'registered': True, 'blockers': blockers, 'eligibility': 'Not asserted: current execution guards and independent writer/effect Evidence remain mandatory', 'action_id': 'recovery.scheduled-firing.reconcile',
            'bindings': [{'id': b.id, 'enabled': b.enabled} for b in bindings
                if b.provider_type == 'recovery' and b.provider_instance == 'canonical-recovery'
                and not b.resource_ids],
            'boundary': 'Binding/enabling is separate from authority and explicit Evidence-qualified state1.3 activation. Source startup performs neither.'}

    async def submit(self, event_id, binding_id, payload, *, actor):
        actor = self.resolve_actor(actor.identity_id)
        if event_id != payload.event_id:
            raise RecoveryConflictError('Path and immutable firing differ')
        if self.worker_actor.tenant != actor.tenant:
            raise RecoveryConflictError('No existing ActionIntent worker for this tenant')
        binding = self.intents.execution.registry.binding(binding_id, actor)
        if (binding.provider_type, binding.provider_instance) != ('recovery', 'canonical-recovery'):
            raise RecoveryConflictError('Recovery provider binding required')
        intent = await asyncio.to_thread(self.intents.create, ActionIntentCreate(
            binding_id=binding_id,
            request=ActionRequest(action_id='recovery.scheduled-firing.reconcile',
                organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                requested_by=actor.identity_id, parameters=payload.model_dump(mode='json'),
                idempotency_key=payload.idempotency_key),
            policy_decision=ActionDecisionSnapshot(outcome=ActionDecisionOutcome.ALLOW,
                source='policy:recovery-operator', reason='Authenticated Recovery operator requested the Evidence/CAS-bound operation')),
            actor=actor)
        # Same-key completed/claimed/uncertain requests are observations, not
        # permission for a second execute. Explicit readback uses reconcile().
        if intent.status != ActionIntentStatus.PENDING or intent.attempt:
            return intent
        worker = 'recovery-operator:' + intent.id
        claimed = await asyncio.to_thread(self.intents.claim,
            ActionIntentClaimRequest(worker_id=worker), actor=self.worker_actor, intent_id=intent.id)
        if claimed is None:
            return self.intents.get(intent.id, actor)
        return await self.intents.execute_claimed(intent.id, worker, actor=self.worker_actor)

    async def observe(self, event_id, intent_id, *, actor):
        actor = self.resolve_actor(actor.identity_id)
        intent = self.intents.get(intent_id, actor)
        if (intent.action_id != 'recovery.scheduled-firing.reconcile'
                or intent.requested_by != actor.identity_id
                or intent.request.parameters.get('event_id') != event_id):
            raise RecoveryConflictError('Intent does not bind this operator/firing')
        if self.worker_actor.tenant != actor.tenant:
            raise RecoveryConflictError('No existing ActionIntent worker for this tenant')
        # The human remains the authenticated requester/proof subject through
        # resolve_actor and the guarded provider readback. Canonical settlement
        # belongs to the SAME existing service worker used by submit/execute;
        # never lend its scopes to the human or weaken _require_worker.
        return await self.intents.reconcile(intent_id,
            ActionIntentReconcileRequest(retry_if_idempotent=False), actor=self.worker_actor)

    def describe_storage(self,actor):
        from codex_web.recovery_preservation import CAPTURE, VERIFY
        require_operator(actor)
        blockers=[]
        config=None
        if self.storage_provider is None:
            blockers.append('Preservation runtime configuration service unavailable')
        else:
            from codex_web.services.recovery_preservation_configuration import preservation_configuration
            try:
                config,_=preservation_configuration(self.storage_provider.operations.configuration)
                self.intents.execution.resources.get(config['resource_id'],actor)
            except (ValueError,LookupError,ResourceCatalogError):
                blockers.append('Published deployment Resource/memory/disk/wall configuration required')
        bindings=self.intents.execution.registry.list_bindings(actor)
        eligible=[b for b in bindings if b.provider_type=='recovery-storage'
            and b.provider_instance=='canonical-state-preservation' and b.enabled
            and config is not None and b.resource_ids==(config['resource_id'],)]
        if not eligible:
            blockers.append('Explicit enabled exact-Resource preservation binding required')
        return {'schema_version':'1.0','registered':self.storage_provider is not None,
            'blockers':blockers,'bindings':[{'id':b.id} for b in eligible],
            'configuration':config,'actions':[CAPTURE,VERIFY],
            **self.storage_incarnation(),
            'boundary':'Whole-StateStore opaque storage only. Independent fresh capacity Evidence, current MFA/authority/procedure/security, actual disk reservation and isolated verification required. No semantic Recovery PASS or source repair.'}

    def storage_incarnation(self):
        # Read only the CURRENT process admission owner, never a historical
        # firing, scheduler owner, PID or request-supplied identity.
        return {'recovery_executor_owner_id': self.recovery._scheduled_admission.owner_id,
            'incarnation_provenance': {
                'source': 'current_runtime.recovery_schedule_admission',
                'scope': 'current_process_incarnation',
                'restart_invalidates_capacity_evidence': True,
                'capacity_or_health_qualified': False}}

    def _storage_request(self,action,binding_id,payload,actor):
        from codex_web.recovery_preservation import ACTIONS
        if self.storage_provider is None or action not in ACTIONS:
            raise RecoveryConflictError('Preservation runtime unavailable')
        actor=self.resolve_actor(actor.identity_id)
        if self.worker_actor.tenant!=actor.tenant:
            raise RecoveryConflictError('No current ActionIntent worker for this tenant')
        binding=self.intents.execution.registry.binding(binding_id,actor)
        request=ActionRequest(action_id=action,organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,resource_ids=(payload.resource_id,),
            parameters=payload.model_dump(mode='json'),idempotency_key=payload.idempotency_key,
            requested_by=actor.identity_id)
        self.storage_provider._input(request,binding)
        return actor,binding,request

    async def preview_storage(self,action,binding_id,payload,*,actor):
        actor,binding,request=self._storage_request(action,binding_id,payload,actor)
        await self.intents.execution.prepare(binding.id,request,actor=actor)
        def inspect():
            _,config,pins,key=self.storage_provider.operations.current(payload,actor,capacity=False)
            stats=self.storage_provider.operations.inventory()
            return {'schema_version':'1.0','scope':'whole-StateStore',
                'inventory':{k:v for k,v in stats.items() if not k.startswith('_')},
                **self.storage_incarnation(),
                'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest(),
                'effective_budget':config,'key_id':key.id,'key_version':key.current_version,
                'semantic_recovery_qualified':False,'mutated':False,
                'next_action':'Supply fresh independently verified recovery-storage-capacity.v1 Evidence, confirm opaque preservation, then submit once. Unknown outcome uses observe, never replay.'}
        from codex_web.storage.state_store import StorageOperationBudget,storage_operation_budget
        _,config,_,_=self.storage_provider.operations.current(payload,actor,capacity=False)
        budget=StorageOperationBudget(config['wall_seconds'])
        def bounded():
            token=storage_operation_budget.set(budget)
            try:return inspect()
            finally:storage_operation_budget.reset(token)
        return await self.recovery._scheduled_executor.run('storage-preview:'+hashlib.sha256(canonical(
            {'actor':actor.identity_id,'binding':binding.id,'request':payload.model_dump(mode='json')})).hexdigest(),
            bounded,on_cancel=budget.cancelled.set)

    async def submit_storage(self,action,binding_id,payload,*,actor):
        actor,binding,request=self._storage_request(action,binding_id,payload,actor)
        _,config,_,_=self.storage_provider.operations.current(payload,actor)
        intent=await asyncio.to_thread(self.intents.create,ActionIntentCreate(binding_id=binding.id,
            request=request,timeout_seconds=config['wall_seconds'],
            policy_decision=ActionDecisionSnapshot(outcome=ActionDecisionOutcome.ALLOW,
                source='policy:recovery-operator',reason='Current authenticated operator explicitly requested Resource-bound opaque preservation, not domain recovery')),
            actor=actor)
        if intent.status!=ActionIntentStatus.PENDING or intent.attempt:
            return intent
        worker='recovery-storage:'+intent.id
        claimed=await asyncio.to_thread(self.intents.claim,ActionIntentClaimRequest(worker_id=worker,
            lease_seconds=config['wall_seconds']+5),actor=self.worker_actor,intent_id=intent.id)
        if claimed is None:
            return self.intents.get(intent.id,actor)
        return await self.intents.execute_claimed(intent.id,worker,actor=self.worker_actor)

    async def observe_storage(self,operation_id,intent_id,*,actor):
        from codex_web.recovery_preservation import ACTIONS
        actor=self.resolve_actor(actor.identity_id)
        intent=self.intents.get(intent_id,actor)
        if (intent.action_id not in ACTIONS or intent.requested_by!=actor.identity_id
            or intent.request.parameters.get('operation_id')!=operation_id
            or self.worker_actor.tenant!=actor.tenant):
            raise RecoveryConflictError('Preservation intent does not bind current operator/operation')
        return await self.intents.reconcile(intent_id,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
