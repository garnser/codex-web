"""Bounded Work custody operations over existing guarded owners, never a second runner."""
from __future__ import annotations

import time
from pydantic import BaseModel, ConfigDict, Field

from codex_web.procedure_control_plane import CanonicalOperationContract, CanonicalOperationResult
from codex_web.skill_procedures import ProcedureValueContract


class CustodyInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    work_item_ref: str = Field(min_length=1, max_length=240)
    target_assignment_id: str = Field(min_length=1, max_length=200)
    expected_target_fence: int = Field(ge=0)
    expected_handoff_requested_at: float = Field(ge=0, allow_inf_nan=False)
    original_preflight_id: str | None = Field(default=None, max_length=200)


def output_contract(operation):
    # Distinct output names avoid implicit shadowing/ambiguous previous inputs.
    return (ProcedureValueContract(name=operation.replace('.', '_'), type='string',
        description='Closed canonical custody or handoff disposition'),)


class CustodyOperation:
    def __init__(self, *, operation, workers, routing, threads, continuity, get_work, preflight, clock=None):
        self.operation = operation
        self.workers, self.routing, self.threads = workers, routing, threads
        self.continuity, self.get_work, self.preflight = continuity, get_work, preflight
        self.clock = clock or time.time

    def _work(self, context, value, *, accepted=False):
        context.validate_current()
        state = self.get_work(value.work_item_ref)
        a = context.assignment
        if ((state.organization_id, state.workspace_id, state.project_id)
                != (a.organization_id, a.workspace_id, a.project_id)
                or not state.resource_ids or set(state.resource_ids) - set(context.resource_ids)
                or (a.work_item_ref is not None and a.work_item_ref != state.ref)):
            raise ValueError('custody_work_target_context_missing_or_changed')
        h = state.handoff
        if (h is None or h.requested_at != value.expected_handoff_requested_at
                or h.status not in ({'pending', 'accepted'} if accepted else {'pending'})):
            raise ValueError('custody_handoff_changed')
        snapshot = self.routing.snapshot(state)
        if snapshot is None:
            raise ValueError('custody_owner_policy_unavailable')
        owner = snapshot.owner(h.from_agent if h.status == 'pending' else h.to_agent)
        if snapshot.owner(state.current_owner) != owner:
            raise ValueError('custody_sender_owner_changed')
        return state

    def _target(self, context, value):
        # Same normal worker-administration requirement; the Definition adds none.
        self.workers._require_admin(context.requester_actor)
        target = self.workers.store.assignment(value.target_assignment_id)
        a = context.assignment
        if (target is None or target.fence != value.expected_target_fence
                or (target.organization_id, target.workspace_id, target.project_id)
                    != (a.organization_id, a.workspace_id, a.project_id)
                or set(target.resource_ids) - set(context.resource_ids)
                or target.id == a.id):
            raise ValueError('custody_target_missing_foreign_or_changed')
        return target

    def _result(self, disposition, classification, *, facts=None, refs=()):
        return CanonicalOperationResult(disposition=disposition,
            outputs={output_contract(self.operation)[0].name:classification},
            facts=facts or {}, original_references=refs,
            code=None if disposition == 'succeeded' else classification)

    async def _inspect(self, context, value):
        state = self._work(context, value, accepted=True)
        target = self._target(context, value)
        facts = {'work_item_ref':state.ref, 'target_assignment_id':target.id,
            'target_fence':target.fence, 'target_status':str(target.status),
            'handoff_requested_at':state.handoff.requested_at,
            'current_owner':state.current_owner, 'next_owner':state.next_owner,
            'handoff_status':state.handoff.status}
        if value.original_preflight_id:
            original = self.preflight.get(value.original_preflight_id, actor=context.requester_actor)
            if ((original.organization_id, original.workspace_id, original.project_id)
                    != (target.organization_id, target.workspace_id, target.project_id)):
                raise ValueError('custody_original_scope_changed')
            correlation = original.queued_start_correlation
            if correlation is not None and correlation.assignment_id != target.id:
                raise ValueError('custody_original_assignment_changed')
            facts['original_preflight_id'] = original.id
            facts['original_status'] = str(original.status)
            if original.status in {'admitting', 'retrying', 'reconciliation_required'}:
                return state, target, 'native_unknown', facts
            # A positive known prestart refusal remains an original, not a new input.
            if original.status == 'blocked' and self.preflight.recorded_prestart_refusal(original):
                facts['known_prestart_blocked'] = True
        if target.status in {'cancelled', 'failed', 'succeeded', 'lost'} and target.lease is None:
            return state, target, 'terminal_retired', facts
        if target.lease is not None and target.lease.expires_at <= self.clock():
            facts['lease_expires_at'] = target.lease.expires_at
            return state, target, 'real_expired_lease', facts
        binding = self.routing.bootstraps.get_by_bootstrap(target.subject.ref, context.requester_actor)
        if (binding.assignment_id != target.id or binding.execution_id != target.execution_id
                or binding.execution_workspace_id != target.execution_workspace_id):
            raise ValueError('custody_bootstrap_changed')
        observation = await self.threads.runner(binding.thread_id)
        runner = observation.get('runner') or {}
        activity = observation.get('activity') or {}
        if runner.get('assignmentId') != target.id or runner.get('fence') != target.fence:
            raise ValueError('custody_runner_changed')
        if (activity.get('state') == 'active' or runner.get('pendingExecutorWork') != 0
                or self.continuity.thread_is_active(binding.thread_id)):
            return state, target, 'busy_or_completion_pending', facts
        if (activity.get('state') != 'idle' or activity.get('turnId') is not None
                or runner.get('processRunning') is not True or runner.get('live') is not True):
            return state, target, 'native_unknown', facts
        return state, target, 'positively_idle_live', facts

    async def execute(self, context, value):
        state, target, classification, facts = await self._inspect(context, value)
        if self.operation == 'work.custody.inspect.v1':
            return self._result('succeeded', classification, facts=facts)
        if self.operation == 'work.handoff.inspect.v1':
            return self._verify_delivery(context, value, state, facts)
        if classification in {'busy_or_completion_pending', 'native_unknown'}:
            return self._result('waiting', 'custody_before_effect:' + classification, facts=facts)
        if self.operation == 'work.custody.recover.v1':
            if classification == 'real_expired_lease':
                context.validate_current()
                self._work(context, value)
                self.workers.recover_expired(actor=context.requester_actor, assignment_id=target.id,
                    expected_fence=value.expected_target_fence)
                current = self.workers.store.assignment(target.id)
                if (current is None or current.fence != value.expected_target_fence
                        or current.status not in {'failed', 'lost'} or current.lease is not None):
                    return self._result('waiting', 'custody_expiry_guard_changed', facts=facts)
                return self._result('succeeded', 'canonical_expiry_recorded', facts=facts, refs=(current.id,))
            if classification == 'terminal_retired':
                # Delivery, including compatible successor preparation, remains
                # the normal handoff owner's exact-nonce reservation protocol.
                return self._result('succeeded', 'canonical_target_retired', facts=facts, refs=(target.id,))
            return self._result('succeeded', 'canonical_live_custody_verified', facts=facts)
        if self.operation == 'work.handoff.dispatch-existing.v1':
            context.validate_current()
            state = self._work(context, value)
            await self.continuity.dispatch_structured_handoff(state, source='canonical-procedure-custody')
            latest = self._work(context, value, accepted=True)
            return self._verify_delivery(context, value, latest, facts)
        raise ValueError('custody_operation_unavailable')

    async def reconcile(self, context, value):
        # An interrupted mutation must never repeat preparation/dispatch. The
        # canonical reservation/delivery/expiry owner remains authoritative.
        state, target, classification, facts = await self._inspect(context, value)
        if self.operation == 'work.custody.recover.v1' and classification == 'terminal_retired':
            return self._result('succeeded', 'canonical_target_retired', facts=facts, refs=(target.id,))
        if self.operation == 'work.handoff.dispatch-existing.v1':
            delivery = self._verify_delivery(context, value, state, facts)
            if delivery.disposition == 'succeeded':
                return delivery
        previous = context.previous_step
        # This marker is generated only before calling any mutation owner, is
        # backed by the engine's exact diagnostic Evidence, and is consumed by
        # its RUNNING CAS. Waiting/absence of START alone is never this witness.
        known_no_effect = (previous is not None and previous.status == 'waiting'
            and previous.diagnostic_evidence_ids and state.handoff.status == 'pending'
            and previous.operation_code in {'custody_before_effect:busy_or_completion_pending',
                'custody_before_effect:native_unknown'})
        if known_no_effect and classification in {'busy_or_completion_pending','native_unknown'}:
            # This reconciliation also called no mutation owner. Refresh the
            # positive witness rather than strand a still-busy original.
            return self._result('waiting','custody_before_effect:'+classification,facts=facts)
        if (known_no_effect
                and classification in {'positively_idle_live', 'real_expired_lease', 'terminal_retired'}):
            # Same immutable input/nonce/target fence and current routing guards
            # were rechecked above. The normal canonical dispatch/expiry owner
            # still owns admission and races; this is its first invocation here.
            return await self.execute(context, value)
        return self._result('waiting', 'custody_original_owner_reconciliation_required', facts=facts)

    def settled_retired_dispatch(self, assignment, value):
        """Observe an original dispatch after supervisor retirement, without effects.

        This is not execute/reconcile: it neither validates a fabricated LIVE
        context nor prepares, queues or advances any native/procedure work.
        """
        if self.operation != 'work.handoff.dispatch-existing.v1':
            raise ValueError('custody_historical_dispatch_owner_required')
        state = self.get_work(value.work_item_ref)
        if ((state.organization_id, state.workspace_id, state.project_id) !=
                (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                or state.ref != value.work_item_ref or not state.resource_ids
                or set(state.resource_ids) - set(assignment.resource_ids)):
            raise ValueError('custody_historical_work_scope_changed')
        self.routing.require_resolved_originals(state)
        candidates = [h for h in (*state.handoff_history, state.handoff) if h is not None
            and h.requested_at == value.expected_handoff_requested_at and h.status == 'accepted']
        # Normal archival may add reason_code to an otherwise identical ACK.
        # Only that archival annotation is non-authoritative here: every other
        # handoff and delivery field must still agree before START validation.
        if not candidates or any(h.model_dump(exclude={'reason_code'}) !=
                candidates[0].model_dump(exclude={'reason_code'}) for h in candidates[1:]):
            raise ValueError('custody_historical_ack_unavailable_or_conflicting')
        handoff = candidates[0]
        if (handoff.delivery is None or handoff.delivery.started_at is None
                or handoff.acknowledged_at is None
                or not handoff.requested_at <= handoff.delivery.started_at <= handoff.acknowledged_at):
            raise ValueError('custody_historical_ack_order_invalid')
        result = self._delivery_observation(assignment, assignment.resource_ids, state, handoff, {})
        if result.disposition != 'succeeded':
            raise ValueError(result.code or 'custody_historical_start_unavailable')
        attempt = self.preflight.store.get(result.original_references[0])
        correlation = attempt.queued_start_correlation if attempt is not None else None
        recipient = self.workers.store.assignment(correlation.assignment_id) if correlation is not None else None
        snapshot = attempt.queued_turn_snapshot or {} if attempt is not None else {}
        binding = recipient.agent_profile if recipient is not None else None
        if (attempt is None or attempt.status != 'started' or correlation is None or recipient is None
                or (correlation.assignment_id, correlation.fence) !=
                   (value.target_assignment_id, value.expected_target_fence)
                or attempt.queued_start_receipt.observed_at != handoff.delivery.started_at
                or attempt.queued_start_evidence.observed_at != handoff.delivery.started_at
                or (snapshot.get('work_item_ref'), snapshot.get('handoff_requested_at'), snapshot.get('thread_id')) !=
                   (value.work_item_ref, value.expected_handoff_requested_at, handoff.delivery.thread_id)
                or (recipient.organization_id, recipient.workspace_id, recipient.project_id,
                    recipient.execution_id, recipient.assigned_worker_id, recipient.created_by,
                    recipient.execution_profile_id, recipient.resource_ids) !=
                   (correlation.organization_id, correlation.workspace_id, correlation.project_id,
                    correlation.execution_id, correlation.worker_id, correlation.actor_identity_id,
                    correlation.execution_profile_id, correlation.assignment_resource_ids)
                or binding is None or (binding.profile_id, binding.profile_revision) !=
                   (correlation.agent_profile_id, correlation.agent_profile_revision)
                or not (recipient.fence == correlation.fence or recipient.fence == correlation.fence + 1
                    and recipient.status == 'cancelled' and recipient.lease is None)):
            raise ValueError('custody_historical_recipient_changed')
        from codex_web.services.queued_native_start import validate_attribution
        validate_attribution(self.preflight, attempt, correlation)
        return result

    def _verify_delivery(self, context, value, state, facts):
        return self._delivery_observation(context.assignment, context.resource_ids, state,
            state.handoff, facts)

    def _delivery_observation(self, assignment, resource_ids, state, h, facts):
        # Shared read-only receipt matcher. Historical scheduling disposition
        # uses a canonical retained handoff, never a fabricated current Work.
        delivery = h.delivery
        if (h.status != 'accepted' or h.acknowledged_at is None or delivery is None
                or delivery.status != 'delivered' or not delivery.scope_verified):
            return self._result('waiting', 'custody_actual_start_and_ack_required', facts=facts)
        # This complete exact Work index locates originals; the full retained
        # START tuple below still authorizes the observation. Unrelated lifetime
        # thread history cannot hide this Work's delivery.
        try:
            attempts = self.preflight.store.for_work_item(
                organization_id=assignment.organization_id,
                workspace_id=assignment.workspace_id,
                project_id=assignment.project_id, ref=state.ref)
        except ValueError:
            return self._result('waiting', 'custody_start_inventory_incomplete', facts=facts)
        matching = []
        for attempt in attempts:
            r, c, e = attempt.queued_start_receipt, attempt.queued_start_correlation, attempt.queued_start_evidence
            if (r is not None and c is not None and e is not None
                    and r.requested_execution_id == c.requested_execution_id == attempt.execution_id
                    and r.provider_native_session_id == c.provider_native_session_id == e.provider_native_session_id
                    and r.provider_id == c.provider_id and r.runtime_id == c.runtime_id
                    and e.provider_native_turn_id == r.provider_native_turn_id
                    and e.rpc_request_id == c.rpc_request_id and e.runtime_generation == c.runtime_generation
                    and c.execution_profile_id == delivery.execution_profile_id
                    and c.repository_resource_id == delivery.repository_resource_id
                    and c.writable_repository_resource_ids == delivery.writable_repository_resource_ids
                    and c.read_only_repository_resource_ids == delivery.read_only_repository_resource_ids
                    and attempt.queued_admission_claim_id == r.admission_claim_id
                    == c.admission_claim_id and r.provider_native_turn_id == delivery.turn_id
                    and r.execution_id == c.execution_id == delivery.execution_id
                    and r.thread_id == c.thread_id == delivery.thread_id
                    and c.fence > 0 and set(c.assignment_resource_ids).issubset(resource_ids)
                    and (c.organization_id, c.workspace_id, c.project_id)
                        == (assignment.organization_id, assignment.workspace_id, assignment.project_id)):
                matching.append((attempt, c))
        if len(matching) != 1:
            return self._result('waiting', 'custody_exact_start_proof_unavailable', facts=facts)
        attempt, correlation = matching[0]
        facts.update(native_turn_id=delivery.turn_id, assignment_id=correlation.assignment_id,
            fence=correlation.fence, actual_start=True, actual_ack=True)
        return self._result('succeeded', 'genuine_start_and_recipient_ack', facts=facts, refs=(attempt.id,))


def register_custody_operations(registry, **owners):
    for operation, effect in [('work.custody.inspect.v1', 'read'), ('work.custody.recover.v1', 'mutation'),
            ('work.handoff.dispatch-existing.v1', 'mutation'), ('work.handoff.inspect.v1', 'read')]:
        registry.register(CanonicalOperationContract(operation_id=operation, contract_version='1.0',
            resource_types=('repository',), required_capability='work_item.read' if effect == 'read' else 'work_item.steer',
            authority_level='read' if effect == 'read' else 'execute', input_model=CustodyInput,
            output_contracts=output_contract(operation), effect=effect, timeout_seconds=30),
            CustodyOperation(operation=operation, **owners))
