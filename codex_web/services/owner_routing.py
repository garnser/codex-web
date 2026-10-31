"""Resolve alias recipients through existing bindings, bootstraps and profiles.

No binding/profile is created here. The queued request still crosses canonical
identity, RoleAuthority, worker lease and native-start admission boundaries.
"""
from __future__ import annotations

import time
import hashlib
import json

from fastapi import HTTPException

from codex_web.owner_routing import OwnerRoutingError, OwnerRoutingPin, OwnerRoutingBlocker, OwnerRoutingBinding, OwnerRoutingRecipient, normalized_owner
from codex_web.identity import TenantScope
from codex_web.services.work_item_dependencies import actionable_owner_value, work_item_repository_scope


class _MissingNativePredecessor(OwnerRoutingError):
    """Positive scoped native session, but its historical assignment is absent."""

    def __init__(self):
        super().__init__('owner_recipient_native_custody_unverified')


class _CancelledRecipientContextRequired(OwnerRoutingError):
    """Fresh pending intent has a verified cancelled source, never a resume."""

    def __init__(self):
        super().__init__('owner_recipient_cancelled_context_required')


class OwnerRoutingService:
    def __init__(self, *, routing, get_state, projects, load_bindings, binding_prefix,
                 bootstraps, assignments, profiles, control_actor, actor_resolver,
                 replacement_resolver, update_state=None, append_event=None, preflight=None, workers=None, execution_profiles=None, sessions=None, inspection_ready=None, inspection_actor=None):
        self.routing = routing
        self.get_state = get_state
        self.projects = projects
        self.load_bindings = load_bindings
        self.binding_prefix = binding_prefix
        self.bootstraps = bootstraps
        self.assignments = assignments
        self.profiles = profiles
        self.control_actor = control_actor
        self.actor_resolver = actor_resolver
        self.replacement_resolver = replacement_resolver
        self.update_state, self.append_event = update_state, append_event
        self.preflight = preflight
        self.delivery_recorder = None
        self.workers = workers
        self.execution_profiles = execution_profiles
        self.sessions = sessions
        self.recipient_preparer = None
        self.inspection_ready = inspection_ready
        self.inspection_actor = inspection_actor

    def require_readonly_inspection_ready(self):
        if (self.inspection_ready is None or self.inspection_ready() is not True
                or not callable(self.inspection_actor)):
            raise OwnerRoutingError('owner_routing_inspection_not_initialized')

    def project_native_start(self, attempt):
        """Attribute an exact late positive receipt without renewing authority.

        This projects historical delivery only. It cannot ACK, start a turn,
        resume a worker, replace an intent or reinterpret the original alias.
        """
        from codex_web.models import QueuedTurn
        from codex_web.services.queued_native_start import validate_attribution
        from codex_web.services.execution_preflight import ExecutionPreflightService
        receipt, correlation, evidence = (attempt.queued_start_receipt,
            attempt.queued_start_correlation, attempt.queued_start_evidence)
        if (self.delivery_recorder is None or attempt.status != 'started'
                or receipt is None or correlation is None or evidence is None
                or attempt.queued_turn_snapshot is None):
            return False
        original = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        pin = original.owner_routing_pin
        if pin is None or pin.handoff_requested_at is None:
            return False
        # Reuse the canonical original/correlation checks. The observer runs
        # after the native receipt commit; a changed live lease is irrelevant
        # to attribution of a proven historical start.
        validate_attribution(ExecutionPreflightService, attempt, correlation)
        if (receipt.admission_claim_id != correlation.admission_claim_id
                or receipt.execution_id != correlation.execution_id
                or receipt.requested_execution_id != correlation.requested_execution_id
                or receipt.thread_id != correlation.thread_id
                or receipt.provider_id != correlation.provider_id
                or receipt.runtime_id != correlation.runtime_id
                or receipt.observed_at != evidence.observed_at
                or receipt.provider_native_turn_id != evidence.provider_native_turn_id
                or receipt.provider_native_session_id != evidence.provider_native_session_id
                or evidence.runtime_generation != correlation.runtime_generation
                or evidence.rpc_request_id != correlation.rpc_request_id
                or evidence.provider_native_session_id != correlation.provider_native_session_id
                or original.work_item_ref != pin.work_item_ref
                or original.handoff_requested_at != pin.handoff_requested_at
                or (attempt.organization_id, attempt.workspace_id, attempt.project_id)
                != (pin.binding.organization_id, pin.binding.workspace_id, pin.binding.project_id)
                or correlation.actor_identity_id != original.agent_profile_actor_id):
            raise OwnerRoutingError('owner_routing_native_fact_mismatch')
        profile = self.profiles.store.revision(original.agent_profile_id, original.agent_profile_revision,
            organization_id=attempt.organization_id, workspace_id=attempt.workspace_id)
        if profile is None or profile.role_id != pin.binding.canonical_owner:
            raise OwnerRoutingError('owner_routing_native_profile_unverified')
        return self.delivery_recorder(pin.work_item_ref,
            expected_requested_at=pin.handoff_requested_at, recipient=profile.role_id,
            organization_id=attempt.organization_id, workspace_id=attempt.workspace_id,
            project_id=attempt.project_id, thread_id=receipt.thread_id,
            turn_id=receipt.provider_native_turn_id, execution_id=receipt.execution_id,
            started_at=receipt.observed_at, execution_profile_id=correlation.execution_profile_id,
            repository_resource_id=correlation.repository_resource_id,
            writable_repository_resource_ids=correlation.writable_repository_resource_ids,
            read_only_repository_resource_ids=correlation.read_only_repository_resource_ids,
            owner_routing_pin=pin)

    def require_resolved_originals(self, state, *, excluding_attempt_id=None, pin=None):
        if self.preflight is None:
            raise OwnerRoutingError('owner_routing_retained_index_unavailable')
        if excluding_attempt_id is not None:
            current = self.preflight.get(excluding_attempt_id)
            if (current is None or pin is None
                    or (current.queued_turn_snapshot or {}).get('owner_routing_pin') != pin.model_dump(mode='json')
                    or (current.organization_id, current.workspace_id, current.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)):
                raise OwnerRoutingError('owner_routing_retained_intent_mismatch')
        try:
            unresolved = self.preflight.unresolved_work_item(organization_id=state.organization_id,
                workspace_id=state.workspace_id, project_id=state.project_id, ref=state.ref,
                excluding_attempt_id=excluding_attempt_id)
        except (LookupError, ValueError, RuntimeError) as exc:
            known = {'owner_routing_retained_index_unavailable', 'owner_routing_retained_lookup_truncated'}
            raise OwnerRoutingError(str(exc) if str(exc) in known else 'owner_routing_retained_lookup_unavailable') from exc
        if unresolved is not None:
            raise OwnerRoutingError('owner_routing_native_outcome_unresolved')

    def record_failure(self, state, error):
        if self.update_state is None:
            return
        definition = None
        try:
            routing = self.snapshot(state)
            definition = routing.definition if routing is not None else None
        except OwnerRoutingError:
            pass
        owner = normalized_owner(state.handoff.to_agent if state.handoff and state.handoff.status == 'pending'
            else actionable_owner_value(state))
        blocker = OwnerRoutingBlocker(code=str(error), configured_owner=owner,
            definition=definition, recorded_at=time.time())
        changed = False
        def update(current):
            nonlocal changed
            changed = False
            if (current is None or (current.organization_id, current.workspace_id, current.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)
                    or current.updated_at != state.updated_at):
                return current
            previous = current.owner_routing_blocker
            if previous is not None and previous.model_dump(exclude={'recorded_at'}) == blocker.model_dump(exclude={'recorded_at'}):
                return current
            current.owner_routing_blocker = blocker
            changed = True
            return current
        self.update_state(state.ref, update)
        if changed and self.append_event is not None:
            from codex_web.models import WorkItemEvent
            self.append_event(WorkItemEvent(ref=state.ref, event_type='owner_routing_blocked',
                created_at=blocker.recorded_at, source='canonical-owner-routing',
                payload={'code': blocker.code, 'configured_owner': owner,
                    'definition_record_id': definition.record_id if definition is not None else None}))

    def snapshot(self, state):
        return self.routing(state)

    def clear_failure(self, pin):
        if self.update_state is None:
            return
        def update(current):
            if current is None or current.owner_routing_blocker is None:
                return current
            try:
                self.validate_intent(pin, current)
            except OwnerRoutingError:
                return current
            current.owner_routing_blocker = None
            return current
        self.update_state(pin.work_item_ref, update)

    def existing_binding(self, state, owner, *, preferred_conversation_id=None):
        # Alias routing never uses fuzzy thread names or clones a binding into
        # another conversation. Only an existing exact canonical role qualifies.
        candidates = [item for item in self.load_bindings()
            if item.project_id == state.project_id and not item.is_master
            and normalized_owner(self.binding_prefix(item)) == owner and item.thread_id]
        preferred = [item for item in candidates
            if item.external_conversation_id == preferred_conversation_id]
        candidates = preferred or candidates
        threads = {item.thread_id for item in candidates}
        if not candidates:
            raise OwnerRoutingError('work_item_owner_binding_unavailable')
        if len(threads) != 1:
            raise OwnerRoutingError('work_item_owner_binding_ambiguous')
        # Multiple provider reply bindings for the same thread are equivalent
        # executors; retain the selected reply binding in the original request.
        return sorted(candidates, key=lambda item: item.id)[0]

    def executor(self, state, binding, owner, *, recipient_thread_id=None, lookup_actor=None):
        try:
            project = self.projects.get(state.project_id, scope=TenantScope(
                organization_id=state.organization_id, workspace_id=state.workspace_id))
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_routing_scope_mismatch') from exc
        if ((project.organization_id, project.workspace_id, project.id)
                != (state.organization_id, state.workspace_id, state.project_id)
                or binding.project_id != project.id
                or normalized_owner(self.binding_prefix(binding)) != owner):
            raise OwnerRoutingError('owner_routing_scope_mismatch')
        lookup_actor = (lookup_actor or self.control_actor)()
        if (lookup_actor.organization_id, lookup_actor.workspace_id) != (
                state.organization_id, state.workspace_id):
            raise OwnerRoutingError('owner_executor_actor_scope_mismatch')
        try:
            bootstrap = self.bootstraps.get_by_thread(recipient_thread_id or binding.thread_id, lookup_actor)
            assignment = self.assignments(bootstrap.assignment_id)
            if (assignment is None or assignment.agent_profile is None
                    or (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)):
                raise OwnerRoutingError('owner_executor_bootstrap_unverified')
            original = assignment.agent_profile
            actor = self.actor_resolver(assignment.created_by, project)
            if (actor.identity_id != assignment.created_by
                    or (actor.organization_id, actor.workspace_id)
                    != (state.organization_id, state.workspace_id)):
                raise OwnerRoutingError('owner_executor_actor_scope_mismatch')
            profile, _ = self.profiles.resolve_for_execution(original.profile_id, actor=actor,
                project_id=project.id)
            if (profile.revision != original.profile_revision or profile.record_id != original.profile_record_id
                    or profile.role_id != owner or original.role_id != owner
                    or (profile.organization_id, profile.workspace_id)
                    != (state.organization_id, state.workspace_id)):
                raise OwnerRoutingError('owner_executor_profile_changed')
            return profile, actor
        except OwnerRoutingError:
            raise
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_executor_profile_unavailable') from exc

    @staticmethod
    def _authority_checksum(assignment):
        # Lease renewal and append-only procedure evidence are not custody changes.
        fields = ('organization_id', 'workspace_id', 'project_id', 'execution_id',
            'created_by', 'agent_profile', 'execution_profile_id',
            'execution_profile_definition', 'execution_workspace_id', 'runtime_binding',
            'repository_target', 'repository_scope', 'resource_ids', 'resource_context',
            'sandbox', 'approval_policy', 'network', 'secret_refs', 'skill_refs')
        raw = assignment.model_dump(mode='json', include=set(fields))
        return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _work_item_owner_checksum(state):
        fields = ('current_owner', 'next_owner', 'implementation_owner', 'validation_owner', 'release_owner')
        raw = {name: getattr(state, name, None) for name in fields}
        raw['handoff_from'] = state.handoff.from_agent if state.handoff is not None else None
        return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def native_scope_compatible(self, state, bootstrap, assignment):
        """Current bootstrap scope cannot replace the original native custody."""
        if self.sessions is None:
            return True  # Optional for existing non-native compositions.
        try:
            session = self.sessions.native_custody(bootstrap.thread_id)
        except (LookupError, ValueError, RuntimeError) as exc:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified') from exc
        if session is None:
            # No provider session exists yet: ordinary first admission remains
            # subject to the normal canonical bootstrap/native guards.
            return True
        expected = (state.organization_id, state.workspace_id, state.project_id)
        if (session.organization_id, session.workspace_id, session.project_id) != expected:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        previous = self.assignments(session.assignment_id) if session.assignment_id else None
        if previous is None:
            if session.assignment_id:
                raise _MissingNativePredecessor()
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        if ((previous.organization_id, previous.workspace_id, previous.project_id, previous.created_by)
                != (*expected, assignment.created_by)):
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        if (previous.id != assignment.id and str(previous.status) not in
                {'succeeded', 'failed', 'cancelled', 'lost'}):
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        return (set(previous.resource_ids) == set(assignment.resource_ids)
                and previous.repository_scope == assignment.repository_scope)

    def _verified_missing_predecessor_replacement(self, state, binding, owner, source, requested, *, lookup_actor=None):
        """Separate current custody never repairs or resumes missing old custody."""
        if str(source.status) not in {'succeeded', 'failed', 'cancelled', 'lost'} or source.lease is not None:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        try:
            target = self.replacement_resolver(binding.thread_id, project_id=state.project_id,
                organization_id=state.organization_id, workspace_id=state.workspace_id)
            if not target or target == binding.thread_id:
                raise OwnerRoutingError('owner_recipient_native_custody_unverified')
            bootstrap = self.bootstraps.get_by_thread(target, (lookup_actor or self.control_actor)())
            witness, _, _ = self._live_recipient(state, binding, bootstrap, owner, lookup_actor=lookup_actor)
            current = self.assignments(witness.assignment_id)
            session = self.sessions.native_custody(target)
            if (current is None or current.created_by != source.created_by
                    or current.agent_profile != source.agent_profile
                    or current.repository_scope is None
                    or set(current.repository_scope.writable_repository_ids) != set(requested)
                    or session is None or session.assignment_id != current.id
                    or session.execution_id != current.execution_id
                    or session.execution_workspace_id != current.execution_workspace_id
                    or session.worker_id != current.assigned_worker_id):
                raise OwnerRoutingError('owner_recipient_native_custody_unverified')
            return target
        except OwnerRoutingError:
            raise
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified') from exc

    def cancelled_recipient_source(self, state, binding, owner, requested, *, lookup_actor=None):
        """Positively identify source custody for ONE new pending-handoff context.

        This never makes a cancelled assignment LIVE or resolves missing old
        custody. A missing historical predecessor requires its existing exact
        canonical replacement, whose own native/assignment association is known.
        """
        from codex_web.services.work_item_timing import WorkItemTimingPolicy
        handoff = state.handoff
        deadline = WorkItemTimingPolicy.handoff_deadline_projection(state)
        routing = self.snapshot(state)
        if (handoff is None or handoff.status != 'pending' or handoff.acknowledged_at is not None
                or handoff.delivery is None or handoff.delivery.status != 'awaiting_dispatch'
                or routing is None or routing.owner(normalized_owner(handoff.to_agent)) != owner
                or deadline is None or deadline['deadline_at'] is None
                or not handoff.requested_at <= time.time() < deadline['deadline_at']):
            raise OwnerRoutingError('owner_recipient_preparation_changed')
        return self._cancelled_recipient_custody(state, binding, owner, requested,
            lookup_actor=lookup_actor)

    def _cancelled_recipient_custody(self, state, binding, owner, requested, *, lookup_actor=None):
        """Read exact cancelled source metadata; this alone grants no preparation."""
        from codex_web.agent_runtime import AgentSessionStatus
        actor = (lookup_actor or self.control_actor)()
        try:
            original = self.bootstraps.get_by_thread(binding.thread_id, actor)
            old = self.assignments(original.assignment_id)
            previous = self.sessions.native_custody(binding.thread_id) if self.sessions is not None else None
            expected = (state.organization_id, state.workspace_id, state.project_id)
            if (old is None or previous is None or old.agent_profile is None
                    or (old.organization_id, old.workspace_id, old.project_id) != expected
                    or (previous.organization_id, previous.workspace_id, previous.project_id) != expected
                    or (original.organization_id, original.workspace_id, original.created_by)
                        != (state.organization_id, state.workspace_id, old.created_by)
                    or original.execution_id != old.execution_id
                    or original.execution_workspace_id != old.execution_workspace_id
                    or str(old.status) != 'cancelled' or old.lease is not None):
                raise OwnerRoutingError('owner_recipient_native_custody_unverified')
            target = self.replacement_resolver(binding.thread_id, project_id=state.project_id,
                organization_id=state.organization_id, workspace_id=state.workspace_id) or binding.thread_id
            bootstrap = self.bootstraps.get_by_thread(target, actor)
            assignment = self.assignments(bootstrap.assignment_id)
            session = self.sessions.native_custody(target)
            if (assignment is None or session is None or assignment.agent_profile != old.agent_profile
                    or (assignment.organization_id, assignment.workspace_id, assignment.project_id,
                        assignment.created_by) != (*expected, old.created_by)
                    or (bootstrap.organization_id, bootstrap.workspace_id, bootstrap.created_by)
                        != (state.organization_id, state.workspace_id, old.created_by)
                    or str(assignment.status) != 'cancelled' or assignment.lease is not None
                    or type(assignment.fence) is not int or assignment.fence < 1
                    or assignment.execution_id != bootstrap.execution_id
                    or assignment.execution_workspace_id != bootstrap.execution_workspace_id
                    or (session.organization_id, session.workspace_id, session.project_id) != expected
                    or session.status != AgentSessionStatus.READY
                    or session.failure is not None
                    or session.assignment_id != assignment.id or session.execution_id != assignment.execution_id
                    or session.execution_workspace_id != assignment.execution_workspace_id
                    or not assignment.assigned_worker_id or session.worker_id != assignment.assigned_worker_id
                    or (session.provider_id, session.runtime_id) != (previous.provider_id, previous.runtime_id)
                    or set(session.resource_ids) != set(assignment.resource_ids)
                    or assignment.repository_scope is None
                    or set(assignment.repository_scope.writable_repository_ids) != set(requested)
                    or assignment.repository_scope.read_only_repository_ids
                    or set(assignment.resource_ids) != set(requested)
                    or assignment.sandbox != binding.sandbox or assignment.approval_policy != binding.approval_policy):
                raise OwnerRoutingError('owner_recipient_native_custody_unverified')
            # Retain the ordinary original-native checks. Only the existing
            # typed missing-predecessor case may use separate replacement custody.
            try:
                self.native_scope_compatible(state, original, old)
            except _MissingNativePredecessor:
                if target == binding.thread_id:
                    raise
            self.executor(state, binding, owner, recipient_thread_id=target, lookup_actor=lookup_actor)
            witness = {'original_thread_id': binding.thread_id, 'source_thread_id': target,
                'original_bootstrap_id': original.id, 'original_assignment': old.model_dump(mode='json'),
                'original_native': previous.model_dump(mode='json'),
                'source_bootstrap_id': bootstrap.id, 'source_native': session.model_dump(mode='json')}
            return bootstrap, assignment, session, witness
        except OwnerRoutingError:
            raise
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified') from exc

    def _prepared_cancelled_recipient(self, state, binding, configured_owner, requested, *, lookup_actor=None, originals_checked=False):
        # The immutable existing bootstrap key is the provider-attempt boundary.
        # Do not substitute an arbitrary same-scope LIVE lane for this intent.
        if self.recipient_preparer is None:
            raise _CancelledRecipientContextRequired()
        bound = self.recipient_preparer.prepared_binding(state, binding, configured_owner,
            tuple(requested), lookup_actor=lookup_actor, originals_checked=originals_checked)
        if bound is None:
            raise _CancelledRecipientContextRequired()
        return bound.thread_id

    def _live_recipient(self, state, binding, bootstrap, owner, *, lookup_actor=None):
        from codex_web.execution_workers import AssignmentStatus, WorkerLifecycle
        assignment = self.assignments(bootstrap.assignment_id)
        now = time.time()
        if (assignment is None or assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
                or assignment.lease is None or assignment.lease.expires_at <= now
                or assignment.lease.fence != assignment.fence
                or assignment.lease.worker_id != assignment.assigned_worker_id
                or (assignment.deadline_at is not None and assignment.deadline_at <= now)
                or assignment.execution_id != bootstrap.execution_id
                or assignment.execution_workspace_id != bootstrap.execution_workspace_id
                or (bootstrap.organization_id, bootstrap.workspace_id, bootstrap.created_by)
                   != (state.organization_id, state.workspace_id, assignment.created_by)):
            raise OwnerRoutingError('owner_recipient_custody_unavailable')
        if assignment.sandbox != binding.sandbox or assignment.approval_policy != binding.approval_policy:
            raise OwnerRoutingError('owner_recipient_policy_incompatible')
        worker = self.workers(assignment.assigned_worker_id) if self.workers is not None else None
        if (worker is None or worker.lifecycle != WorkerLifecycle.ACTIVE or worker.revoked_at is not None
                or (worker.organization_id, worker.workspace_id)
                   != (state.organization_id, state.workspace_id)):
            raise OwnerRoutingError('owner_recipient_worker_unavailable')
        if not self.native_scope_compatible(state, bootstrap, assignment):
            raise OwnerRoutingError('owner_recipient_native_scope_incompatible')
        profile, actor = self.executor(state, binding, owner, recipient_thread_id=bootstrap.thread_id, lookup_actor=lookup_actor)
        return OwnerRoutingRecipient(thread_id=bootstrap.thread_id, bootstrap_id=bootstrap.id,
            assignment_id=assignment.id, execution_id=assignment.execution_id,
            worker_id=assignment.assigned_worker_id, fence=assignment.fence,
            authority_checksum=self._authority_checksum(assignment),
            work_item_owner_checksum=self._work_item_owner_checksum(state)), profile, actor

    def _accepted_owner_recipient(self, state, binding, owner, requested, *, lookup_actor=None, originals_checked=False):
        """Select an already delivered recipient, never prepare or replay input.

        Only the current accepted handoff is lineage. Its retained exact START
        and deterministic reservation locate a candidate; all current authority
        and custody judgments still run before returning a new owner witness.
        Callers retain their normal unresolved-original check.
        """
        from codex_web.models import QueuedTurn, QueuedNativeStartReceipt
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.services.queued_native_start import validate_attribution
        handoff = state.handoff
        routing = self.snapshot(state)
        if (handoff is None or handoff.status != 'accepted'
                or normalized_owner(actionable_owner_value(state)) != owner
                or routing is None
                or routing.owner(normalized_owner(state.current_owner)) != owner
                or routing.owner(normalized_owner(handoff.to_agent)) != owner):
            return None
        delivery = handoff.delivery
        if delivery is None or self.recipient_preparer is None:
            return None
        token = self.recipient_preparer.logical_id(state, binding,
            normalized_owner(handoff.to_agent), tuple(requested))
        physical = delivery.execution_id.removeprefix('thread-bootstrap-') if delivery.execution_id else ''
        previous = None
        if physical != token:
            previous = self.recipient_preparer.existing_assignment(execution_id='thread-bootstrap-' + token)
            if previous is None:
                # Ordinary non-reservation handoffs retain their existing path.
                return None
            expected = self.recipient_preparer.digest({'logical_id': token,
                'predecessor_id': previous.id, 'predecessor_fence': previous.fence})[:32]
            if physical != expected:
                return None
        if (handoff.acknowledged_at is None
                or delivery.status != 'delivered' or delivery.scope_verified is not True
                or not delivery.thread_id or not delivery.turn_id
                or not delivery.execution_id or delivery.started_at is None
                or set(delivery.writable_repository_resource_ids) != set(requested)
                or delivery.read_only_repository_resource_ids
                or set(work_item_repository_scope(state)[1]) != set(requested)):
            raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
        if not originals_checked:
            self.require_resolved_originals(state)
        try:
            attempts = self.preflight.for_work_item(organization_id=state.organization_id,
                workspace_id=state.workspace_id, project_id=state.project_id, ref=state.ref)
            matches = []
            for attempt in attempts:
                raw = attempt.queued_turn_snapshot
                if (raw is None or raw.get('handoff_requested_at') != handoff.requested_at
                        or attempt.status != 'started'):
                    continue
                original = QueuedTurn.model_validate(raw)
                old_pin = original.owner_routing_pin
                receipt, correlation, evidence = (attempt.queued_start_receipt,
                    attempt.queued_start_correlation, attempt.queued_start_evidence)
                if (old_pin is None or old_pin.recipient is None
                        or old_pin.handoff_requested_at != handoff.requested_at
                        or old_pin.work_item_ref != state.ref
                        or old_pin.bot_binding_id != binding.id
                        or old_pin.original_thread_id != binding.thread_id
                        or old_pin.binding.canonical_owner != owner
                        or old_pin.binding.configured_owner != normalized_owner(handoff.to_agent)
                        or (old_pin.binding.organization_id, old_pin.binding.workspace_id, old_pin.binding.project_id)
                            != (state.organization_id, state.workspace_id, state.project_id)
                        or receipt is None or correlation is None or evidence is None
                        or receipt.thread_id != delivery.thread_id
                        or receipt.execution_id != delivery.execution_id
                        or receipt.provider_native_turn_id != delivery.turn_id
                        or receipt.observed_at != delivery.started_at):
                    continue
                validate_attribution(ExecutionPreflightService, attempt, correlation)
                expected = QueuedNativeStartReceipt(
                    admission_claim_id=correlation.admission_claim_id,
                    requested_execution_id=correlation.requested_execution_id,
                    execution_id=correlation.execution_id, thread_id=correlation.thread_id,
                    provider_native_session_id=evidence.provider_native_session_id,
                    provider_native_turn_id=evidence.provider_native_turn_id,
                    provider_id=correlation.provider_id, runtime_id=correlation.runtime_id,
                    observed_at=evidence.observed_at)
                if (receipt != expected or evidence.runtime_generation != correlation.runtime_generation
                        or evidence.rpc_request_id != correlation.rpc_request_id
                        or evidence.provider_native_session_id != correlation.provider_native_session_id
                        or correlation.actor_identity_id != original.agent_profile_actor_id
                        or old_pin.binding.definition != routing.definition
                        or (attempt.organization_id, attempt.workspace_id, attempt.project_id)
                            != (state.organization_id, state.workspace_id, state.project_id)
                        or original.work_item_ref != state.ref
                        or original.thread_id != delivery.thread_id
                        or correlation.execution_profile_id != delivery.execution_profile_id
                        or correlation.repository_resource_id != delivery.repository_resource_id
                        or tuple(correlation.writable_repository_resource_ids)
                            != tuple(delivery.writable_repository_resource_ids)
                        or tuple(correlation.read_only_repository_resource_ids)
                            != tuple(delivery.read_only_repository_resource_ids)):
                    raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
                matches.append((old_pin, original, correlation))
            if len(matches) != 1:
                raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
            old_pin, original, correlation = matches[0]
            token = self.recipient_preparer.logical_id(state, binding,
                old_pin.binding.configured_owner, tuple(requested))
            actor = (lookup_actor or self.control_actor)()
            historical = old_pin.recipient
            admitted = self.assignments(historical.assignment_id)
            source_bootstrap, source_assignment, source_native, cancelled = self._cancelled_recipient_custody(
                state, binding, owner, requested, lookup_actor=lookup_actor)
            source_profile, source_actor = self.executor(state, binding, owner,
                recipient_thread_id=source_bootstrap.thread_id, lookup_actor=lookup_actor)
            if previous is not None:
                proof = self.recipient_preparer._context_proof(state, binding,
                    old_pin.binding.configured_owner, tuple(requested), source_bootstrap,
                    source_assignment, source_native, cancelled, source_profile, source_actor, routing)
                # The positive retained original pins the pre-ACK business
                # identity; ACK changes only ownership/delivery bookkeeping.
                proof['owner_checksum'] = historical.work_item_owner_checksum
                proof['stage'] = old_pin.stage
                proof['retry_attempt'] = old_pin.retry_attempt
                if not self.recipient_preparer.recoverable(previous, proof, source_profile, token):
                    raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
            # Historical START attribution survives canonical rebind, which
            # intentionally removes the superseded bootstrap mapping. The
            # immutable original and its retained assignment remain the anchor.
            if (admitted is None or admitted.subject is None
                    or admitted.subject.kind != 'thread_bootstrap'
                    or admitted.subject.ref != 'bootstrap-' + physical
                    or admitted.execution_id != 'thread-bootstrap-' + physical
                    or admitted.execution_id != delivery.execution_id
                    or admitted.agent_profile is None
                    or admitted.agent_profile != source_assignment.agent_profile
                    or admitted.created_by != source_assignment.created_by
                    or (admitted.organization_id, admitted.workspace_id, admitted.project_id)
                        != (state.organization_id, state.workspace_id, state.project_id)
                    or admitted.agent_profile.profile_id != original.agent_profile_id
                    or admitted.agent_profile.profile_revision != original.agent_profile_revision
                    or admitted.execution_profile_id != delivery.execution_profile_id
                    or admitted.repository_scope is None
                    or set(admitted.repository_scope.writable_repository_ids) != set(requested)
                    or admitted.repository_scope.read_only_repository_ids
                    or set(admitted.resource_ids) != set(requested)
                    or self._authority_checksum(admitted) != historical.authority_checksum
                    or correlation.assignment_id != historical.assignment_id
                    or correlation.execution_id != historical.execution_id
                    or correlation.worker_id != historical.worker_id
                    or correlation.fence != historical.fence):
                raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
            from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
            try:
                anchor = self.bootstraps.get_by_bootstrap('bootstrap-' + physical, actor)
            except ThreadBootstrapBindingNotFoundError:
                from codex_web.thread_bootstrap import deterministic_thread_bootstrap_binding_id
                if historical.bootstrap_id != deterministic_thread_bootstrap_binding_id(
                        state.organization_id, state.workspace_id, 'bootstrap-' + physical):
                    raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
            else:
                if (anchor.id != historical.bootstrap_id or anchor.thread_id != delivery.thread_id
                        or anchor.assignment_id != admitted.id or anchor.execution_id != admitted.execution_id
                        or anchor.execution_workspace_id != admitted.execution_workspace_id):
                    raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified')
            target = delivery.thread_id
            try:
                bootstrap = self.bootstraps.get_by_thread(target, actor)
                assignment = self.assignments(bootstrap.assignment_id)
            except LookupError:
                bootstrap, assignment = None, None
            # A current canonical same-thread rebind is the normal recovery
            # owner. Only a missing/retired mapping consults the scoped durable
            # replacement graph, never an arbitrary matching live candidate.
            if (assignment is None or (str(assignment.status) in {'succeeded', 'failed', 'cancelled'}
                    and assignment.lease is None)):
                replacement = self.replacement_resolver(delivery.thread_id, project_id=state.project_id,
                    organization_id=state.organization_id, workspace_id=state.workspace_id)
                if replacement:
                    target = replacement
                    bootstrap = self.bootstraps.get_by_thread(target, actor)
                    assignment = self.assignments(bootstrap.assignment_id)
            if bootstrap is None or assignment is None:
                raise OwnerRoutingError('owner_recipient_custody_unavailable')
            changed = bootstrap.assignment_id != admitted.id or target != delivery.thread_id
            if changed and (str(admitted.status) not in {'succeeded', 'failed', 'cancelled'}
                    or admitted.lease is not None):
                raise OwnerRoutingError('owner_recipient_custody_unavailable')
            if (assignment is None or assignment.subject is None
                    or assignment.subject.kind != 'thread_bootstrap'
                    or assignment.subject.ref != bootstrap.bootstrap_id
                    or assignment.agent_profile != admitted.agent_profile
                    or assignment.created_by != admitted.created_by
                    or assignment.execution_profile_id != admitted.execution_profile_id
                    or assignment.execution_profile_definition != admitted.execution_profile_definition
                    or assignment.runtime_binding != admitted.runtime_binding
                    or assignment.resource_context != admitted.resource_context
                    or assignment.repository_target != admitted.repository_target
                    or assignment.skill_refs != admitted.skill_refs
                    or assignment.repository_scope != admitted.repository_scope
                    or set(assignment.resource_ids) != set(admitted.resource_ids)
                    or (assignment.sandbox, assignment.approval_policy, assignment.network)
                        != (admitted.sandbox, admitted.approval_policy, admitted.network)):
                raise OwnerRoutingError('owner_recipient_custody_changed')
            witness, profile, resolved_actor = self._live_recipient(
                state, binding, bootstrap, owner, lookup_actor=lookup_actor)
            if changed:
                self._accepted_recovery_credentials(admitted, assignment, historical, delivery.started_at)
                self._accepted_recovery_native_custody(state, delivery.thread_id,
                    target, admitted, assignment)
            elif witness.model_dump(exclude={'work_item_owner_checksum'}) != historical.model_dump(
                    exclude={'work_item_owner_checksum'}):
                raise OwnerRoutingError('owner_recipient_custody_changed')
            if (profile.profile_id != original.agent_profile_id
                    or profile.revision != original.agent_profile_revision
                    or resolved_actor.identity_id != original.agent_profile_actor_id):
                raise OwnerRoutingError('owner_recipient_custody_changed')
            return witness, profile, resolved_actor
        except OwnerRoutingError:
            raise
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_recipient_accepted_lineage_unverified') from exc

    def _accepted_recovery_credentials(self, admitted, current, historical, started_at):
        """Distinguish canonical initial sealing from an arbitrary reference change.

        This checks reference provenance only. Native admission still resolves
        selected Skills, provider bindings and both principals' current SecretUse
        before its existing CAS can seal a pending replacement assignment.
        """
        old, new = admitted.native_credential_custody, current.native_credential_custody
        if old is None:
            if admitted.secret_refs != current.secret_refs:
                raise OwnerRoutingError('owner_recipient_custody_changed')
            if new is None:
                return
            if new.admitted_at is None:
                if (new.requester_identity_id is not None or new.worker_identity_id is not None
                        or new.fence is not None or new.procedures or new.provider_bindings
                        or new.procedure_secret_refs
                        or current.secret_refs != ((new.runtime_secret_ref,) if new.runtime_secret_ref else ())):
                    raise OwnerRoutingError('owner_recipient_custody_changed')
                return
            worker = self.workers(current.assigned_worker_id) if self.workers is not None else None
            allowed = set(new.procedure_secret_refs)
            if new.runtime_secret_ref:
                allowed.add(new.runtime_secret_ref)
            if (worker is None or new.fence != current.fence
                    or new.requester_identity_id != current.created_by
                    or new.worker_identity_id != worker.service_identity_id
                    or set(current.secret_refs) != allowed
                    or not set(new.procedures).issubset(current.skill_refs)):
                raise OwnerRoutingError('owner_recipient_custody_changed')
            return
        if new is None:
            raise OwnerRoutingError('owner_recipient_custody_changed')
        old_allowed = set(old.procedure_secret_refs)
        if old.runtime_secret_ref:
            old_allowed.add(old.runtime_secret_ref)
        old_worker = self.workers(historical.worker_id) if self.workers is not None else None
        if (old_worker is None or admitted.assigned_worker_id != historical.worker_id
                or (old_worker.organization_id, old_worker.workspace_id)
                    != (admitted.organization_id, admitted.workspace_id)
                or old.worker_identity_id != old_worker.service_identity_id
                or old.admitted_at is None or old.admitted_at > started_at
                or old.fence != historical.fence
                or old.requester_identity_id != admitted.created_by
                or set(admitted.secret_refs) != old_allowed
                or new.runtime_secret_ref != old.runtime_secret_ref):
            raise OwnerRoutingError('owner_recipient_custody_changed')
        if new.admitted_at is None:
            # Exact pristine pending marker emitted by the bootstrap builder.
            if (new.requester_identity_id is not None or new.worker_identity_id is not None
                    or new.fence is not None or new.procedures or new.provider_bindings
                    or new.procedure_secret_refs
                    or current.secret_refs != ((new.runtime_secret_ref,) if new.runtime_secret_ref else ())):
                raise OwnerRoutingError('owner_recipient_custody_changed')
            return
        worker = self.workers(current.assigned_worker_id) if self.workers is not None else None
        if (worker is None or new.fence != current.fence
                or new.requester_identity_id != current.created_by
                or new.worker_identity_id != worker.service_identity_id
                or new.procedures != old.procedures
                or new.provider_bindings != old.provider_bindings
                or new.procedure_secret_refs != old.procedure_secret_refs
                or set(current.secret_refs) != old_allowed):
            raise OwnerRoutingError('owner_recipient_custody_changed')

    def _accepted_recovery_native_custody(self, state, delivered_thread, target, admitted, current):
        """Fresh canonical native metadata only, never infer a recovery edge."""
        from codex_web.agent_runtime import AgentSessionStatus
        if self.sessions is None:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        session = self.sessions.native_custody(target)
        if session is None:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        source = current if session.assignment_id == current.id else admitted
        if (session.assignment_id != source.id
                or (target != delivered_thread and source.id != current.id)
                or (session.organization_id, session.workspace_id, session.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)
                or session.status != AgentSessionStatus.READY or session.failure is not None
                or session.execution_id != source.execution_id
                or session.execution_workspace_id != source.execution_workspace_id
                or session.worker_id != source.assigned_worker_id
                or (source.runtime_binding is not None
                    and (session.provider_id, session.runtime_id)
                        != (source.runtime_binding.provider_id, source.runtime_binding.runtime_id))
                or set(session.resource_ids) != set(current.resource_ids)):
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')

    async def prepare_compatible_pin(self, state, binding, configured_owner, *, writable_repository_ids):
        import asyncio
        try:
            return await asyncio.to_thread(self.compatible_pin, state, binding, configured_owner,
                writable_repository_ids=writable_repository_ids)
        except OwnerRoutingError as exc:
            if (not isinstance(exc, _CancelledRecipientContextRequired)
                    and str(exc) != 'owner_recipient_native_scope_incompatible') or self.recipient_preparer is None:
                raise
        return await self.recipient_preparer.prepare(state, binding, configured_owner,
            writable_repository_ids=writable_repository_ids)

    def compatible_pin(self, state, binding, configured_owner, *, writable_repository_ids, lookup_actor=None):
        """Choose a unique current canonical executor without moving its source binding."""
        requested = set(writable_repository_ids or ())
        if not requested:
            return self.pin(state, binding, configured_owner, lookup_actor=lookup_actor)
        owner = normalized_owner(configured_owner)
        routing = self.snapshot(state)
        if routing is None:
            raise OwnerRoutingError('owner_routing_definition_unavailable')
        owner = routing.owner(owner)
        if owner not in routing.known_owners:
            raise OwnerRoutingError('owner_routing_owner_unavailable')
        actor = (lookup_actor or self.control_actor)()
        if binding.is_master:
            raise OwnerRoutingError('owner_executor_binding_changed')
        try:
            source = self.bootstraps.get_by_thread(binding.thread_id, actor)
            source_assignment = self.assignments(source.assignment_id)
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_executor_bootstrap_unverified') from exc
        if (source_assignment is None or (source_assignment.organization_id, source_assignment.workspace_id, source_assignment.project_id)
                != (state.organization_id, state.workspace_id, state.project_id)):
            raise OwnerRoutingError('owner_executor_bootstrap_unverified')
        if self.execution_profiles is not None:
            profile, _ = self.executor(state, binding, owner, lookup_actor=lookup_actor)
            execution_profile, _ = self.execution_profiles.resolve(profile.execution_profile_id,
                organization_id=state.organization_id, workspace_id=state.workspace_id, project_id=state.project_id)
            if execution_profile.repository_access == 'none':
                return self.pin(state, binding, configured_owner, lookup_actor=lookup_actor)
        accepted = self._accepted_owner_recipient(state, binding, owner, requested,
            lookup_actor=lookup_actor)
        if accepted is not None:
            intent = routing.binding(configured_owner) or OwnerRoutingBinding(schema_version='1.1',
                organization_id=state.organization_id, workspace_id=state.workspace_id,
                project_id=state.project_id, configured_owner=normalized_owner(configured_owner),
                canonical_owner=owner, definition=routing.definition)
            return OwnerRoutingPin(schema_version='1.1', binding=intent, recipient=accepted[0],
                work_item_ref=state.ref, stage=state.current_stage,
                retry_attempt=state.execution.retry.attempt, handoff_requested_at=None,
                bot_binding_id=binding.id, original_thread_id=binding.thread_id)
        required_replacement = None
        try:
            native_compatible = self.native_scope_compatible(state, source, source_assignment)
        except _MissingNativePredecessor:
            self.require_resolved_originals(state)
            try:
                required_replacement = self._verified_missing_predecessor_replacement(
                    state, binding, owner, source_assignment, requested, lookup_actor=lookup_actor)
            except OwnerRoutingError as exc:
                if str(exc) != 'owner_recipient_custody_unavailable':
                    raise
                self.cancelled_recipient_source(state, binding, owner, requested, lookup_actor=lookup_actor)
                required_replacement = self._prepared_cancelled_recipient(
                    state, binding, configured_owner, requested, lookup_actor=lookup_actor)
            native_compatible = True
        source_scope = getattr(source_assignment, 'repository_scope', None)
        if (source_scope is not None and set(source_scope.writable_repository_ids) == requested
                and not source_scope.read_only_repository_ids
                and native_compatible and required_replacement is None):
            # Equal historical scope is not a current executor when canonical
            # recovery already linked this source to another conversation.
            # Unreplaced first admission/recovery keeps its existing bootstrap.
            try:
                replacement = self.replacement_resolver(binding.thread_id,
                    project_id=state.project_id, organization_id=state.organization_id,
                    workspace_id=state.workspace_id)
            except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
                raise OwnerRoutingError('owner_executor_replacement_unverified') from exc
            if not replacement or replacement == binding.thread_id:
                native = self.sessions.native_custody(binding.thread_id) if self.sessions is not None else None
                if (str(source_assignment.status) == 'cancelled' and native is not None
                        and native.assignment_id == source_assignment.id):
                    self.cancelled_recipient_source(state, binding, owner, requested, lookup_actor=lookup_actor)
                    required_replacement = self._prepared_cancelled_recipient(
                        state, binding, configured_owner, requested, lookup_actor=lookup_actor)
                else:
                    # Historical native custody on another terminal assignment
                    # retains ordinary same-thread recovery; it is not the new
                    # positive current-cancellation preparation boundary.
                    return self.pin(state, binding, configured_owner, lookup_actor=lookup_actor)
            elif self.sessions is not None:
                try:
                    replacement_bootstrap = self.bootstraps.get_by_thread(replacement, actor)
                except LookupError:
                    replacement_bootstrap = None
                replacement_assignment = (self.assignments(replacement_bootstrap.assignment_id)
                    if replacement_bootstrap is not None else None)
                if replacement_assignment is not None and str(replacement_assignment.status) == 'cancelled':
                    self.cancelled_recipient_source(state, binding, owner, requested, lookup_actor=lookup_actor)
                    required_replacement = self._prepared_cancelled_recipient(
                        state, binding, configured_owner, requested, lookup_actor=lookup_actor)
        self.require_resolved_originals(state)
        candidates = self.bootstraps.list(actor)
        if len(candidates) > 5000:
            raise OwnerRoutingError('owner_recipient_lookup_truncated')
        matches = []
        for bootstrap in candidates:
            assignment = self.assignments(bootstrap.assignment_id)
            scope = getattr(assignment, 'repository_scope', None)
            profile = getattr(assignment, 'agent_profile', None)
            if (assignment is None or profile is None or profile.role_id != owner
                    or assignment.created_by != source_assignment.created_by
                    or assignment.project_id != state.project_id or scope is None
                    or set(scope.writable_repository_ids) != requested):
                continue
            try:
                witness, _, _ = self._live_recipient(state, binding, bootstrap, owner, lookup_actor=lookup_actor)
            except OwnerRoutingError:
                continue
            matches.append(witness)
        if required_replacement is not None and (len(matches) != 1
                or matches[0].thread_id != required_replacement):
            raise OwnerRoutingError('owner_recipient_ambiguous' if len(matches) > 1
                else 'owner_recipient_native_custody_unverified')
        if len(matches) != 1:
            request_incompatible = not native_compatible
            if (not matches and native_compatible and source_scope is not None
                    and (set(source_scope.writable_repository_ids) != requested
                         or source_scope.read_only_repository_ids)
                    and self.sessions is not None):
                # Original native custody can agree with the old bootstrap
                # while both differ from this Work Item's requested scope.
                # Only positive native custody makes this a fresh-context
                # case; absent custody keeps ordinary admission/recovery.
                try:
                    request_incompatible = self.sessions.native_custody(binding.thread_id) is not None
                except (LookupError, ValueError, RuntimeError) as exc:
                    raise OwnerRoutingError('owner_recipient_native_custody_unverified') from exc
            raise OwnerRoutingError('owner_recipient_ambiguous' if matches else
                'owner_recipient_native_scope_incompatible' if request_incompatible else
                'owner_recipient_compatible_lane_unavailable')
        intent = routing.binding(configured_owner) or OwnerRoutingBinding(schema_version='1.1',
            organization_id=state.organization_id, workspace_id=state.workspace_id,
            project_id=state.project_id, configured_owner=normalized_owner(configured_owner),
            canonical_owner=owner, definition=routing.definition)
        handoff = state.handoff
        if (handoff is not None and handoff.status == 'pending'
                and intent.schema_version == '1.0' and handoff.owner_routing != intent):
            raise OwnerRoutingError('owner_alias_intent_unbound_or_changed')
        return OwnerRoutingPin(schema_version='1.1', binding=intent, recipient=matches[0],
            work_item_ref=state.ref, stage=state.current_stage, retry_attempt=state.execution.retry.attempt,
            handoff_requested_at=handoff.requested_at if handoff and handoff.status == 'pending' else None,
            bot_binding_id=binding.id, original_thread_id=binding.thread_id)

    def _accepted_initial_seal_compatible(self, pinned, fresh):
        """One same-owner initial seal may expand only its recorded references.

        Never transfer a queued witness across a rebind or fence. Reconstruct
        the exact pre-seal authority fingerprint instead of ignoring checksum
        differences; current accepted lineage and native custody already passed.
        """
        if pinned.model_dump(exclude={'authority_checksum'}) != fresh.model_dump(
                exclude={'authority_checksum'}):
            return False
        current = self.assignments(fresh.assignment_id)
        if current is None or self._authority_checksum(current) != fresh.authority_checksum:
            return False
        seal = current.native_credential_custody
        worker = self.workers(fresh.worker_id) if self.workers is not None else None
        if (seal is None or seal.admitted_at is None or worker is None
                or current.id != fresh.assignment_id or current.fence != fresh.fence
                or current.assigned_worker_id != fresh.worker_id
                or seal.fence != fresh.fence
                or seal.requester_identity_id != current.created_by
                or seal.worker_identity_id != worker.service_identity_id):
            return False
        allowed = set(seal.procedure_secret_refs)
        pending_refs = (seal.runtime_secret_ref,) if seal.runtime_secret_ref else ()
        allowed.update(pending_refs)
        if set(current.secret_refs) != allowed:
            return False
        pending = current.model_copy(update={'secret_refs': pending_refs})
        return self._authority_checksum(pending) == pinned.authority_checksum

    def recipient_context(self, pin, state, binding, *, lookup_actor=None):
        if binding.is_master or binding.id != pin.bot_binding_id or binding.thread_id != pin.original_thread_id:
            raise OwnerRoutingError('owner_executor_binding_changed')
        if self._work_item_owner_checksum(state) != pin.recipient.work_item_owner_checksum:
            raise OwnerRoutingError('owner_routing_work_item_changed')
        if pin.handoff_requested_at is None:
            _, writable = work_item_repository_scope(state)
            accepted = self._accepted_owner_recipient(state, binding,
                pin.binding.canonical_owner, set(writable), lookup_actor=lookup_actor,
                originals_checked=True)
            if accepted is not None:
                if (accepted[0] != pin.recipient
                        and not self._accepted_initial_seal_compatible(pin.recipient, accepted[0])):
                    raise OwnerRoutingError('owner_recipient_custody_changed')
                return accepted[1], accepted[2]
        try:
            source = self.bootstraps.get_by_thread(binding.thread_id, (lookup_actor or self.control_actor)())
            original = self.assignments(source.assignment_id)
            if original is None:
                raise OwnerRoutingError('owner_executor_bootstrap_unverified')
            try:
                native_compatible = self.native_scope_compatible(state, source, original)
            except _MissingNativePredecessor:
                # The caller has already checked retained originals with its
                # exact current admission exclusion; do not block that own START.
                _, requested = work_item_repository_scope(state)
                try:
                    target = self._verified_missing_predecessor_replacement(
                        state, binding, pin.binding.canonical_owner, original, requested, lookup_actor=lookup_actor)
                except OwnerRoutingError as exc:
                    if (str(exc) != 'owner_recipient_custody_unavailable'
                            or self.replacement_resolver(binding.thread_id, project_id=state.project_id,
                                organization_id=state.organization_id, workspace_id=state.workspace_id)
                                == pin.recipient.thread_id):
                        raise
                    self.cancelled_recipient_source(state, binding, pin.binding.canonical_owner,
                        requested, lookup_actor=lookup_actor)
                    target = self._prepared_cancelled_recipient(state, binding,
                        pin.binding.configured_owner, requested, lookup_actor=lookup_actor,
                        originals_checked=True)
                if target != pin.recipient.thread_id:
                    raise OwnerRoutingError('owner_recipient_custody_changed')
            else:
                source_scope = original.repository_scope
                source_native = (self.sessions.native_custody(binding.thread_id)
                    if self.sessions is not None else None)
                _, requested = work_item_repository_scope(state)
                if (native_compatible and str(original.status) == 'cancelled'
                        and source_scope is not None
                        and set(source_scope.writable_repository_ids) == set(requested)
                        and not source_scope.read_only_repository_ids
                        and pin.recipient.thread_id != binding.thread_id
                        and self.replacement_resolver(binding.thread_id, project_id=state.project_id,
                            organization_id=state.organization_id, workspace_id=state.workspace_id)
                            != pin.recipient.thread_id
                        and source_native is not None and source_native.assignment_id == original.id):
                    self.cancelled_recipient_source(state, binding, pin.binding.canonical_owner,
                        requested, lookup_actor=lookup_actor)
                    target = self._prepared_cancelled_recipient(state, binding,
                        pin.binding.configured_owner, requested, lookup_actor=lookup_actor,
                        originals_checked=True)
                    if target != pin.recipient.thread_id:
                        raise OwnerRoutingError('owner_recipient_custody_changed')
            bootstrap = self.bootstraps.get_by_thread(pin.recipient.thread_id, (lookup_actor or self.control_actor)())
            witness, profile, actor = self._live_recipient(state, binding, bootstrap, pin.binding.canonical_owner, lookup_actor=lookup_actor)
        except OwnerRoutingError:
            raise
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_recipient_custody_unavailable') from exc
        if witness != pin.recipient:
            raise OwnerRoutingError('owner_recipient_custody_changed')
        assignment = self.assignments(witness.assignment_id)
        scope = assignment.repository_scope
        _, writable = work_item_repository_scope(state)
        if scope is None or set(scope.writable_repository_ids) != set(writable):
            raise OwnerRoutingError('owner_recipient_work_item_scope_changed')
        return profile, actor

    def repository_scope_for_pin(self, pin, binding):
        """Read only the current recipient's already-admitted full scope.

        The typed witness pins all scope/resource authority. No caller-provided
        repository is added to this existing assignment.
        """
        state = self.get_state(pin.work_item_ref)
        self.validate_intent(pin, state)
        self.require_resolved_originals(state, pin=pin)
        if pin.recipient is None:
            raise OwnerRoutingError('owner_recipient_scope_unavailable')
        self.recipient_context(pin, state, binding)
        return self.assignments(pin.recipient.assignment_id).repository_scope

    def pin(self, state, binding, configured_owner, *, lookup_actor=None):
        routing = self.snapshot(state)
        intent = routing.binding(configured_owner) if routing is not None else None
        if intent is None:
            return None
        self.require_resolved_originals(state)
        self.executor(state, binding, intent.canonical_owner, lookup_actor=lookup_actor)
        handoff = state.handoff
        pending = handoff is not None and handoff.status == 'pending'
        if pending and (handoff.owner_routing != intent
                or normalized_owner(handoff.to_agent) != intent.configured_owner
                or (handoff.delivery is not None and handoff.delivery.status == 'delivered')):
            # Do not attach a new interpretation to a retained/possibly started
            # legacy handoff. A governed new intent must identify its recipient.
            raise OwnerRoutingError('owner_alias_intent_unbound_or_changed')
        return OwnerRoutingPin(binding=intent, work_item_ref=state.ref, stage=state.current_stage,
            retry_attempt=state.execution.retry.attempt,
            handoff_requested_at=handoff.requested_at if pending else None,
            bot_binding_id=binding.id, original_thread_id=binding.thread_id)

    def context_for_pin(self, pin, binding, *, lookup_actor=None):
        state = self.get_state(pin.work_item_ref)
        self.validate_intent(pin, state)
        self.require_resolved_originals(state)
        if pin.recipient is not None:
            return self.recipient_context(pin, state, binding, lookup_actor=lookup_actor)
        self.validate_thread(pin, state, binding)
        return self.executor(state, binding, pin.binding.canonical_owner, lookup_actor=lookup_actor)

    def validate_thread(self, pin, state, binding):
        if binding.thread_id == pin.original_thread_id:
            if binding.id != pin.bot_binding_id:
                raise OwnerRoutingError('owner_executor_binding_changed')
        else:
            replacement = self.replacement_resolver(pin.original_thread_id,
                project_id=state.project_id, organization_id=state.organization_id,
                workspace_id=state.workspace_id)
            if replacement != binding.thread_id:
                raise OwnerRoutingError('owner_executor_replacement_unverified')

    def validate_intent(self, pin, state):
        pin.binding.for_work_item(state)
        if (state.ref != pin.work_item_ref or state.closed_at or state.current_stage == 'closed'
                or state.current_stage != pin.stage or state.execution.retry.attempt != pin.retry_attempt):
            raise OwnerRoutingError('owner_routing_work_item_changed')
        routing = self.snapshot(state)
        expected = routing.binding(pin.binding.configured_owner) if routing is not None else None
        if (routing is not None and pin.binding.schema_version == '1.1'
                and pin.binding.configured_owner == pin.binding.canonical_owner
                and pin.binding.canonical_owner in routing.known_owners
                and routing.owner(pin.binding.configured_owner) == pin.binding.canonical_owner):
            expected = pin.binding.model_copy(update={'definition': routing.definition})
        if routing is None or expected != pin.binding:
            raise OwnerRoutingError('owner_routing_definition_changed')
        if pin.handoff_requested_at is not None:
            handoff = state.handoff
            if (handoff is None or handoff.status != 'pending'
                    or handoff.requested_at != pin.handoff_requested_at
                    or (handoff.owner_routing != pin.binding and not (pin.binding.schema_version == '1.1'
                        and handoff.owner_routing is None))
                    or normalized_owner(handoff.to_agent) != pin.binding.configured_owner
                    or (handoff.delivery is not None and handoff.delivery.status == 'delivered')):
                raise OwnerRoutingError('owner_routing_handoff_changed')
        elif ((state.handoff is not None and state.handoff.status == 'pending')
                or normalized_owner(actionable_owner_value(state)) != pin.binding.configured_owner):
            raise OwnerRoutingError('owner_routing_owner_changed')

    def validate_native(self, pin, *, thread_id, project, work_item_ref, handoff_requested_at,
                        agent_profile_id, agent_profile_revision, agent_profile_actor_id,
                        queued_admission_attempt_id=None, writable_repository_resource_ids=(),
                        read_only_repository_resource_ids=()):
        state = self.get_state(pin.work_item_ref)
        self.validate_intent(pin, state)
        self.require_resolved_originals(state, excluding_attempt_id=queued_admission_attempt_id, pin=pin)
        if ((project.organization_id, project.workspace_id, project.id)
                != (state.organization_id, state.workspace_id, state.project_id)
                or work_item_ref != pin.work_item_ref or handoff_requested_at != pin.handoff_requested_at):
            raise OwnerRoutingError('owner_routing_request_mismatch')
        if pin.recipient is not None:
            if thread_id != pin.recipient.thread_id:
                raise OwnerRoutingError('owner_recipient_thread_changed')
            sources = [item for item in self.load_bindings() if item.id == pin.bot_binding_id]
            if len(sources) != 1:
                raise OwnerRoutingError('owner_executor_binding_changed')
            profile, actor = self.recipient_context(pin, state, sources[0])
            scope = self.assignments(pin.recipient.assignment_id).repository_scope
            if (set(writable_repository_resource_ids) != set(scope.writable_repository_ids)
                    or set(read_only_repository_resource_ids) != set(scope.read_only_repository_ids)):
                raise OwnerRoutingError('owner_recipient_request_scope_changed')
            if (profile.profile_id, profile.revision, actor.identity_id) != (
                    agent_profile_id, agent_profile_revision, agent_profile_actor_id):
                raise OwnerRoutingError('owner_executor_profile_changed')
            return
        candidates = [item for item in self.load_bindings() if item.thread_id == thread_id
            and item.project_id == state.project_id and not item.is_master
            and normalized_owner(self.binding_prefix(item)) == pin.binding.canonical_owner]
        if thread_id == pin.original_thread_id:
            candidates = [item for item in candidates if item.id == pin.bot_binding_id]
        if not candidates:
            raise OwnerRoutingError('owner_executor_binding_changed')
        binding = sorted(candidates, key=lambda item: item.id)[0]
        self.validate_thread(pin, state, binding)
        profile, actor = self.executor(state, binding, pin.binding.canonical_owner)
        if (profile.profile_id, profile.revision, actor.identity_id) != (
                agent_profile_id, agent_profile_revision, agent_profile_actor_id):
            raise OwnerRoutingError('owner_executor_profile_changed')
