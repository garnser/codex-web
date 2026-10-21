"""Finite same-origin READY settlement following a canonical terminal turn."""
from __future__ import annotations

import asyncio
import hashlib
import weakref
from dataclasses import replace
from types import SimpleNamespace
import time

from codex_web.action_intents import ActionIntentReconcileRequest, ActionIntentStatus
from codex_web.procedure_reconciliation import (
    MAX_RECOVERY_ATTEMPTS, read_only_reconciliation,
)
from codex_web.services.action_providers import ActionRequirementError
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget
from codex_web.storage.action_intents import ReadinessOriginIndexProgressing


def require_post_turn_admission(assignment, intent, actor, *, controller,
        identity, runtime_enabled, gates):
    """Use existing global/scoped autonomy and queue-recovery pause controls."""
    requester = identity.actor_for_identity(intent.requested_by, scope=actor.tenant)
    control = controller.store.control()
    resources = tuple(dict.fromkeys((*assignment.resource_ids, *intent.resource_ids)))
    event = SimpleNamespace(payload={'goal_id': intent.goal_id,
        'project_id': intent.project_id, 'work_item_ref': intent.work_item_ref})
    if (not runtime_enabled() or control.mode != 'active'
            or control.dry_run or control.simulation
            or str(control.policy.level) in {'observe', 'recommend', 'prepare'}
            or controller._scoped_pause_reason(control, actor=requester,
                project_id=intent.project_id, resource_ids=resources)
            or controller._exclusive_goal_scope_reason(control, event)):
        raise ActionRequirementError('Automatic post-turn readiness is paused')
    if controller.policy is not None:
        effective, _ = controller.policy.effective(actor=requester,
            project_id=intent.project_id, action_id=intent.action_id, now=time.time())
        if str(effective.level) in {'observe', 'recommend', 'prepare'}:
            raise ActionRequirementError('Scoped policy does not admit post-turn readiness')
    if (not intent.project_id or not gates.decision('queue-recovery',
            intent.project_id, actor=requester).eligible):
        raise ActionRequirementError('Project queue recovery is paused')


