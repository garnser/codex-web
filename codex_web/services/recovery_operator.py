"""Request-scoped adapter to the existing provider and ActionIntent engine.

No actor/worker credentials are accepted in action parameters, no binding/grant
is created, and no provider effect is retried from an existing intent.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

from codex_web.action_intents import (ActionDecisionOutcome, ActionDecisionSnapshot,
    ActionIntentClaimRequest, ActionIntentCreate, ActionIntentReconcileRequest, ActionIntentStatus)
from codex_web.action_providers import ActionRequest
from codex_web.services.identity import AuthenticationError, AuthorizationError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_action_provider import RecoveryActionProvider
from codex_web.services.recovery_reconciliation import require_operator


class RecoveryOperatorActions:
    def __init__(self, recovery, intents, worker_actor):
        self.recovery, self.intents, self.worker_actor = recovery, intents, worker_actor
        self._request = ContextVar('recovery_operator_request', default=None)
        self.provider = RecoveryActionProvider(recovery, self.resolve_actor)
        self.intents.execution.registry.register(self.provider)

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
