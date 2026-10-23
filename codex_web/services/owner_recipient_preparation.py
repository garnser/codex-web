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


class OwnerRecipientPreparationService:
    def __init__(self, *, routing, threads, existing_assignment, ownership, queue_lock):
        self.routing, self.threads = routing, threads
        self.existing_assignment = existing_assignment
        self.ownership, self.queue_lock = ownership, queue_lock

    def context(self, state, binding, owner, writable):
        routing = self.routing
        current = routing.get_state(state.ref)
        handoff = current.handoff
        if (handoff is None or handoff.status != 'pending' or handoff.requested_at is None
                or current.ref != state.ref or current.project_id != binding.project_id
                or handoff != state.handoff):
            raise OwnerRoutingError('owner_recipient_preparation_changed')
        routing.require_resolved_originals(current)
        try:
            routing.compatible_pin(current, binding, owner, writable_repository_ids=writable)
        except OwnerRoutingError as exc:
            if str(exc) != 'owner_recipient_native_scope_incompatible':
                raise
        else:
            raise OwnerRoutingError('owner_recipient_preparation_not_required')
        bootstrap = routing.bootstraps.get_by_thread(binding.thread_id, routing.control_actor())
        assignment = routing.assignments(bootstrap.assignment_id)
        canonical_owner = routing.snapshot(current).owner(normalized_owner(owner))
        profile, actor = routing.executor(current, binding, canonical_owner)
        source = routing.sessions.native_custody(binding.thread_id)
        if source is None:
            raise OwnerRoutingError('owner_recipient_native_custody_unverified')
        # Full source custody and current business intent are rechecked before
        # every provider preparation edge; the token uses the genuine nonce.
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
            'profile': profile.model_dump(mode='json'), 'actor_id': actor.identity_id}
        return proof, profile, actor

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

        def guard():
            self.ownership.require_fence(name)
            fresh, _, _ = self.context(state, binding, owner, writable)
            if fresh != proof:
                raise OwnerRoutingError('owner_recipient_preparation_changed')

        async def operation():
            # Another legitimate admission may have finished while we waited.
            try:
                return await asyncio.to_thread(self.routing.compatible_pin, state, binding, owner,
                    writable_repository_ids=writable)
            except OwnerRoutingError as exc:
                if str(exc) != 'owner_recipient_native_scope_incompatible':
                    raise
            await asyncio.to_thread(guard)
            bootstrap_id = 'bootstrap-' + token
            try:
                bound = await asyncio.to_thread(self.routing.bootstraps.get_by_bootstrap,
                    bootstrap_id, actor)
            except ThreadBootstrapBindingNotFoundError:
                bound = None
            if bound is None:
                existing = await asyncio.to_thread(self.existing_assignment,
                    execution_id='thread-bootstrap-' + token)
                if existing is not None:
                    # The original provider call may have happened. A missing
                    # positive receipt never permits another create/start.
                    raise OwnerRoutingError('owner_recipient_bootstrap_pending')
                await self.threads.create(project_id=state.project_id,
                    sandbox=proof['assignment']['sandbox'],
                    approval_policy=proof['assignment']['approval_policy'],
                    agent_profile_id=profile.profile_id, agent_profile_revision=profile.revision,
                    execution_profile_id=profile.execution_profile_id,
                    actor=actor, repository_resource_id=writable[0] if len(writable) == 1 else None,
                    writable_repository_resource_ids=writable, read_only_repository_resource_ids=(),
                    bootstrap_token=token, admission_guard=guard)
            # Require the canonical binding, exact assignment and normal LIVE
            # recipient/final native guard. A returned JSON thread ID is not proof.
            bound = await asyncio.to_thread(self.routing.bootstraps.get_by_bootstrap,
                bootstrap_id, actor)
            existing = await asyncio.to_thread(self.routing.assignments, bound.assignment_id)
            if (existing is None or existing.execution_id != 'thread-bootstrap-' + token
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
