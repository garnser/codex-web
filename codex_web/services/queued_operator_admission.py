"""One explicitly reviewed original through the existing owned native drain."""
from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import HTTPException

from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel, AuthorityDecisionOutcome
from codex_web.coordination import CoordinationFenceError
from codex_web.identity import AuthenticationAssurance
from codex_web.models import QueuedTurn, QueuedOperatorAdmission
from codex_web.services.identity import IdentityService
from codex_web.services.queue_order_recovery import QueueOrderRecoveryService


class QueuedOperatorAdmissionService:
    def __init__(self, *, preflight, runtime, ownership, authority, resources,
                 actor_resolver, replacement_resolver, clock=time.time):
        self.preflight, self.runtime, self.ownership = preflight, runtime, ownership
        self.authority, self.resources = authority, resources
        self.actor_resolver, self.replacement_resolver, self.clock = actor_resolver, replacement_resolver, clock

    @staticmethod
    def refuse(message):
        raise HTTPException(status_code=409, detail={
            'code':'operator_admission_refused', 'message':message, 'retryable':False})

    def authorize(self, attempt, project, actor):
        self.preflight._authorize(attempt, actor, require_admin=True)
        if (attempt.project_id, attempt.organization_id, attempt.workspace_id) != (
                project.id, project.organization_id, project.workspace_id):
            raise HTTPException(status_code=404, detail='Retained request unavailable')
        try:
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            fresh = self.actor_resolver(actor.identity_id, project)
            IdentityService.require_admin(fresh)
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if (fresh.identity_id, fresh.organization_id, fresh.workspace_id) != (
                actor.identity_id, actor.organization_id, actor.workspace_id):
            raise HTTPException(status_code=403, detail='Operator identity scope changed')
        targets = tuple(dict.fromkeys((*attempt.writable_repository_resource_ids,
            *attempt.read_only_repository_resource_ids,
            *((attempt.repository_resource_id,) if attempt.repository_resource_id else ()))))
        if not set(targets).issubset(self.resources.resource_ids_for_project(project)):
            self.refuse('Original resources are no longer bound to this Project.')
        decision = self.authority.evaluate(AuthorityEvaluationRequest(
            capability='execution.retry', level=AuthorityLevel.EXECUTE,
            project_id=project.id, resource_ids=targets), actor=fresh)
        if decision.outcome != AuthorityDecisionOutcome.ALLOW:
            raise HTTPException(status_code=403, detail='Current Role authority denies original admission')

    def context(self, attempt_id, *, thread_id, project, actor):
        attempt = self.preflight.get(attempt_id, actor=actor)
        self.authorize(attempt, project, actor)
        self.preflight._require_individual_recovery(attempt)
        if (attempt.status != 'queued' or attempt.queued_turn_snapshot is None
                or attempt.queued_start_receipt is not None or attempt.started_at is not None
                or attempt.scheduling_retirement is not None
                or (attempt.bootstrap_recovery is not None and attempt.bootstrap_recovery.state != 'created')):
            self.refuse('Only a retained, queued, proven-unstarted original can be admitted.')
        try:
            original = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        except (ValueError, TypeError):
            self.refuse('The retained original requires a compatible, complete snapshot.')
        fields = ('thread_id', 'project_id', 'message', 'source', 'sandbox', 'approval_policy',
            'model', 'reasoning_effort', 'repository_resource_id', 'writable_repository_resource_ids',
            'read_only_repository_resource_ids', 'execution_profile_id', 'agent_profile_id',
            'agent_profile_revision', 'agent_profile_actor_id')
        if any(getattr(original, field) != getattr(attempt, field) for field in fields):
            self.refuse('Retained admission metadata differs from its immutable original.')
        if self.runtime.automation_wait_reason(original.source) is None:
            self.refuse('Automation is not paused for this original. Use ordinary queue admission.')
        target = self.replacement_resolver(attempt.thread_id, project_id=project.id,
            organization_id=project.organization_id, workspace_id=project.workspace_id)
        if attempt.replacement_thread_id != target or (target or attempt.thread_id) != thread_id:
            self.refuse('The original replacement route must match the selected Thread.')
        if self.runtime.thread_is_active(thread_id) or self.runtime.thread_is_active(attempt.thread_id):
            self.refuse('A native turn already owns the selected Thread.')
        if not original.agent_profile_id or original.agent_profile_revision is None or not original.agent_profile_actor_id:
            self.refuse('Exact original actor and profile revision are required.')
        projected = original.model_copy(deep=True, update={'thread_id':thread_id})
        if original.reply_target and original.reply_target.thread_id == original.thread_id:
            projected.reply_target = original.reply_target.model_copy(update={'thread_id':thread_id})
        items = self.runtime.host._thread_queue(thread_id)
        matches = [item for item in items if item.id == original.id]
        if len(matches) != 1:
            self.refuse('Exactly one original queue copy is required.')
        expected, observed = projected.model_dump(mode='json'), matches[0].model_dump(mode='json')
        # Only the canonical queue retry counter may differ from the immutable original.
        expected.pop('attempts'); observed.pop('attempts')
        if expected != observed:
            self.refuse('The queued copy differs from its retained original.')
        return attempt, matches[0], QueueOrderRecoveryService.digest(items)

    def preview(self, attempt_id, *, thread_id, project, actor):
        attempt, queued, digest = self.context(attempt_id, thread_id=thread_id, project=project, actor=actor)
        return {'attempt_id':attempt.id, 'queued_turn_id':queued.id, 'thread_id':thread_id,
            'project_id':project.id, 'source':queued.source,
            'original_actor_id':queued.agent_profile_actor_id,
            'agent_profile_id':queued.agent_profile_id, 'agent_profile_revision':queued.agent_profile_revision,
            'execution_profile_id':queued.execution_profile_id,
            'writable_repository_resource_ids':list(queued.writable_repository_resource_ids),
            'read_only_repository_resource_ids':list(queued.read_only_repository_resource_ids),
            'reply_target':queued.reply_target.model_dump(mode='json') if queued.reply_target else None,
            'snapshot_sha256':self.preflight.queued_snapshot_digest(attempt.queued_turn_snapshot),
            'queue_sha256':digest, 'automation_will_resume':False}

    async def admit(self, attempt_id, payload, *, thread_id, project, actor):
        if self.ownership is None:
            self.refuse('Canonical queue ownership is unavailable.')
        reason = payload.reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail='A reviewed admission reason is required')
        name = f'thread-queue:{thread_id}'
        async def operation():
            self.ownership.require_fence(name)
            attempt, queued, queue_digest = self.context(attempt_id,
                thread_id=thread_id, project=project, actor=actor)
            snapshot_digest = self.preflight.queued_snapshot_digest(attempt.queued_turn_snapshot)
            if snapshot_digest != payload.expected_snapshot_sha256 or queue_digest != payload.expected_queue_sha256:
                self.refuse('The reviewed original or queue has changed. Inspect it again.')
            claim = 'operator-admission-' + uuid.uuid4().hex
            proof = QueuedOperatorAdmission(claim_id=claim, requested_by=actor.identity_id,
                requested_at=self.clock(), reason=reason, snapshot_sha256=snapshot_digest,
                queue_sha256=queue_digest)
            def reserve(current):
                self.authorize(current, project, actor)
                self.preflight._require_individual_recovery(current)
                if (current.status != 'queued' or current.queued_start_receipt is not None
                        or current.started_at is not None or current.scheduling_retirement is not None
                        or current.model_dump(mode='json') != attempt.model_dump(mode='json')):
                    self.refuse('Original admission state changed during review.')
                return current.model_copy(update={'status':'admitting',
                    'queued_admission_claim_id':claim, 'operator_admission':proof, 'updated_at':self.clock()})
            reserved = await asyncio.to_thread(self.preflight.store.update, attempt_id, reserve)
            def validate():
                try:
                    self.ownership.require_fence(name)
                except CoordinationFenceError:
                    self.refuse('Queue ownership was lost before native input delivery.')
                if self.runtime.automation_wait_reason(queued.source) is None:
                    self.refuse('The reviewed automation pause changed before native input delivery.')
                current = self.preflight.store.get(attempt_id)
                if (current is None or current.status not in {'admitting','reconciliation_required'}
                        or current.queued_admission_claim_id != claim or current.operator_admission != proof
                        or current.notification_batch_id is not None or current.scheduling_retirement is not None
                        or current.queued_start_receipt is not None or current.started_at is not None
                        or current.queued_turn_snapshot != attempt.queued_turn_snapshot
                        or current.replacement_thread_id != attempt.replacement_thread_id
                        or self.clock() - proof.requested_at > 120):
                    self.refuse('Reviewed admission is stale or has already been consumed.')
                try:
                    self.authorize(current, project, actor)
                except HTTPException as exc:
                    self.refuse('Current operator authority no longer permits this admission.')
            try:
                await self.runtime._drain_thread_queue_owned(thread_id,
                    operator_original=(queued, reserved, claim), operator_admission_validator=validate)
            finally:
                current = self.preflight.store.get(attempt_id)
                if current.status == 'admitting' and current.queued_admission_claim_id == claim:
                    self.preflight.queued_disposition(attempt_id, status='queued', claim_id=claim)
            current = self.preflight.get(attempt_id, actor=actor)
            return {'ok':current.queued_start_receipt is not None, 'threadId':thread_id,
                'automationResumed':False, 'attempt':self.preflight.public(current, actor=actor)}
        lock = self.runtime.queue_admission_lock(thread_id)
        if lock.locked():
            self.refuse('Another local admission owns this queue. Inspect the outcome before another action.')
        async with lock:
            acquired, result = await self.ownership.run_exclusive(name, operation)
            if not acquired:
                self.refuse('Queue ownership was unavailable or lost. Inspect the original outcome before another action.')
            return result