class PostTurnReadinessService:
    """Reuse worker identity, ActionIntents and their append-only recovery reserve.

    This engine creates neither a native/model task nor a credential/provider
    write. A stored marker only prevents duplicate callbacks; current canonical
    custody and authority remain mandatory for every new attempt.
    """

    def __init__(self, intents, *, admission, ownership=None, append_event=None):
        self.intents = intents
        self.admission = admission
        self.ownership = ownership
        self.append_event = append_event or (lambda _event: None)
        self._locks = weakref.WeakValueDictionary()

    async def settle(self, active, *, actor):
        if not active.assignment_id or not active.turn_id:
            return False
        key = active.assignment_id
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            async def operation():
                return await self._settle_current(active, actor)
            if self.ownership is not None:
                owned, result = await self.ownership.run_exclusive(
                    'post-turn-readiness:' + key, operation)
                return bool(owned and result)
            return await operation()

    async def _settle_current(self, active, actor):
        workers = self.intents.execution_workers
        current = await asyncio.to_thread(workers.store.assignment, active.assignment_id)
        if current is None:
            return False
        if ((current.id, current.execution_id, current.assigned_worker_id, current.fence)
                != (active.assignment_id, active.execution_id, active.worker_id, active.fence)):
            raise ActionRequirementError('Post-turn readiness custody changed')
        self.intents._require_worker(actor)
        await asyncio.to_thread(self.intents.identity.actor_for_identity,
            actor.identity_id, scope=actor.tenant)
        candidates = await self._prepare_candidates(current.id)
        settled = False
        # The canonical reserve already limits all attempts for this origin;
        # also bound selection before any provider access.
        for intent in sorted(candidates, key=lambda row: (row.created_at, row.id))[:MAX_RECOVERY_ATTEMPTS]:
            if intent.status not in {
                    ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}:
                continue
            payload = ActionIntentReconcileRequest(retry_if_idempotent=False)
            scope = await asyncio.to_thread(self.intents.read_only_reconciliation_scope,
                intent, payload, actor, assignment=current, include_normal_budget=True)
            if scope is None:
                continue
            # Native terminal identity is supplied only by the runtime engine.
            # Re-delivery/restart cannot spend another reserve on the same turn.
            identity = '\0'.join((current.id, str(current.fence), active.turn_id, intent.id))
            scope = replace(scope, id=hashlib.sha256(identity.encode()).hexdigest()[:32])
            current = await asyncio.to_thread(workers.store.assignment, current.id)
            if any(scope.marker in row.supporting_sources
                    for row in current.procedure_discoveries):
                continue
            # Pause applies to admission. Like other already-admitted finite
            # worker operations, an in-flight read-only callback may finish.
            await asyncio.to_thread(self.admission, current, intent, actor)
            self.append_event({'type': 'post_turn_readiness_started',
                'assignment_id': current.id, 'turn_id': active.turn_id, 'intent_id': intent.id})
            try:
                result = await self._reconcile_owned(intent, payload, actor, scope)
                settled = settled or result.status == ActionIntentStatus.SUCCEEDED
                self.append_event({'type': 'post_turn_readiness_completed',
                    'assignment_id': current.id, 'turn_id': active.turn_id,
                    'intent_id': intent.id, 'status': str(result.status)})
            except Exception as exc:
                self.append_event({'type': 'post_turn_readiness_blocked',
                    'assignment_id': current.id, 'turn_id': active.turn_id,
                    'intent_id': intent.id, 'error_type': type(exc).__name__})
                raise
        return settled

    async def _prepare_candidates(self, assignment_id):
        # Continue only an explicitly typed bounded migration with a changing
        # canonical cursor. No generic retirement/authority error is retried.
        async def prepare():
            seen = set()
            for _ in range(8):
                try:
                    return await asyncio.to_thread(
                        self.intents.store.unsettled_readiness_origin, assignment_id)
                except ReadinessOriginIndexProgressing as exc:
                    progress = (exc.epoch, exc.cursor)
                    if progress in seen:
                        raise
                    seen.add(progress)
            raise ActionRequirementError('Post-turn readiness index preparation budget exhausted')
        return await self._run_owned(prepare, seconds=30,
            name='ready-origin-preparation-' + assignment_id)

    async def _reconcile_owned(self, intent, payload, actor, scope):
        async def operation():
            with read_only_reconciliation(scope):
                return await self.intents.reconcile(intent.id, payload, actor=actor)
        return await self._run_owned(operation, seconds=intent.timeout_seconds,
            name='ready-settlement-' + intent.id)

    async def _run_owned(self, operation, *, seconds, name):
        """A cancelled waiter cannot retire a still-running canonical writer."""
        parent_budget = storage_operation_budget.get()
        seconds = min(seconds, parent_budget.remaining()
            if parent_budget is not None else seconds)
        budget = StorageOperationBudget(seconds)

        async def owned_operation():
            token = storage_operation_budget.set(budget)
            try:
                return await operation()
            finally:
                storage_operation_budget.reset(token)

        task = asyncio.create_task(owned_operation(), name=name)
        try:
            done, _ = await asyncio.wait((task,), timeout=seconds)
            if not done:
                raise TimeoutError('Post-turn readiness operation deadline exceeded')
            return task.result()
        except BaseException:
            # Existing store budgets follow asyncio.to_thread and check again
            # before canonical commits. Stop subsequent operations, then join
            # the actual owner; do not cancel its asyncio waiter and abandon a
            # synchronous database writer behind a released admission barrier.
            budget.cancelled.set()
            while not task.done():
                try:
                    await asyncio.wait((task,))
                except asyncio.CancelledError:
                    budget.cancelled.set()
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()  # Observe the real terminal outcome.
            raise
