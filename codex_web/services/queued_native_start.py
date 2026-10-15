"""Internal positive-start capture; no provider reads, replay, or proof-input API."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from codex_web.models import (
    QueuedNativeStartCorrelation, QueuedNativeStartEvidence, QueuedNativeStartReceipt, QueuedTurn,
    ExecutionPreflightBlocker, ExecutionPreflightBlockerSnapshot,
)


class NativeStartAdmissionRefused(RuntimeError):
    """Server witness: an admission guard refused before the native write."""


def validate_attribution(service, current, correlation):
    snapshot = current.queued_turn_snapshot
    original = QueuedTurn.model_validate(snapshot) if snapshot is not None else None
    if (snapshot is None
            or service.queued_snapshot_digest(snapshot) != correlation.snapshot_sha256
            or current.queued_admission_claim_id != correlation.admission_claim_id
            or current.execution_id != correlation.requested_execution_id
            or (current.replacement_thread_id or current.thread_id) != correlation.thread_id
            or any(getattr(current, name) != getattr(correlation, name) for name in (
                'organization_id', 'workspace_id', 'project_id', 'agent_profile_actor_id',
                'agent_profile_id', 'agent_profile_revision', 'execution_profile_id',
                'repository_resource_id', 'writable_repository_resource_ids',
                'read_only_repository_resource_ids'))):
        raise ValueError('native start attribution does not match the retained original')
    if (original.thread_id != current.thread_id or original.project_id != current.project_id
            or original.message != current.message
            or (original.execution_id or 'reserved-queue-'+original.id) != current.execution_id
            or any(getattr(original, name) != getattr(current, name) for name in (
                'agent_profile_actor_id', 'agent_profile_id', 'agent_profile_revision',
                'execution_profile_id', 'repository_resource_id',
                'writable_repository_resource_ids', 'read_only_repository_resource_ids'))):
        raise ValueError('retained admission metadata does not match its original snapshot')


def reconcile_captured_start(service, attempt_id):
    """Historical fact attribution; deliberately does not validate/renew a live lease."""
    def reconcile(current):
        correlation, evidence = current.queued_start_correlation, current.queued_start_evidence
        if correlation is None or evidence is None:
            raise ValueError('no exactly correlated native start evidence')
        validate_attribution(service, current, correlation)
        if (evidence.runtime_generation != correlation.runtime_generation
                or evidence.rpc_request_id != correlation.rpc_request_id
                or evidence.provider_native_session_id != correlation.provider_native_session_id):
            raise ValueError('native start evidence attribution changed')
        receipt = QueuedNativeStartReceipt(
            admission_claim_id=correlation.admission_claim_id,
            requested_execution_id=correlation.requested_execution_id,
            execution_id=correlation.execution_id, thread_id=correlation.thread_id,
            provider_native_session_id=evidence.provider_native_session_id,
            provider_native_turn_id=evidence.provider_native_turn_id,
            provider_id=correlation.provider_id, runtime_id=correlation.runtime_id,
            observed_at=evidence.observed_at)
        if current.queued_start_receipt is not None:
            if current.queued_start_receipt != receipt:
                raise ValueError('conflicting native start receipt')
            return current
        if current.status != 'reconciliation_required':
            raise ValueError('native start disposition changed')
        return current.model_copy(update={'queued_start_receipt':receipt, 'status':'started',
            'started_at':receipt.observed_at, 'updated_at':float(service.clock()),
            'last_error':None, 'blockers':()})
    return service.store.update(attempt_id, reconcile)


@dataclass(frozen=True)
class QueuedNativeStartCapture:
    service: Any
    attempt_id: str
    claim_id: str
    requested_execution_id: str
    thread_id: str
    actor_identity_id: str | None
    # Assigned only by the validated assignment-session boundary, not task text.
    assignment: Any = None
    worker_id: str | None = None
    fence: int | None = None
    send_validator: Any = None
    admission_validator: Any = None
    require_send_guard: bool = False

    def bind_assignment(self, assignment, *, worker_id, fence, validator):
        if (assignment.assigned_worker_id != worker_id or assignment.fence != fence
                or assignment.lease is None or assignment.lease.worker_id != worker_id
                or assignment.lease.fence != fence):
            raise ValueError('native start assignment fence changed')
        return replace(self, assignment=assignment.model_copy(deep=True),
                       worker_id=worker_id, fence=fence, send_validator=validator)

    def validate_before_send(self, correlation):
        """Run inside the native write lock, without an intervening await.

        Preparation adds an asynchronous storage gap. Reuse the session's
        canonical live authority/lease guard at the actual send boundary, then
        recheck the exact admission reservation. This guard is never used for
        historical evidence reconciliation.
        """
        if self.send_validator is None:
            raise ValueError('native start send validator is unavailable')
        assignment = self.send_validator()
        if any(getattr(assignment, name) != getattr(self.assignment, name) for name in (
                'id', 'execution_id', 'organization_id', 'workspace_id', 'project_id',
                'resource_ids', 'repository_scope', 'runtime_binding', 'agent_profile',
                'execution_profile_id', 'created_by', 'assigned_worker_id', 'fence')):
            raise ValueError('native start assignment changed during preparation')
        def guard(current):
            validate_attribution(self.service, current, correlation)
            if (current.queued_start_correlation != correlation
                    or current.status != 'reconciliation_required'
                    or current.queued_start_receipt is not None
                    or current.queued_start_evidence is not None
                    or current.scheduling_retirement is not None):
                raise ValueError('native start reservation changed before send')
            return current
        self.service.store.update(self.attempt_id, guard)
        if self.admission_validator is not None:
            try:
                self.admission_validator()
            except Exception as exc:
                # This synchronous method runs before stdin.write under the
                # native writer lock. Only this local witness can release the
                # prepared correlation; an absent/failed RPC response cannot.
                def refuse(current):
                    validate_attribution(self.service, current, correlation)
                    if (current.queued_start_correlation != correlation
                            or current.status != 'reconciliation_required'
                            or current.queued_start_receipt is not None
                            or current.queued_start_evidence is not None
                            or current.started_at is not None
                            or current.scheduling_retirement is not None):
                        raise ValueError('native refusal reservation changed')
                    blocker = ExecutionPreflightBlocker(code='native_preinput_admission_refused',
                        message='Live admission refused before native write',
                        correlation=correlation.model_dump(mode='json'))
                    history = ExecutionPreflightBlockerSnapshot(blockers=(blocker,),
                        recorded_at=float(self.service.clock()), attempt_number=current.attempt_number)
                    return current.model_copy(update={'status':'queued', 'queued_start_correlation':None,
                        'blockers':(blocker,), 'blocker_history':(*current.blocker_history, history)[-self.service.MAX_HISTORY:],
                        'updated_at':float(self.service.clock())})
                self.service.store.update(self.attempt_id, refuse)
                raise NativeStartAdmissionRefused("Live admission refused before native write") from exc

    def prepare(self, *, generation, rpc_request_id, native_session_id):
        """Commit before _send; a storage error forbids this native RPC."""
        assignment = self.assignment
        if assignment is None:
            raise ValueError('native start capture requires a validated assignment')
        binding = assignment.runtime_binding
        if binding is not None and (binding.provider_id, binding.runtime_id) != ('openai', 'codex'):
            raise ValueError('native start capture runtime mismatch')
        correlation = None
        def prepare(current):
            nonlocal correlation
            profile, scope = assignment.agent_profile, assignment.repository_scope
            if (current.status != 'reconciliation_required'
                    or current.queued_start_receipt is not None
                    or current.queued_start_evidence is not None
                    or current.scheduling_retirement is not None
                    or current.notification_batch_id is not None
                    or current.execution_id != self.requested_execution_id
                    or current.queued_admission_claim_id != self.claim_id
                    or (current.replacement_thread_id or current.thread_id) != self.thread_id
                    or any(getattr(current, name) != getattr(assignment, name)
                           for name in ('organization_id', 'workspace_id', 'project_id'))
                    or (current.agent_profile_actor_id is not None
                        and current.agent_profile_actor_id != self.actor_identity_id)
                    or (current.agent_profile_id is not None and (profile is None
                        or (current.agent_profile_id, current.agent_profile_revision)
                        != (profile.profile_id, profile.profile_revision)))
                    or (current.execution_profile_id is not None
                        and current.execution_profile_id != assignment.execution_profile_id)
                    or (current.repository_resource_id is not None and (scope is None
                        or current.repository_resource_id not in scope.writable_repository_ids))
                    or (current.writable_repository_resource_ids and (scope is None
                        or set(current.writable_repository_resource_ids) != set(scope.writable_repository_ids)))
                    or (current.read_only_repository_resource_ids and (scope is None
                        or set(current.read_only_repository_resource_ids) != set(scope.read_only_repository_ids)))):
                raise ValueError('native start assignment does not match the retained admission')
            correlation = QueuedNativeStartCorrelation(
                **{name:getattr(current, name) for name in (
                    'organization_id', 'workspace_id', 'project_id', 'agent_profile_actor_id',
                    'agent_profile_id', 'agent_profile_revision', 'execution_profile_id',
                    'repository_resource_id', 'writable_repository_resource_ids',
                    'read_only_repository_resource_ids')},
                snapshot_sha256=self.service.queued_snapshot_digest(current.queued_turn_snapshot),
                admission_claim_id=self.claim_id, requested_execution_id=self.requested_execution_id,
                actor_identity_id=self.actor_identity_id or assignment.created_by,
                assignment_resource_ids=assignment.resource_ids, assignment_id=assignment.id,
                execution_id=assignment.execution_id, worker_id=self.worker_id, fence=self.fence,
                thread_id=self.thread_id, provider_native_session_id=native_session_id,
                runtime_generation=generation, rpc_request_id=rpc_request_id,
                prepared_at=float(self.service.clock()))
            validate_attribution(self.service, current, correlation)
            if current.queued_start_correlation is not None:
                # An uncertain original must never acquire another native request.
                raise ValueError('native start correlation is already reserved')
            return current.model_copy(update={'queued_start_correlation':correlation})
        self.service.store.update(self.attempt_id, prepare)
        return correlation

    def observe(self, correlation, *, generation, message):
        """Called only by the trusted RPC reader with its own generation and ID."""
        if (generation != correlation.runtime_generation
                or message.get('id') != correlation.rpc_request_id
                or 'error' in message or 'method' in message):
            return False
        result = message.get('result')
        if (not isinstance(result, dict) or 'error' in result
                or not isinstance(result.get('turn'), dict)
                or ('threadId' in result and result['threadId'] != correlation.provider_native_session_id)):
            return False
        turn = result['turn']
        turn_id = turn.get('id')
        if (not isinstance(turn_id, str) or not turn_id.strip() or turn_id != turn_id.strip()
                or len(turn_id) > 256 or turn.get('error') is not None
                or ('status' in turn and turn['status'] not in ('inProgress', 'completed', 'interrupted', 'failed'))
                or ('threadId' in turn and turn['threadId'] != correlation.provider_native_session_id)):
            return False
        if 'thread' in result:
            thread = result['thread']
            if not isinstance(thread, dict) or thread.get('id') != correlation.provider_native_session_id:
                return False
        evidence = QueuedNativeStartEvidence(runtime_generation=generation,
            rpc_request_id=correlation.rpc_request_id,
            provider_native_session_id=correlation.provider_native_session_id,
            provider_native_turn_id=turn_id, observed_at=float(self.service.clock()))
        def capture(current):
            validate_attribution(self.service, current, correlation)
            if current.queued_start_correlation != correlation:
                raise ValueError('native start correlation changed')
            # Retirement may have prepared/completed while this exact response
            # was in flight. Preserve that scheduling proof (unknown at its
            # preparation time) and add the later historical native fact. This
            # never requeues, resumes, cancels or performs a provider action.
            previous = current.queued_start_evidence
            if previous is not None:
                if previous.model_dump(exclude={'observed_at'}) != evidence.model_dump(exclude={'observed_at'}):
                    raise ValueError('conflicting native start evidence')
                return current
            if current.status != 'reconciliation_required' or current.queued_start_receipt is not None:
                raise ValueError('native start disposition changed')
            return current.model_copy(update={'queued_start_evidence':evidence})
        # The first commit survives a crash or failure in receipt projection.
        self.service.store.update(self.attempt_id, capture)
        reconcile_captured_start(self.service, self.attempt_id)
        return True
