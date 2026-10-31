"""One canonical fresh bootstrap for a scope-incompatible native recipient.

No source session, binding, retained input or replacement graph is rewritten.
The existing bootstrap assignment reserves the provider attempt durably.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from codex_web.owner_routing import OwnerRoutingError, normalized_owner
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
from codex_web.services.owner_routing import _MissingNativePredecessor, _CancelledRecipientContextRequired
from codex_web.thread_bootstrap import RecipientBootstrapOrigin
from codex_web.services.threads import ThreadBootstrapPrestartRefusal, bootstrap_preparation_diagnostic


class OwnerRecipientPreparationService:
    def __init__(self, *, routing, threads, existing_assignment, ownership, queue_lock):
        self.routing, self.threads = routing, threads
        self.existing_assignment = existing_assignment
        self.ownership, self.queue_lock = ownership, queue_lock

    def context(self, state, binding, owner, writable, *, discover=True, lookup_actor=None, originals_checked=False):
        routing = self.routing
        current = routing.get_state(state.ref)
        handoff = current.handoff
        if (handoff is None or handoff.status != 'pending' or handoff.requested_at is None
                or current.ref != state.ref or current.project_id != binding.project_id
                or handoff != state.handoff):
            raise OwnerRoutingError('owner_recipient_preparation_changed')
        if not originals_checked:
            routing.require_resolved_originals(current)
        if discover:
            try:
                routing.compatible_pin(current, binding, owner, writable_repository_ids=writable, lookup_actor=lookup_actor)
            except OwnerRoutingError as exc:
                if not isinstance(exc, _CancelledRecipientContextRequired) and str(exc) != 'owner_recipient_native_scope_incompatible':
                    raise
            else:
                raise OwnerRoutingError('owner_recipient_preparation_not_required')
        bootstrap = routing.bootstraps.get_by_thread(binding.thread_id, (lookup_actor or routing.control_actor)())
        assignment = routing.assignments(bootstrap.assignment_id)
        snapshot = routing.snapshot(current)
        canonical_owner = snapshot.owner(normalized_owner(owner))
        profile, actor = routing.executor(current, binding, canonical_owner, lookup_actor=lookup_actor)
        # This exact-source check retains prior native assignment/scope/tenant
        # validation without rediscovering every unrelated candidate at each
        # provider edge. All authority below is freshly read, never cached.
        cancelled = None
        try:
            compatible = routing.native_scope_compatible(current, bootstrap, assignment)
        except _MissingNativePredecessor:
            compatible = True
            cancelled = routing.cancelled_recipient_source(current, binding, canonical_owner, writable,
                lookup_actor=lookup_actor)
        source = routing.sessions.native_custody(binding.thread_id)
        if (cancelled is None and compatible and str(assignment.status) == 'cancelled'
                and assignment.repository_scope is not None
                and set(assignment.repository_scope.writable_repository_ids) == set(writable)
                and not assignment.repository_scope.read_only_repository_ids):
            cancelled = routing.cancelled_recipient_source(current, binding, canonical_owner, writable,
                lookup_actor=lookup_actor)
        if cancelled is not None:
            bootstrap, assignment, source, original = cancelled
            profile, actor = routing.executor(current, binding, canonical_owner,
                recipient_thread_id=bootstrap.thread_id, lookup_actor=lookup_actor)
        if source is None:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        # Full source custody and current business intent are rechecked before
        # every provider preparation edge; the token uses the genuine nonce.
        proof = self._context_proof(current, binding, owner, writable, bootstrap,
            assignment, source, original if cancelled is not None else None, profile, actor, snapshot)
        return proof, profile, actor

    def _context_proof(self, current, binding, owner, writable, bootstrap,
                       assignment, source, cancelled_source, profile, actor, snapshot):
        """Construct the existing immutable reservation attribution, read only."""
        routing = self.routing
        handoff = current.handoff
        execution_definition = None
        if routing.execution_profiles is not None:
            _, reference = routing.execution_profiles.resolve(profile.execution_profile_id,
                organization_id=current.organization_id, workspace_id=current.workspace_id,
                project_id=current.project_id)
            execution_definition = reference.model_dump(mode='json') if reference is not None else None
        proof = {'work_item_ref': current.ref, 'organization_id': current.organization_id,
            'workspace_id': current.workspace_id, 'project_id': current.project_id,
            'owner': owner, 'binding': binding.model_dump(mode='json'),
            'handoff': handoff.model_dump(mode='json'), 'stage': current.current_stage,
            'retry_attempt': current.execution.retry.attempt,
            'owner_checksum': routing._work_item_owner_checksum(current),
            'writable': sorted(writable), 'resources': sorted(current.resource_ids),
            'source': {key: getattr(source, key) for key in ('id', 'assignment_id',
                'execution_id', 'execution_workspace_id', 'worker_id', 'provider_id', 'runtime_id')},
            'assignment': {key: assignment.model_dump(mode='json').get(key) for key in
                ('id', 'execution_id', 'execution_workspace_id', 'created_by', 'agent_profile',
                 'resource_ids', 'repository_scope', 'sandbox', 'approval_policy', 'fence', 'status',
                 'assigned_worker_id')},
            'profile': profile.model_dump(mode='json'), 'actor_id': actor.identity_id,
            'routing_definition': snapshot.definition.model_dump(mode='json'),
            'execution_definition': execution_definition}
        if cancelled_source is not None:
            proof['cancelled_source'] = cancelled_source
        return proof

    @staticmethod
    def logical_id(state, binding, owner, writable):
        # Same established key: a new context belongs to the genuine Work/nonce
        # and original reply binding, never a cancelled assignment's resume.
        logical = {'work_item_ref': state.ref, 'organization_id': state.organization_id,
            'workspace_id': state.workspace_id, 'project_id': state.project_id,
            'owner': owner, 'writable': sorted(writable),
            'handoff': {key: getattr(state.handoff, key) for key in ('from_agent', 'to_agent', 'requested_at')},
            'binding_id': binding.id}
        return hashlib.sha256(json.dumps(logical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]

    def prepared_binding(self, state, binding, owner, writable, *, lookup_actor=None, originals_checked=False):
        """Read only the existing exact nonce reservation, including typed gen1.

        A provider reply or another matching LIVE candidate is not provenance.
        This performs no reservation, create, recovery or ownership mutation.
        """
        proof, profile, actor = self.context(state, binding, owner, writable,
            discover=False, lookup_actor=lookup_actor, originals_checked=originals_checked)
        if 'cancelled_source' not in proof:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        token = self.logical_id(state, binding, owner, writable)
        def checked(bound, physical):
            assignment = self.routing.assignments(bound.assignment_id)
            if (bound.bootstrap_id != 'bootstrap-' + physical or assignment is None
                    or assignment.execution_id != 'thread-bootstrap-' + physical
                    or bound.execution_id != assignment.execution_id
                    or bound.execution_workspace_id != assignment.execution_workspace_id
                    or assignment.subject is None or assignment.subject.kind != 'thread_bootstrap'
                    or assignment.subject.ref != bound.bootstrap_id
                    or bound.thread_id in {binding.thread_id, proof['cancelled_source']['source_thread_id']}):
                raise OwnerRoutingError('owner_recipient_bootstrap_unverified')
            session = self.routing.sessions.native_custody(bound.thread_id)
            if session is not None and (
                    session.assignment_id != assignment.id or session.execution_id != assignment.execution_id
                    or session.execution_workspace_id != assignment.execution_workspace_id
                    or session.worker_id != assignment.assigned_worker_id
                    or (session.organization_id, session.workspace_id, session.project_id)
                        != (state.organization_id, state.workspace_id, state.project_id)):
                raise OwnerRoutingError('owner_recipient_native_custody_unverified')
            return bound
        try:
            return checked(self.routing.bootstraps.get_by_bootstrap('bootstrap-' + token, actor), token)
        except ThreadBootstrapBindingNotFoundError:
            pass
        previous = self.existing_assignment(execution_id='thread-bootstrap-' + token)
        if previous is None or not self.recoverable(previous, proof, profile, token):
            return None
        physical = self.digest({'logical_id': token, 'predecessor_id': previous.id,
            'predecessor_fence': previous.fence})[:32]
        try:
            return checked(self.routing.bootstraps.get_by_bootstrap('bootstrap-' + physical, actor), physical)
        except ThreadBootstrapBindingNotFoundError:
            return None

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @classmethod
    def attribution(cls, proof):
        # Delivery/error bookkeeping may advance while the genuine intent is
        # unchanged. Actor, Profile, source custody, definitions and scope may not.
        value = {key: item for key, item in proof.items() if key != 'handoff'}
        value['handoff'] = {key: proof['handoff'][key] for key in ('from_agent', 'to_agent', 'requested_at')}
        return cls.digest(value)

    def recoverable(self, assignment, proof, profile, logical_id):
        failure = assignment.failure
        details = failure.details if failure is not None else {}
        scope = assignment.repository_scope
        reference = assignment.execution_profile_definition
        return (str(assignment.status) == 'cancelled' and assignment.fence == 1
            and assignment.lease is None and assignment.assigned_worker_id is None
            and assignment.started_at is None
            and assignment.subject is not None and assignment.subject.kind == 'thread_bootstrap'
            and assignment.subject.ref == 'bootstrap-' + logical_id
            and assignment.execution_id == 'thread-bootstrap-' + logical_id
            and (assignment.organization_id, assignment.workspace_id, assignment.project_id,
                 assignment.created_by) == (proof['organization_id'], proof['workspace_id'],
                 proof['project_id'], proof['assignment']['created_by'])
            and assignment.agent_profile is not None
            and (assignment.agent_profile.profile_id, assignment.agent_profile.profile_revision)
                == (profile.profile_id, profile.revision)
            and assignment.execution_profile_id == profile.execution_profile_id
            and (reference.model_dump(mode='json') if reference is not None else None) == proof['execution_definition']
            and assignment.sandbox == proof['assignment']['sandbox']
            and assignment.approval_policy == proof['assignment']['approval_policy']
            and set(assignment.resource_ids) == set(proof['writable'])
            and scope is not None and set(scope.writable_repository_ids) == set(proof['writable'])
            and not scope.read_only_repository_ids
            and failure is not None and failure.outcome == 'known'
            and failure.source_subsystem == 'thread-bootstrap-admission'
            and failure.assignment_id == assignment.id and failure.execution_id == assignment.execution_id
            and failure.correlation_id == assignment.subject.ref
            and failure.source_native_code == 'before_worker_start'
            and details.get('phase') == 'before_worker_start'
            and details.get('provider_start_called') is False and details.get('worker_start_called') is False
            and type(details.get('reservation_fence')) is int and details.get('reservation_fence') == 0
            and type(details.get('generation')) is int and details.get('generation') == 0
            and details.get('created_by') == assignment.created_by
            and details.get('logical_id') == logical_id
            and details.get('work_item_ref') == proof['work_item_ref']
            and details.get('handoff_requested_at') == proof['handoff']['requested_at']
            and details.get('binding_id') == proof['binding']['id']
            and details.get('origin_actor_id') == proof['actor_id']
            and details.get('attribution_sha256') == self.attribution(proof))

    async def prepare(self, state, binding, owner, *, writable_repository_ids):
        writable = tuple(writable_repository_ids)
        # Only the actual pending handoff has a stable logical invocation.
        # Routine watchdog work without one cannot mint native bootstraps here.
        try:
            proof, profile, actor = await asyncio.to_thread(self.context, state, binding, owner, writable)
        except OwnerRoutingError as exc:
            if str(exc) != 'owner_recipient_preparation_not_required':
                raise
            return await asyncio.to_thread(self.routing.compatible_pin, state, binding, owner,
                writable_repository_ids=writable)
        token = self.logical_id(state, binding, owner, writable)
        name = 'owner-recipient-bootstrap:' + token
        recovery_predecessor = None

        def guard():
            self.ownership.require_fence(name)
            fresh, fresh_profile, _ = self.context(state, binding, owner, writable, discover=False)
            if fresh != proof:
                raise OwnerRoutingError('owner_recipient_preparation_changed')
            if recovery_predecessor is not None:
                current = self.existing_assignment(execution_id='thread-bootstrap-' + token)
                if (current is None or current.id != recovery_predecessor[0]
                        or self.digest(current.model_dump(mode='json')) != recovery_predecessor[1]
                        or not self.recoverable(current, fresh, fresh_profile, token)):
                    raise OwnerRoutingError('owner_recipient_bootstrap_predecessor_changed')

        async def operation():
            nonlocal recovery_predecessor
            # Another legitimate admission may have finished while we waited.
            try:
                return await asyncio.to_thread(self.routing.compatible_pin, state, binding, owner,
                    writable_repository_ids=writable)
            except OwnerRoutingError as exc:
                if not isinstance(exc, _CancelledRecipientContextRequired) and str(exc) != 'owner_recipient_native_scope_incompatible':
                    raise
            await asyncio.to_thread(guard)
            physical = token
            origin = RecipientBootstrapOrigin(token, 0, self.attribution(proof),
                work_item_ref=proof['work_item_ref'], handoff_requested_at=proof['handoff']['requested_at'],
                binding_id=binding.id, actor_id=proof['actor_id'])
            bootstrap_id = 'bootstrap-' + physical
            try:
                bound = await asyncio.to_thread(self.routing.bootstraps.get_by_bootstrap,
                    bootstrap_id, actor)
            except ThreadBootstrapBindingNotFoundError:
                bound = None
            if bound is None:
                existing = await asyncio.to_thread(self.existing_assignment,
                    execution_id='thread-bootstrap-' + token)
                if existing is not None:
                    # A reason string or missing provider receipt is never a
                    # grant. Only the controller's atomic unclaimed refusal can
                    # reserve ONE distinct physical generation for this intent.
                    if not self.recoverable(existing, proof, profile, token):
                        raise OwnerRoutingError('owner_recipient_bootstrap_pending')
                    recovery_predecessor = (existing.id, self.digest(existing.model_dump(mode='json')))
                    physical = self.digest({'logical_id': token, 'predecessor_id': existing.id,
                        'predecessor_fence': existing.fence})[:32]
                    origin = RecipientBootstrapOrigin(token, 1, self.attribution(proof), existing.id,
                        work_item_ref=proof['work_item_ref'], handoff_requested_at=proof['handoff']['requested_at'],
                        binding_id=binding.id, actor_id=proof['actor_id'])
                    bootstrap_id = 'bootstrap-' + physical
                    try:
                        bound = await asyncio.to_thread(self.routing.bootstraps.get_by_bootstrap,
                            bootstrap_id, actor)
                    except ThreadBootstrapBindingNotFoundError:
                        bound = None
                    if bound is None and await asyncio.to_thread(self.existing_assignment,
                            execution_id='thread-bootstrap-' + physical) is not None:
                        raise OwnerRoutingError('owner_recipient_bootstrap_recovery_pending')
                if bound is None:
                    admission_refusal = None
                    def admission_guard():
                        nonlocal admission_refusal
                        try:
                            guard()
                        except OwnerRoutingError as exc:
                            admission_refusal = exc
                            raise
                    try:
                        await self.threads.create(project_id=state.project_id,
                            sandbox=proof['assignment']['sandbox'],
                            approval_policy=proof['assignment']['approval_policy'],
                            agent_profile_id=profile.profile_id, agent_profile_revision=profile.revision,
                            execution_profile_id=profile.execution_profile_id,
                            actor=actor, repository_resource_id=writable[0] if len(writable) == 1 else None,
                            writable_repository_resource_ids=writable, read_only_repository_resource_ids=(),
                            bootstrap_token=physical, admission_guard=admission_guard, recipient_origin=origin)
                    except ThreadBootstrapPrestartRefusal as exc:
                        # Restore only this invocation's actual admission denial.
                        # The generic thread owner strips pre-start authority
                        # after provider startup; adapter lookalikes and unrelated
                        # failures cannot become a canonical routing refusal.
                        if (admission_refusal is not None
                                and exc.__cause__ is admission_refusal
                                and bootstrap_preparation_diagnostic(exc) is not None):
                            raise admission_refusal
                        raise
            # Require the canonical binding, exact assignment and normal LIVE
            # recipient/final native guard. A returned JSON thread ID is not proof.
            bound = await asyncio.to_thread(self.routing.bootstraps.get_by_bootstrap,
                bootstrap_id, actor)
            existing = await asyncio.to_thread(self.routing.assignments, bound.assignment_id)
            if (existing is None or existing.execution_id != 'thread-bootstrap-' + physical
                    or bound.assignment_id != existing.id
                    or bound.thread_id == binding.thread_id):
                raise OwnerRoutingError('owner_recipient_bootstrap_unverified')
            return await asyncio.to_thread(self.routing.compatible_pin, state, binding, owner,
                writable_repository_ids=writable)

        # Local queue lock prevents same-instance reentrant ownership; the
        # existing replicated fence excludes another serving instance.
        async with self.queue_lock(name):
            acquired, result = await self.ownership.run_exclusive(name, operation)
            if not acquired:
                raise OwnerRoutingError('owner_recipient_preparation_busy')
            return result
