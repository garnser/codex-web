from __future__ import annotations

import asyncio

from fastapi import HTTPException

from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.models import QueuedTurn, ExecutionPreflightAttempt
from codex_web.storage.state_store import state_record_storage_key
from codex_web.services.identity import IdentityService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
from codex_web.services.threads import ThreadBootstrapPrestartRefusal


class QueuedBootstrapRecoveryService:
    """Create a fresh, correlated bootstrap without claiming a native Turn."""

    def __init__(self, *, preflight, threads, bootstrap_bindings, assignments,
                 thread_index, actor_resolver, thread_is_active, ownership=None, queue_lock=None):
        self.preflight = preflight
        self.threads = threads
        self.bootstrap_bindings = bootstrap_bindings
        self.assignments = assignments
        self.thread_index = thread_index
        self.actor_resolver = actor_resolver
        self.thread_is_active = thread_is_active
        self.ownership = ownership
        self.queue_lock = queue_lock

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
        if original is None or not original.archived or original.project_id != project.id:
            self.refused('bootstrap_recovery_source_unverified', 'Fresh recovery requires the canonical archived original thread.')
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
                or getattr(assignment.repository_target, 'mutable_repository_id', None) != queued.repository_resource_id):
            self.refused('bootstrap_recovery_binding_mismatch', 'Correlated bootstrap does not match the exact original scope.')
        return binding

    def complete(self, attempt, queued, binding, *, actor, fence_name, source_fence_name=None):
        if self.ownership is not None:
            self.ownership.require_fence(fence_name)
            if source_fence_name:
                self.ownership.require_fence(source_fence_name)
        recovery = attempt.bootstrap_recovery
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
                'preflight_attempt_id':attempt.id, 'requested_by':recovery.requested_by})
        def update(current):
            self.preflight._authorize(current, actor, require_admin=True)
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
        attempt_key = state_record_storage_key(self.preflight.store.NAMESPACE,
            self.preflight.store._attempt_key(attempt.id))
        graph_key = self.thread_index.REPLACEMENTS_NAMESPACE
        def commit(values):
            if self.ownership is not None:
                self.ownership.require_fence(fence_name)
                if source_fence_name:
                    self.ownership.require_fence(source_fence_name)
            current = ExecutionPreflightAttempt.model_validate(values[attempt_key])
            completed = update(current)
            return {attempt_key:completed.model_dump(mode='json'),
                graph_key:graph_updater(values[graph_key])}
        # The fresh edge and its retained admission fence commit together. A
        # late original receipt wins or loses the same row lock, never leaving
        # an edge published after a rejected preflight transition.
        self.preflight.store.store.update_many(
            {attempt_key:None, graph_key:{'version':1, 'edges':{}}}, commit)
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
                bootstrap_token=attempt.bootstrap_recovery.bootstrap_id.removeprefix('bootstrap-'))
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
                    actor=actor, fence_name=name,
                    source_fence_name='thread-queue:' + owned_source if owned_source else None)
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
