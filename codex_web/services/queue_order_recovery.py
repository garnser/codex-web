"""Guarded historical ordering repair in the existing queue/preflight domain."""
from __future__ import annotations

import hashlib
import json
import math

from fastapi import HTTPException

from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.models import QueuedTurn
from codex_web.services.identity import IdentityService


class QueueOrderRecoveryService:
    def __init__(self, *, queues, preflight, replacement_resolver, thread_is_active,
                 ownership=None, event_sink=None):
        self.queues = queues
        self.preflight = preflight
        self.replacement_resolver = replacement_resolver
        self.thread_is_active = thread_is_active
        self.ownership = ownership
        self.event_sink = event_sink

    @staticmethod
    def digest(items):
        return hashlib.sha256(json.dumps([item.model_dump(mode='json') for item in items],
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def guard_digest(self, items):
        intents = []
        for item in items:
            attempt = self.preflight.retained_queued(item)
            intents.append(attempt.model_dump(mode='json') if attempt is not None else None)
        return hashlib.sha256(json.dumps({'queue':[item.model_dump(mode='json') for item in items],
            'retained_intents':intents}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def conflict(code, message):
        raise HTTPException(status_code=409, detail={'code':code, 'message':message})

    @staticmethod
    def authorize(actor, project, *, mutate=False):
        if (actor.organization_id, actor.workspace_id) != (project.organization_id, project.workspace_id):
            raise HTTPException(status_code=404, detail='Queue unavailable')
        if mutate:
            try:
                IdentityService.require_admin(actor)
                if actor.principal_kind == PrincipalKind.HUMAN:
                    IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            except RuntimeError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error

    def proposal(self, items, *, thread_id, project, actor):
        self.authorize(actor, project)
        if self.thread_is_active(thread_id):
            self.conflict('queue_order_turn_active', 'Queue order cannot be repaired while a turn is active.')
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids):
            self.conflict('queue_order_duplicate_identity', 'Duplicate request identity prevents order repair.')
        priority = []
        ordinary = []
        for item in items:
            if item.thread_id != thread_id or item.project_id != project.id:
                self.conflict('queue_order_scope_changed', 'A queued request is outside the selected scope.')
            attempt = self.preflight.retained_queued(item)
            if attempt is None or attempt.queued_turn_snapshot is None:
                self.conflict('queue_order_provenance_missing', 'Retained original admission provenance is required.')
            self.preflight._authorize(attempt, actor)
            if attempt.project_id != project.id:
                self.conflict('queue_order_scope_changed', 'Retained Project provenance does not match the queue.')
            if attempt.status in {'admitting', 'retrying'}:
                self.conflict('queue_order_admission_active', 'A live admission or retry claim prevents order repair.')
            if self.thread_is_active(attempt.thread_id):
                self.conflict('queue_order_turn_active', 'The original Thread is still active.')
            if set(attempt.queued_turn_snapshot) - set(QueuedTurn.model_fields):
                self.conflict('queue_order_snapshot_unsupported', 'The retained snapshot requires a compatible reader.')
            try:
                original = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
                resolved = self.replacement_resolver(attempt.thread_id, project_id=attempt.project_id,
                    organization_id=attempt.organization_id, workspace_id=attempt.workspace_id)
            except (RuntimeError, ValueError, KeyError):
                self.conflict('queue_order_route_unverified', 'Retained snapshot or replacement provenance is invalid.')
            target = resolved or attempt.thread_id
            if (attempt.replacement_thread_id != resolved or target != thread_id
                or original.thread_id != attempt.thread_id):
                self.conflict('queue_order_route_unverified', 'The retained replacement route must be verified first.')
            projected = original.model_copy(update={'thread_id':target})
            if original.reply_target and original.reply_target.thread_id == original.thread_id:
                projected = projected.model_copy(update={'reply_target':original.reply_target.model_copy(
                    update={'thread_id':target})})
            expected = projected.model_dump(mode='json')
            current = item.model_dump(mode='json')
            expected.pop('attempts', None)
            current.pop('attempts', None)
            if expected != current or not math.isfinite(original.created_at):
                self.conflict('queue_order_snapshot_changed', 'The complete retained request no longer matches the queue.')
            if attempt.attempt_number > 1:
                priority.append(item)
            else:
                ordinary.append(item)
        # Keep explicit retry priority and its current relative order. Original
        # creation provenance orders only ordinary requests; equal times retain
        # their current stable order rather than inventing a UUID ordering.
        return priority + sorted(ordinary, key=lambda item:item.created_at)

    def preview(self, thread_id, *, project, actor):
        self.authorize(actor, project)
        items = self.queues.get(thread_id)
        result = {'threadId':thread_id, 'snapshot_sha256':self.guard_digest(items),
            'current_order':[item.id for item in items], 'model_calls':0,
            'native_outcomes_changed':False, 'can_repair':False, 'blockers':[]}
        try:
            proposed = self.proposal(items, thread_id=thread_id, project=project, actor=actor)
            result['proposed_order'] = [item.id for item in proposed]
            self.authorize(actor, project, mutate=True)
            result['can_repair'] = result['proposed_order'] != result['current_order']
        except HTTPException as error:
            if error.status_code == 404:
                raise
            result['blockers'] = [error.detail if isinstance(error.detail, dict)
                else {'code':'queue_order_authority_denied', 'message':str(error.detail)}]
        return result

    async def repair(self, thread_id, payload, *, project, actor):
        self.authorize(actor, project, mutate=True)
        name = 'thread-queue:'+thread_id
        async def operation():
            proof = {}
            def apply(items):
                self.authorize(actor, project, mutate=True)
                if self.ownership is not None:
                    self.ownership.require_fence(name)
                if self.guard_digest(items) != payload.expected_snapshot_sha256:
                    self.conflict('queue_order_snapshot_stale', 'Queue changed; inspect a fresh repair preview.')
                proposed = self.proposal(items, thread_id=thread_id, project=project, actor=actor)
                proof.update({'type':'queue_order_repaired', 'thread_id':thread_id,
                    'project_id':project.id, 'organization_id':actor.organization_id,
                    'workspace_id':actor.workspace_id, 'actor_id':actor.identity_id,
                    'reason':payload.reason, 'before_sha256':self.digest(items),
                    'after_sha256':self.digest(proposed), 'current_order':[item.id for item in items],
                    'repaired_order':[item.id for item in proposed], 'native_outcomes_changed':False, 'changed':items != proposed})
                return proposed
            self.queues.update(thread_id, apply)
            audit_recorded = False
            if self.event_sink is not None:
                try:
                    self.event_sink(proof)
                    audit_recorded = True
                except Exception:
                    # The queue CAS already committed. Report that fact without
                    # retrying or claiming an audit receipt that was not stored.
                    audit_recorded = False
            return {'ok':True, 'threadId':thread_id, 'repair':proof, 'model_calls':0,
                'audit_recorded':audit_recorded}
        if self.ownership is None:
            return await operation()
        acquired, result = await self.ownership.run_exclusive(name, operation)
        if not acquired:
            self.conflict('queue_order_ownership_busy', 'Queue admission is owned elsewhere; repair was not confirmed.')
        return result
