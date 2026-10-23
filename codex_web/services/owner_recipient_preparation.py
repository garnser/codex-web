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
from codex_web.thread_bootstrap import RecipientBootstrapOrigin


class OwnerRecipientPreparationService:
    def __init__(self, *, routing, threads, existing_assignment, ownership, queue_lock):
        self.routing, self.threads = routing, threads
        self.existing_assignment = existing_assignment
        self.ownership, self.queue_lock = ownership, queue_lock

    def context(self, state, binding, owner, writable, *, discover=True):
        routing = self.routing
        current = routing.get_state(state.ref)
        handoff = current.handoff
        if (handoff is None or handoff.status != 'pending' or handoff.requested_at is None
                or current.ref != state.ref or current.project_id != binding.project_id
                or handoff != state.handoff):
            raise OwnerRoutingError('owner_recipient_preparation_changed')
        routing.require_resolved_originals(current)
        if discover:
            try:
                routing.compatible_pin(current, binding, owner, writable_repository_ids=writable)
            except OwnerRoutingError as exc:
                if str(exc) != 'owner_recipient_native_scope_incompatible':
                    raise
            else:
                raise OwnerRoutingError('owner_recipient_preparation_not_required')
        bootstrap = routing.bootstraps.get_by_thread(binding.thread_id, routing.control_actor())
        assignment = routing.assignments(bootstrap.assignment_id)
        snapshot = routing.snapshot(current)
        canonical_owner = snapshot.owner(normalized_owner(owner))
        profile, actor = routing.executor(current, binding, canonical_owner)
        # This exact-source check retains prior native assignment/scope/tenant
        # validation without rediscovering every unrelated candidate at each
        # provider edge. All authority below is freshly read, never cached.
        routing.native_scope_compatible(current, bootstrap, assignment)
        source = routing.sessions.native_custody(binding.thread_id)
        if source is None:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        # Full source custody and current business intent are rechecked before
        # every provider preparation edge; the token uses the genuine nonce.
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
        return proof, profile, actor

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
        logical = {key: proof[key] for key in ('work_item_ref', 'organization_id', 'workspace_id',
            'project_id', 'owner', 'writable')}
        logical['handoff'] = {key: proof['handoff'][key] for key in
            ('from_agent', 'to_agent', 'requested_at')}
        logical['binding_id'] = binding.id
        token = hashlib.sha256(json.dumps(logical, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()[:32]
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
                if str(exc) != 'owner_recipient_native_scope_incompatible':
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
                    await self.threads.create(project_id=state.project_id,
                        sandbox=proof['assignment']['sandbox'],
                        approval_policy=proof['assignment']['approval_policy'],
                        agent_profile_id=profile.profile_id, agent_profile_revision=profile.revision,
                        execution_profile_id=profile.execution_profile_id,
                        actor=actor, repository_resource_id=writable[0] if len(writable) == 1 else None,
                        writable_repository_resource_ids=writable, read_only_repository_resource_ids=(),
                        bootstrap_token=physical, admission_guard=guard, recipient_origin=origin)
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
