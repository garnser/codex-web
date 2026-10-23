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


class OwnerRoutingService:
    def __init__(self, *, routing, get_state, projects, load_bindings, binding_prefix,
                 bootstraps, assignments, profiles, control_actor, actor_resolver,
                 replacement_resolver, update_state=None, append_event=None, preflight=None, workers=None, execution_profiles=None):
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

    def executor(self, state, binding, owner, *, recipient_thread_id=None):
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
        lookup_actor = self.control_actor()
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

    def _live_recipient(self, state, binding, bootstrap, owner):
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
        profile, actor = self.executor(state, binding, owner, recipient_thread_id=bootstrap.thread_id)
        return OwnerRoutingRecipient(thread_id=bootstrap.thread_id, bootstrap_id=bootstrap.id,
            assignment_id=assignment.id, execution_id=assignment.execution_id,
            worker_id=assignment.assigned_worker_id, fence=assignment.fence,
            authority_checksum=self._authority_checksum(assignment),
            work_item_owner_checksum=self._work_item_owner_checksum(state)), profile, actor

    def compatible_pin(self, state, binding, configured_owner, *, writable_repository_ids):
        """Choose a unique current canonical executor without moving its source binding."""
        requested = set(writable_repository_ids or ())
        if not requested:
            return self.pin(state, binding, configured_owner)
        owner = normalized_owner(configured_owner)
        routing = self.snapshot(state)
        if routing is None:
            raise OwnerRoutingError('owner_routing_definition_unavailable')
        owner = routing.owner(owner)
        if owner not in routing.known_owners:
            raise OwnerRoutingError('owner_routing_owner_unavailable')
        actor = self.control_actor()
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
            profile, _ = self.executor(state, binding, owner)
            execution_profile, _ = self.execution_profiles.resolve(profile.execution_profile_id,
                organization_id=state.organization_id, workspace_id=state.workspace_id, project_id=state.project_id)
            if execution_profile.repository_access == 'none':
                return self.pin(state, binding, configured_owner)
        scope = getattr(source_assignment, 'repository_scope', None)
        if scope is not None and set(scope.writable_repository_ids) == requested and not scope.read_only_repository_ids:
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
                return self.pin(state, binding, configured_owner)
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
                witness, _, _ = self._live_recipient(state, binding, bootstrap, owner)
            except OwnerRoutingError:
                continue
            matches.append(witness)
        if len(matches) != 1:
            raise OwnerRoutingError('owner_recipient_ambiguous' if matches else 'owner_recipient_compatible_lane_unavailable')
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

    def recipient_context(self, pin, state, binding):
        if binding.is_master or binding.id != pin.bot_binding_id or binding.thread_id != pin.original_thread_id:
            raise OwnerRoutingError('owner_executor_binding_changed')
        if self._work_item_owner_checksum(state) != pin.recipient.work_item_owner_checksum:
            raise OwnerRoutingError('owner_routing_work_item_changed')
        try:
            bootstrap = self.bootstraps.get_by_thread(pin.recipient.thread_id, self.control_actor())
            witness, profile, actor = self._live_recipient(state, binding, bootstrap, pin.binding.canonical_owner)
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

    def pin(self, state, binding, configured_owner):
        routing = self.snapshot(state)
        intent = routing.binding(configured_owner) if routing is not None else None
        if intent is None:
            return None
        self.require_resolved_originals(state)
        self.executor(state, binding, intent.canonical_owner)
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

    def context_for_pin(self, pin, binding):
        state = self.get_state(pin.work_item_ref)
        self.validate_intent(pin, state)
        self.require_resolved_originals(state)
        if pin.recipient is not None:
            return self.recipient_context(pin, state, binding)
        self.validate_thread(pin, state, binding)
        return self.executor(state, binding, pin.binding.canonical_owner)

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
