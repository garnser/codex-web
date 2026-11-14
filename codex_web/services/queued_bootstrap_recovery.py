from __future__ import annotations

import asyncio

from fastapi import HTTPException

from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.models import QueuedTurn, ExecutionPreflightAttempt
from codex_web.services.identity import IdentityService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
from codex_web.services.threads import ThreadBootstrapPrestartRefusal


class QueuedBootstrapRecoveryService:
    """Create a fresh, correlated bootstrap without claiming a native Turn."""

    def __init__(self, *, preflight, threads, bootstrap_bindings, assignments,
                 thread_index, actor_resolver, thread_is_active, ownership=None, queue_lock=None, identity=None):
        self.preflight = preflight
        self.threads = threads
        self.bootstrap_bindings = bootstrap_bindings
        self.assignments = assignments
        self.thread_index = thread_index
        self.actor_resolver = actor_resolver
        self.thread_is_active = thread_is_active
        self.ownership = ownership
        self.queue_lock = queue_lock
        self.identity = identity
        self.owner_routing = None

    @staticmethod
    def refused(code, message):
        raise HTTPException(status_code=409, detail={'code':code, 'message':message})

    def recovery_source(self, attempt, *, project, actor):
        recovery = attempt.bootstrap_recovery
        if recovery is not None and recovery.state == 'created' and not self.preflight.recorded_initial_process_refusal(attempt):
            return attempt.replacement_thread_id or attempt.thread_id
        target = self.thread_index.replacement_thread_id(attempt.thread_id,
            project_id=project.id, organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        if (recovery is not None and recovery.state in {'pending', 'refused'}
                and (target or attempt.thread_id) != (attempt.replacement_thread_id or attempt.thread_id)):
            self.refused('bootstrap_recovery_route_changed', 'The reserved recovery source has acquired another successor.')
        if not target:
            return attempt.thread_id
        if self.preflight.recorded_custody_refusal(attempt):
            queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
            proof = self.verify_custody_source(attempt, queued, actor)
            if proof['threadId'] != target:
                self.refused('bootstrap_recovery_route_changed', 'The custody refusal belongs to another source.')
            return target
        # A verified route normally wins. Only a pending initial destination
        # whose worker is affirmatively terminal/fenced can acquire a successor.
        # This says nothing about another original's possible native outcome.
        try:
            binding = self.bootstrap_bindings.get_by_thread(target, actor)
        except ThreadBootstrapBindingNotFoundError:
            self.refused('bootstrap_recovery_route_exists', 'Existing destination requires canonical reconciliation.')
        assignment = self.assignments(binding.assignment_id)
        indexed = self.thread_index.get(target)
        if (not binding.initial_turn_pending or self.thread_is_active(target)
                or indexed is None or indexed.project_id != project.id
                or assignment is None or assignment.status not in {'cancelled', 'failed', 'succeeded'}
                or assignment.lease is not None or assignment.execution_id != binding.execution_id
                or (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                    != (attempt.organization_id, attempt.workspace_id, project.id)):
            self.refused('bootstrap_recovery_route_exists', 'Use the existing verified replacement and ordinary retry, or fence its unusable pending worker.')
        if recovery is not None and recovery.state == 'created':
            proof = next(b.model_dump(mode='json') for b in attempt.blockers
                if b.code == 'initial_bootstrap_process_lost')
            if (proof.get('bootstrap_id'), proof.get('assignment_id')) != (binding.bootstrap_id, binding.assignment_id):
                self.refused('bootstrap_recovery_route_changed', 'The controller refusal belongs to another bootstrap.')
        return target

    def verify_custody_source(self, attempt, queued, actor):
        """A live-history thread may acquire a successor only after fencing.

        This checks the controller's exact refused assignment, never the lack
        of a provider receipt or the thread's display name.
        """
        proof = next(b.model_dump(mode='json') for b in attempt.blockers
            if b.code in {'native_session_scope_incompatible', 'native_session_prior_assignment_missing'})
        try:
            binding = self.bootstrap_bindings.get_by_thread(proof['threadId'], actor)
        except ThreadBootstrapBindingNotFoundError:
            self.refused('bootstrap_recovery_source_unverified', 'Native custody binding is unavailable.')
        assignment = self.assignments(proof['assignment_id'])
        if proof['code'] == 'native_session_prior_assignment_missing' and self.assignments(proof['previous_assignment_id']) is not None:
            self.refused('bootstrap_recovery_source_unverified', 'Prior assignment custody changed; inspect it before recovery.')
        if (binding.assignment_id != proof['assignment_id'] or binding.bootstrap_id != proof['bootstrap_id']
                or assignment is None or assignment.status not in {'cancelled', 'failed', 'succeeded', 'lost'}
                or assignment.lease is not None
                or assignment.fence not in {proof['assignment_fence'], proof['assignment_fence'] + 1}
                or assignment.execution_id != proof['execution_id']
                or assignment.execution_workspace_id != proof['execution_workspace_id']
                or assignment.execution_id != binding.execution_id
                or assignment.execution_workspace_id != binding.execution_workspace_id
                or (assignment.organization_id, assignment.workspace_id, assignment.project_id, assignment.created_by)
                    != (attempt.organization_id, attempt.workspace_id, attempt.project_id, queued.agent_profile_actor_id)
                or assignment.agent_profile is None
                or (assignment.agent_profile.profile_id, assignment.agent_profile.profile_revision)
                    != (queued.agent_profile_id, queued.agent_profile_revision)
                or set(assignment.resource_ids) != set(proof['requested_resource_ids'])
                or (assignment.repository_scope.model_dump(mode='json') if assignment.repository_scope else None)
                    != proof['requested_repository_scope']):
            self.refused('bootstrap_recovery_source_unverified', 'Refused native custody is not terminal, fenced and unchanged.')
        return proof

    def verify_initial_process_source(self, attempt, queued, actor):
        """Validate the controller's exact post-stop, before-input refusal.

        The refusal is emitted only after awaited bootstrap cancellation and
        process cleanup. This explicit operator path does not infer process
        loss from a missing handle or authorize automatic owner recovery.
        """
        if not self.preflight.recorded_initial_process_refusal(attempt):
            self.refused('bootstrap_recovery_source_unverified', 'No exact initial-process controller refusal is retained.')
        proof = next(b.model_dump(mode='json') for b in attempt.blockers
            if b.code == 'initial_bootstrap_process_lost')
        try:
            binding = self.bootstrap_bindings.get_by_thread(proof['threadId'], actor)
        except ThreadBootstrapBindingNotFoundError:
            self.refused('bootstrap_recovery_source_unverified', 'Initial bootstrap custody is unavailable.')
        assignment = self.assignments(proof['assignment_id'])
        profile = getattr(assignment, 'agent_profile', None)
        scope = getattr(assignment, 'repository_scope', None)
        target = getattr(assignment, 'repository_target', None)
        expected_resources = set(queued.writable_repository_resource_ids) | set(queued.read_only_repository_resource_ids)
        if queued.repository_resource_id:
            expected_resources.add(queued.repository_resource_id)
        if queued.resource_context:
            expected_resources.update(queued.resource_context.read_only_resource_ids)
        if (not binding.initial_turn_pending or binding.bootstrap_id != proof['bootstrap_id']
                or binding.assignment_id != proof['assignment_id'] or assignment is None
                or assignment.status != 'cancelled' or assignment.lease is not None
                or assignment.fence < 1 or assignment.failure_code != 'thread_bootstrap_cancelled'
                or assignment.execution_id != binding.execution_id
                or assignment.execution_workspace_id != binding.execution_workspace_id
                or assignment.subject.kind != 'thread_bootstrap' or assignment.subject.ref != binding.bootstrap_id
                or (assignment.organization_id, assignment.workspace_id, assignment.project_id, assignment.created_by)
                    != (attempt.organization_id, attempt.workspace_id, attempt.project_id, queued.agent_profile_actor_id)
                or profile is None or (profile.profile_id, profile.profile_revision)
                    != (queued.agent_profile_id, queued.agent_profile_revision)
                or assignment.execution_profile_id != queued.execution_profile_id
                or assignment.sandbox != queued.sandbox or assignment.approval_policy != queued.approval_policy
                or scope is None or set(scope.writable_repository_ids) != set(queued.writable_repository_resource_ids)
                or set(scope.read_only_repository_ids) != set(queued.read_only_repository_resource_ids)
                or target is None or target.primary_repository_id != queued.repository_resource_id
                or set(assignment.resource_ids) != expected_resources
                or getattr(assignment, 'resource_context', None) != queued.resource_context
                or self.thread_is_active(proof['threadId'])):
            self.refused('bootstrap_recovery_source_unverified', 'Refused initial custody is not cancelled, fenced and unchanged in its original scope.')
        return proof

    def verify_native_preinput_source(self, attempt, queued, actor):
        """Positive renderer refusal plus exact canonical retired source custody."""
        proof = self.preflight.recorded_native_preinput_refusal(attempt)
        if proof is None:
            self.refused('bootstrap_recovery_source_unverified', 'Exact controller before-input proof is unavailable.')
        binding = self.bootstrap_bindings.get_by_thread(proof.thread_id, actor)
        assignment = self.assignments(proof.assignment_id)
        from codex_web.services.owner_routing import OwnerRoutingService
        pin = queued.owner_routing_pin
        resources = set(queued.writable_repository_resource_ids) | set(queued.read_only_repository_resource_ids)
        if queued.repository_resource_id:
            resources.add(queued.repository_resource_id)
        if queued.resource_context:
            resources.update(queued.resource_context.read_only_resource_ids)
        if (assignment is None or str(assignment.status) != 'cancelled' or assignment.lease is not None
                or assignment.fence != proof.fence + 1 or assignment.failure_code != 'thread_bootstrap_cancelled'
                or not binding.initial_turn_pending
                or binding.id != proof.bootstrap_binding_id
                or (binding.bootstrap_id, binding.assignment_id, binding.execution_id, binding.execution_workspace_id)
                    != (proof.bootstrap_id, proof.assignment_id, proof.execution_id, proof.execution_workspace_id)
                or (assignment.execution_id, assignment.execution_workspace_id, assignment.assigned_worker_id)
                    != (proof.execution_id, proof.execution_workspace_id, proof.worker_id)
                or assignment.subject is None or assignment.subject.kind != 'thread_bootstrap'
                or assignment.subject.ref != proof.bootstrap_id
                or OwnerRoutingService._authority_checksum(assignment) != proof.assignment_authority_sha256
                or (assignment.organization_id, assignment.workspace_id, assignment.project_id, assignment.created_by)
                    != (attempt.organization_id, attempt.workspace_id, attempt.project_id, queued.agent_profile_actor_id)
                or assignment.agent_profile is None
                or (assignment.agent_profile.profile_id, assignment.agent_profile.profile_revision)
                    != (queued.agent_profile_id, queued.agent_profile_revision)
                or assignment.execution_profile_id != queued.execution_profile_id
                or assignment.sandbox != queued.sandbox or assignment.approval_policy != queued.approval_policy
                or set(assignment.resource_ids) != resources or assignment.resource_context != queued.resource_context
                or assignment.repository_scope is None
                or set(assignment.repository_scope.writable_repository_ids) != set(queued.writable_repository_resource_ids)
                or set(assignment.repository_scope.read_only_repository_ids) != set(queued.read_only_repository_resource_ids)
                or assignment.repository_target is None
                or assignment.repository_target.primary_repository_id != queued.repository_resource_id
                or assignment.runtime_binding is None
                or (assignment.runtime_binding.provider_id, assignment.runtime_binding.runtime_id) != ('openai', 'codex')
                or pin is None or pin.recipient is None
                or (pin.recipient.thread_id, pin.recipient.bootstrap_id, pin.recipient.assignment_id,
                    pin.recipient.execution_id, pin.recipient.worker_id, pin.recipient.fence)
                    != (proof.thread_id, proof.bootstrap_binding_id, proof.assignment_id, proof.execution_id,
                        proof.worker_id, proof.fence)
                or self.thread_is_active(proof.thread_id)):
            self.refused('bootstrap_recovery_source_unverified', 'Before-input source custody is not exactly retired in its original scope.')
        owner = self.owner_routing
        if owner is None:
            self.refused('bootstrap_recovery_owner_unavailable', 'Current Work owner validation is unavailable.')
        from codex_web.owner_routing import OwnerRoutingError
        try:
            state = owner.get_state(pin.work_item_ref)
            owner.validate_intent(pin, state)
            owner.require_resolved_originals(state, excluding_attempt_id=attempt.id, pin=pin)
            from codex_web.services.work_item_dependencies import work_item_repository_scope
            bindings = [item for item in owner.load_bindings() if item.id == pin.bot_binding_id]
            if (len(bindings) != 1 or bindings[0].thread_id != pin.original_thread_id
                    or owner._work_item_owner_checksum(state) != pin.recipient.work_item_owner_checksum
                    or set(work_item_repository_scope(state)[1]) != set(queued.writable_repository_resource_ids)):
                raise OwnerRoutingError('owner_context_changed')
            profile, current_actor = owner.executor(state, bindings[0], pin.binding.canonical_owner,
                recipient_thread_id=proof.thread_id)
            if (profile.profile_id, profile.revision, current_actor.identity_id) != (
                    queued.agent_profile_id, queued.agent_profile_revision, queued.agent_profile_actor_id):
                raise OwnerRoutingError('owner_context_changed')
        except (OwnerRoutingError, LookupError, RuntimeError, HTTPException) as exc:
            self.refused('bootstrap_recovery_owner_changed', 'Original Work Profile or actor changed.')
        return proof

    def context(self, attempt_id, *, project, actor, thread_id=None):
        attempt = self.preflight.get(attempt_id, actor=actor)
        if thread_id is not None and thread_id not in {attempt.thread_id, attempt.replacement_thread_id}:
            raise HTTPException(status_code=404, detail='Retained request unavailable')
        self.preflight._authorize(attempt, actor, require_admin=True)
        if actor.principal_kind == PrincipalKind.HUMAN:
            try:
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            except RuntimeError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        if (project.id != attempt.project_id or project.organization_id != attempt.organization_id
                or project.workspace_id != attempt.workspace_id):
            raise HTTPException(status_code=404, detail='Retained request unavailable')
        if (attempt.status != 'blocked' or attempt.scheduling_retirement is not None
                or attempt.queued_start_receipt is not None or attempt.started_at is not None):
            self.refused('bootstrap_recovery_unavailable', 'The original must remain blocked and proven unstarted.')
        recovery = attempt.bootstrap_recovery
        if recovery is None or recovery.state != 'created':
            if not self.preflight.recorded_prestart_refusal(attempt):
                self.refused('bootstrap_recovery_unavailable', 'No authoritative pre-start refusal is retained.')
        try:
            queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        except (ValueError, TypeError):
            self.refused('bootstrap_recovery_provenance_missing', 'Original queued request is incomplete or invalid.')
        if (not queued.agent_profile_id or queued.agent_profile_revision is None
                or not queued.agent_profile_actor_id or queued.sandbox is None
                or queued.approval_policy is None or not queued.execution_profile_id):
            self.refused('bootstrap_recovery_provenance_missing', 'Exact original actor, profile and execution settings are required.')
        original = self.thread_index.get(attempt.thread_id)
        custody_refusal = self.preflight.recorded_custody_refusal(attempt)
        initial_refusal = self.preflight.recorded_initial_process_refusal(attempt)
        native_refusal = self.preflight.recorded_native_preinput_refusal(attempt)
        custody_recovered = (recovery is not None and recovery.state == 'created'
            and recovery.destination_thread_id == attempt.replacement_thread_id)
        if original is None or (not original.archived and not custody_refusal and not custody_recovered
                and not initial_refusal and not native_refusal) or original.project_id != project.id:
            self.refused('bootstrap_recovery_source_unverified', 'Fresh recovery requires the canonical archived original thread.')
        if custody_refusal:
            self.verify_custody_source(attempt, queued, actor)
        if initial_refusal and not original.archived:
            self.verify_initial_process_source(attempt, queued, actor)
        if native_refusal:
            self.verify_native_preinput_source(attempt, queued, actor)
        target = attempt.replacement_thread_id or attempt.thread_id
        if self.thread_is_active(attempt.thread_id) or self.thread_is_active(target):
            self.refused('bootstrap_recovery_thread_active', 'Original or scheduling thread has active work.')
        original_actor = self.actor_resolver(queued.agent_profile_actor_id, project)
        if (original_actor.identity_id != queued.agent_profile_actor_id
                or original_actor.organization_id != attempt.organization_id
                or original_actor.workspace_id != attempt.workspace_id):
            self.refused('bootstrap_recovery_actor_unverified', 'Original actor scope is unverified.')
        digest = self.preflight.queued_snapshot_digest(attempt.queued_turn_snapshot)
        if recovery is not None and (recovery.queued_snapshot_sha256 != digest
                or recovery.queued_turn_id != queued.id):
            self.refused('bootstrap_recovery_snapshot_changed', 'Original request differs from the reserved bootstrap.')
        existing = self.thread_index.replacement_thread_id(attempt.thread_id,
            project_id=project.id, organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        if existing and (recovery is None or self.preflight.recorded_initial_process_refusal(attempt)):
            self.recovery_source(attempt, project=project, actor=actor)
        return attempt, queued, original_actor, digest

    def preview(self, attempt_id, *, project, actor, thread_id=None):
        attempt, queued, _, digest = self.context(attempt_id, project=project, actor=actor, thread_id=thread_id)
        recovery = attempt.bootstrap_recovery
        return {'attempt_id':attempt.id, 'snapshot_sha256':digest,
            'refusal_phase':('before_native_turn_input'
                if self.preflight.recorded_native_preinput_refusal(attempt) else None),
            'queued_turn_id':queued.id, 'original_thread_id':attempt.thread_id,
            'source_thread_id':self.recovery_source(attempt, project=project, actor=actor),
            'project_id':attempt.project_id, 'organization_id':attempt.organization_id,
            'workspace_id':attempt.workspace_id, 'agent_profile_id':queued.agent_profile_id,
            'agent_profile_revision':queued.agent_profile_revision,
            'original_actor_id':queued.agent_profile_actor_id,
            'execution_profile_id':queued.execution_profile_id,
            'repository_resource_id':queued.repository_resource_id,
            'writable_repository_resource_ids':list(queued.writable_repository_resource_ids),
            'read_only_repository_resource_ids':list(queued.read_only_repository_resource_ids),
            **({'resource_context':queued.resource_context.model_dump(mode='json')} if queued.resource_context else {}),
            'state':recovery.state if recovery else 'not_requested',
            'prestart_refusal':recovery.prestart_refusal.model_dump(mode='json')
                if recovery and recovery.prestart_refusal else None,
            'can_create':recovery is None or recovery.state == 'refused'
                or (recovery.state == 'created' and self.preflight.recorded_initial_process_refusal(attempt)),
            'can_reconcile':bool(recovery and recovery.state == 'pending'),
            'native_turn_submitted':False}

    def verify_binding(self, attempt, queued, original_actor, *, expected_thread=None):
        recovery = attempt.bootstrap_recovery
        try:
            binding = self.bootstrap_bindings.get_by_bootstrap(recovery.bootstrap_id, original_actor)
        except ThreadBootstrapBindingNotFoundError as exc:
            raise HTTPException(status_code=409, detail={
                'code':'bootstrap_recovery_pending',
                'message':'No canonical correlated bootstrap receipt is available; creation must not be repeated.'}) from exc
        assignment = self.assignments(binding.assignment_id)
        indexed = self.thread_index.get(binding.thread_id)
        profile = getattr(assignment, 'agent_profile', None)
        scope = getattr(assignment, 'repository_scope', None)
        subject = getattr(assignment, 'subject', None)
        if (binding.bootstrap_id != recovery.bootstrap_id or binding.execution_id != recovery.execution_id
                or binding.organization_id != attempt.organization_id or binding.workspace_id != attempt.workspace_id
                or not binding.initial_turn_pending or binding.thread_id == attempt.thread_id
                or (expected_thread is not None and binding.thread_id != expected_thread)
                or indexed is None or indexed.archived or indexed.project_id != attempt.project_id
                or assignment is None or assignment.execution_id != recovery.execution_id
                or assignment.status not in {'claimed', 'running'}
                or assignment.organization_id != attempt.organization_id or assignment.workspace_id != attempt.workspace_id
                or getattr(assignment,'resource_context',None)!=queued.resource_context
                or assignment.project_id != attempt.project_id or subject is None
                or subject.kind != 'thread_bootstrap' or subject.ref != recovery.bootstrap_id
                or assignment.execution_workspace_id != binding.execution_workspace_id
                or assignment.sandbox != queued.sandbox or assignment.approval_policy != queued.approval_policy
                or assignment.execution_profile_id != queued.execution_profile_id
                or profile is None or profile.profile_id != queued.agent_profile_id
                or profile.profile_revision != queued.agent_profile_revision
                or scope is None or set(scope.writable_repository_ids) != set(queued.writable_repository_resource_ids)
                or set(scope.read_only_repository_ids) != set(queued.read_only_repository_resource_ids)
                or getattr(assignment.repository_target, 'primary_repository_id', None) != queued.repository_resource_id):
            self.refused('bootstrap_recovery_binding_mismatch', 'Correlated bootstrap does not match the exact original scope.')
        return binding

    def complete(self, attempt, queued, binding, *, actor, project, fence_name, source_fence_name=None,
                 expected_initial_source=None):
        if self.ownership is not None:
            self.ownership.require_fence(fence_name)
            if source_fence_name:
                self.ownership.require_fence(source_fence_name)
        recovery = attempt.bootstrap_recovery
        custody_refusal = self.preflight.recorded_custody_refusal(attempt)
        native_refusal = self.preflight.recorded_native_preinput_refusal(attempt)
        initial_refusal = expected_initial_source is not None and not native_refusal
        guarded_source = custody_refusal or initial_refusal or native_refusal
        successor = None
        if native_refusal:
            from codex_web.owner_routing import OwnerRoutingRecipient
            target = self.assignments(binding.assignment_id)
            successor = OwnerRoutingRecipient(thread_id=binding.thread_id, bootstrap_id=binding.id,
                assignment_id=target.id, execution_id=target.execution_id,
                worker_id=target.assigned_worker_id, fence=target.fence,
                authority_checksum=self.owner_routing._authority_checksum(target),
                work_item_owner_checksum=queued.owner_routing_pin.recipient.work_item_owner_checksum)
        # This is an observed fresh bootstrap edge, never reconstructed history.
        graph_updater = self.thread_index.replacement_updater(attempt.replacement_thread_id or attempt.thread_id, binding.thread_id,
            project_id=attempt.project_id, provenance={
                'organization_id':attempt.organization_id, 'workspace_id':attempt.workspace_id,
                'actor_identity_id':queued.agent_profile_actor_id,
                'agent_profile_id':queued.agent_profile_id,
                'agent_profile_revision':queued.agent_profile_revision,
                'execution_profile_id':queued.execution_profile_id,
                'repository_resource_id':queued.repository_resource_id,
                'writable_repository_resource_ids':list(queued.writable_repository_resource_ids),
                'read_only_repository_resource_ids':list(queued.read_only_repository_resource_ids),
            **({'resource_context':queued.resource_context.model_dump(mode='json')} if queued.resource_context else {}),
                'fresh_bootstrap_recovery_id':recovery.id,
                'bootstrap_id':binding.bootstrap_id, 'bootstrap_execution_id':binding.execution_id,
                'preflight_attempt_id':attempt.id, 'requested_by':recovery.requested_by,
                **({'native_preinput_refusal':native_refusal.model_dump(mode='json')}
                    if native_refusal else {}),
                **({'native_preinput_successor':successor.model_dump(mode='json')} if successor else {})})
        def update(current):
            self.preflight._authorize(current, actor, require_admin=True)
            if guarded_source:
                try:
                    fresh = self.actor_resolver(actor.identity_id, project)
                except RuntimeError as exc:
                    raise HTTPException(status_code=403, detail='Recovery operator identity is unavailable.') from exc
                # This credential-free projection refreshes membership only.
                # MFA belongs to the authenticated request checked in context.
                self.preflight._authorize(current, fresh, require_admin=True)
                if (fresh.identity_id, fresh.organization_id, fresh.workspace_id) != (
                        actor.identity_id, actor.organization_id, actor.workspace_id):
                    raise HTTPException(status_code=403, detail='Recovery operator identity changed.')
            if guarded_source and ((custody_refusal and not self.preflight.recorded_custody_refusal(current))
                    or (initial_refusal and not self.preflight.recorded_initial_process_refusal(current))
                    or (native_refusal and self.preflight.recorded_native_preinput_refusal(current) != native_refusal)
                    or current.blockers != attempt.blockers
                    or current.queued_admission_claim_id != attempt.queued_admission_claim_id):
                self.refused('bootstrap_recovery_changed', 'Controller custody proof changed during bootstrap creation.')
            if self.preflight.recorded_custody_refusal(current):
                self.verify_custody_source(current, queued, actor)
            if initial_refusal:
                proof = self.verify_initial_process_source(current, queued, actor)
                if self.assignments(proof['assignment_id']).model_dump(mode='json') != expected_initial_source:
                    self.refused('bootstrap_recovery_source_changed', 'Initial source custody changed during bootstrap creation.')
            if native_refusal:
                proof = self.verify_native_preinput_source(current, queued, actor)
                if self.assignments(proof.assignment_id).model_dump(mode='json') != expected_initial_source:
                    self.refused('bootstrap_recovery_source_changed', 'Before-input source custody changed during bootstrap creation.')
            if (current.status != 'blocked' or current.bootstrap_recovery != recovery
                    or current.queued_turn_snapshot != attempt.queued_turn_snapshot
                    or current.replacement_thread_id != attempt.replacement_thread_id
                    or current.queued_start_receipt is not None or current.started_at is not None):
                self.refused('bootstrap_recovery_changed', 'Retained request changed during bootstrap verification.')
            return current.model_copy(update={
                'bootstrap_recovery':recovery.model_copy(update={
                    'state':'created', 'destination_thread_id':binding.thread_id}),
                'replacement_thread_id':binding.thread_id,
                'updated_at':float(self.preflight.clock())})
        attempt_key = self.preflight.store._attempt_key(attempt.id)
        attempt_ns = self.preflight.store.NAMESPACE
        graph_ns = self.thread_index.REPLACEMENTS_NAMESPACE
        selected = {attempt_ns:(attempt_key,), graph_ns:(None,)}
        if guarded_source:
            # Hold the canonical source binding and assignment against a
            # concurrent rebind/reclaim through final route publication.
            selected[self.bootstrap_bindings.store.namespace] = (None,)
            if self.identity is not None:
                if self.identity.store.store is not self.preflight.store.store:
                    raise ValueError('Custody recovery identity requires one canonical store')
                selected[self.identity.store.namespace] = (None,)
            assignment_store = getattr(self.assignments, '__self__', None)
            if hasattr(assignment_store, 'assignment_namespace'):
                if assignment_store.store is not self.preflight.store.store:
                    raise ValueError('Custody recovery requires one canonical store')
                proof = ({'assignment_id':native_refusal.assignment_id} if native_refusal else next(b.model_dump(mode='json') for b in attempt.blockers
                    if b.code in {'native_session_scope_incompatible', 'native_session_prior_assignment_missing',
                                  'initial_bootstrap_process_lost'}))
                selected[assignment_store.assignment_namespace] = tuple(dict.fromkeys((
                    proof['assignment_id'], binding.assignment_id,
                    *([proof['previous_assignment_id']] if custody_refusal else []))))
        def commit(values):
            if self.ownership is not None:
                self.ownership.require_fence(fence_name)
                if source_fence_name:
                    self.ownership.require_fence(source_fence_name)
            current = ExecutionPreflightAttempt.model_validate(values[attempt_ns][attempt_key])
            if guarded_source:
                self.verify_binding(current, queued, actor, expected_thread=binding.thread_id)
                if self.assignments(binding.assignment_id).created_by != queued.agent_profile_actor_id:
                    self.refused('bootstrap_recovery_binding_mismatch', 'Fresh custody changed the original actor.')
            if successor is not None:
                target = self.assignments(binding.assignment_id)
                if ((target.id, target.execution_id, target.assigned_worker_id, target.fence,
                        self.owner_routing._authority_checksum(target)) != (successor.assignment_id,
                        successor.execution_id, successor.worker_id, successor.fence, successor.authority_checksum)
                        or target.lease is None or target.lease.worker_id != successor.worker_id
                        or target.lease.fence != successor.fence):
                    self.refused('bootstrap_recovery_binding_mismatch', 'Fresh recipient custody changed before route publication.')
            completed = update(current)
            return {attempt_ns:{attempt_key:completed.model_dump(mode='json')},
                graph_ns:{None:graph_updater(values[graph_ns][None] or {'version':1, 'edges':{}})}}
        # Read locks on source custody share the original/graph transaction;
        # only the original's route and new observed edge are written.
        self.preflight.store.store.record_mutate_many(selected, commit)
        # Rebuild the existing projected inspection indexes. Failure here does
        # not undo the authoritative commit or authorize another bootstrap.
        return self.preflight.store.update(attempt.id, lambda current:current)

    async def create_reserved(self, attempt, queued, original_actor, project):
        try:
            return await self.threads.create(project_id=project.id,
                sandbox=queued.sandbox, approval_policy=queued.approval_policy,
                model=queued.model, reasoning_effort=queued.reasoning_effort,
                repository_resource_id=queued.repository_resource_id,
                writable_repository_resource_ids=queued.writable_repository_resource_ids,
                read_only_repository_resource_ids=queued.read_only_repository_resource_ids,
                **({"resource_context":queued.resource_context} if queued.resource_context else {}),
                execution_profile_id=queued.execution_profile_id, actor=original_actor,
                agent_profile_id=queued.agent_profile_id, agent_profile_revision=queued.agent_profile_revision,
                bootstrap_token=attempt.bootstrap_recovery.bootstrap_id.removeprefix('bootstrap-'),
                **({'admission_guard':lambda:self.verify_native_preinput_source(attempt, queued, original_actor)}
                    if self.preflight.recorded_native_preinput_refusal(attempt) else {}))
        except ThreadBootstrapPrestartRefusal as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            await asyncio.to_thread(self.preflight.record_bootstrap_refusal, attempt.id,
                recovery_id=attempt.bootstrap_recovery.id,
                code=str(detail.get('code') or 'bootstrap_prestart_refused'),
                message=str(detail.get('message') or 'Controller refused preparation before provider thread startup.'))
            raise

    async def recover(self, attempt_id, *, project, actor, expected_snapshot_sha256, reason, thread_id=None,
                      expected_source_thread_id=None):
        attempt = self.preflight.get(attempt_id, actor=actor)
        name = 'thread-queue:' + attempt.thread_id
        async def operation(owned_source=None):
            attempt, queued, original_actor, digest = self.context(attempt_id, project=project, actor=actor, thread_id=thread_id)
            if digest != expected_snapshot_sha256:
                self.refused('bootstrap_recovery_snapshot_changed', 'Recovery preview is stale.')
            source = self.recovery_source(attempt, project=project, actor=actor)
            if owned_source is not None and source != owned_source:
                self.refused('bootstrap_recovery_route_changed', 'The recovery source changed during ownership admission.')
            if owned_source is None and source != attempt.thread_id and self.ownership is not None:
                async def own_source():
                    acquired, result = await self.ownership.run_exclusive('thread-queue:' + source,
                        lambda:operation(source))
                    if not acquired:
                        self.refused('bootstrap_recovery_busy', 'The existing destination is owned elsewhere.')
                    return result
                if self.queue_lock is not None:
                    async with self.queue_lock(source):
                        return await own_source()
                return await own_source()
            initial_source = None
            if self.preflight.recorded_native_preinput_refusal(attempt):
                proof = self.verify_native_preinput_source(attempt, queued, actor)
                initial_source = self.assignments(proof.assignment_id).model_dump(mode='json')
            if (self.preflight.recorded_initial_process_refusal(attempt)
                    and not self.thread_index.get(attempt.thread_id).archived):
                proof = self.verify_initial_process_source(attempt, queued, actor)
                initial_source = self.assignments(proof['assignment_id']).model_dump(mode='json')
            fresh = (attempt.bootstrap_recovery is None or attempt.bootstrap_recovery.state == 'refused'
                or (attempt.bootstrap_recovery.state == 'created' and self.preflight.recorded_initial_process_refusal(attempt)))
            if ((fresh and source != attempt.thread_id and expected_source_thread_id != source)
                    or (expected_source_thread_id is not None and expected_source_thread_id != source)):
                self.refused('bootstrap_recovery_route_changed', 'Review the current recovery source before creating a bootstrap.')
            if (attempt.bootstrap_recovery is not None and attempt.bootstrap_recovery.state == 'created'
                    and not self.preflight.recorded_initial_process_refusal(attempt)):
                reserved = False
            else:
                attempt, reserved = await asyncio.to_thread(self.preflight.reserve_bootstrap_recovery,
                    attempt_id, actor=actor, expected_snapshot_sha256=digest, reason=reason,
                    source_thread_id=source, expected_replacement_thread_id=attempt.replacement_thread_id)
            expected_thread = None
            if reserved:
                # Reservation survives cancellation/transport errors. A later
                # request only reconciles canonical binding/assignment receipts.
                response = await self.create_reserved(attempt, queued, original_actor, project)
                expected_thread = (response.get('thread') or {}).get('id')
                if not expected_thread:
                    self.refused('bootstrap_recovery_pending', 'Bootstrap response has no thread; reconcile its canonical receipt.')
            binding = await asyncio.to_thread(self.verify_binding, attempt, queued, original_actor,
                expected_thread=expected_thread)
            if attempt.bootstrap_recovery.state != 'created':
                if self.recovery_source(attempt, project=project, actor=actor) != source:
                    self.refused('bootstrap_recovery_route_changed', 'The reserved recovery source changed during creation.')
                attempt = await asyncio.to_thread(self.complete, attempt, queued, binding,
                    actor=actor, project=project, fence_name=name,
                    source_fence_name='thread-queue:' + owned_source if owned_source else None,
                    expected_initial_source=initial_source)
            return {'ok':True, 'threadId':binding.thread_id,
                'bootstrap_created':True, 'native_turn_started':False,
                'attempt':self.preflight.public(attempt, actor=actor)}
        if self.ownership is None:
            return await operation()
        async def own_original():
            acquired, result = await self.ownership.run_exclusive(name, operation)
            if not acquired:
                self.refused('bootstrap_recovery_busy', 'Queue admission is owned elsewhere; recovery is not confirmed.')
            return result
        if self.queue_lock is not None:
            async with self.queue_lock(attempt.thread_id):
                return await own_original()
        return await own_original()
