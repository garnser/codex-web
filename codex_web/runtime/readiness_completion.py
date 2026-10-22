"""Follow late READY outcomes from existing status and native completion events."""
from __future__ import annotations

import asyncio
import contextvars
import math
import time

from codex_web.action_intents import ActionIntentStatus
from codex_web.models import ActiveThreadTurn
from codex_web.services.post_turn_readiness import PostTurnReadinessService
from codex_web.storage.state_store import storage_operation_budget
from codex_web.procedure_reconciliation import (
    MAX_RECOVERY_ATTEMPTS, current_reconciliation_scope,
)


UNRESOLVED = {ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}


def notify_ready_status(runtime, intent):
    # Notification is a wakeup, never an authority/receipt or a provider retry.
    # A callback's own reconciliation must not recursively schedule itself.
    if (intent.action_id != 'code-host.pull-request.ready'
            or intent.status not in UNRESOLVED or intent.readiness_origin is None
            or intent.attempt < 1 or intent.execution_started_at is None
            or current_reconciliation_scope() is not None
            or runtime.post_turn_readiness is None
            or runtime.readiness_terminal_events is None
            or runtime.readiness_intent_lookup is None):
        return
    loop = runtime._event_loop
    if loop is not None and not loop.is_closed():
        # The original execute operation may have exhausted its own deadline.
        # This separate event receives the existing callback's finite budgets,
        # never an inherited expired request budget or additional authority.
        context = contextvars.copy_context()
        context.run(storage_operation_budget.set, None)
        loop.call_soon_threadsafe(_schedule, runtime, intent, context=context)


def _after_completion(runtime, task, intent):
    # Coalesce only within an existing owned task, bounded by the same origin
    # recovery ceiling. No additional queue, registry or durable grant is made.
    pending = getattr(task, '_ready_status_followups', set())
    if intent.id in pending or len(pending) >= MAX_RECOVERY_ATTEMPTS:
        return
    pending.add(intent.id)
    task._ready_status_followups = pending
    task.add_done_callback(lambda _done: _schedule(runtime, intent))


def _schedule(runtime, intent):
    if getattr(runtime.host, 'IS_SHUTTING_DOWN', False):
        return
    assignment_id = intent.readiness_origin.assignment_id
    for existing in (runtime.assignment_completion_tasks.get(assignment_id),
                     runtime.terminal_recovery_tasks.get(assignment_id)):
        if existing is not None and not existing.done():
            _after_completion(runtime, existing, intent)
            return

    async def follow():
        thread_id = None
        try:
            async def lookup():
                return await asyncio.to_thread(runtime.readiness_intent_lookup, intent.id)
            current = await PostTurnReadinessService._run_owned(lookup,
                seconds=5, name='late-ready-origin-' + intent.id)
            if (current is None or current.status not in UNRESOLVED
                    or current.readiness_origin != intent.readiness_origin
                    or current.attempt != intent.attempt):
                return
            events = await asyncio.wait_for(asyncio.to_thread(
                runtime.readiness_terminal_events, limit=200), timeout=5)
            origin = current.readiness_origin
            matches = [event for event in events
                if event.get('type') in {'thread_bootstrap_turn_completed',
                                        'turn_assignment_completion_failed'}
                and (event.get('assignment_id'), event.get('execution_id'),
                     event.get('worker_id'), event.get('fence')) == (
                    origin.assignment_id, current.execution_id, origin.worker_id, origin.fence)
                and isinstance(event.get('created_at'), (int, float))
                and not isinstance(event['created_at'], bool)
                and math.isfinite(event['created_at'])]
            if not matches:
                return
            event = max(matches, key=lambda row: row['created_at'])
            observed = event['created_at']
            if (not event.get('thread_id') or not event.get('turn_id')
                    or observed < current.execution_started_at
                    or not 0 <= time.time() - observed <= min(current.timeout_seconds, 300)):
                return
            thread_id = event['thread_id']
            notification = getattr(runtime, 'agent_runtime_notification_tasks', {}).get(thread_id)
            if notification is not None and not notification.done():
                _after_completion(runtime, notification, current)
                return
            lock = runtime._turn_start_locks.setdefault(thread_id, asyncio.Lock())
            async with lock:
                completion = runtime.assignment_completion_tasks.get(assignment_id)
                if completion is not None and not completion.done():
                    _after_completion(runtime, completion, current)
                    return
                if runtime.thread_is_active(thread_id) or runtime.thread_handoff_in_progress(thread_id):
                    return
                # The start lock may have been held by another native input.
                # Never reuse this observation for a subsequently changed attempt.
                latest = await PostTurnReadinessService._run_owned(lookup,
                    seconds=5, name='late-ready-current-' + intent.id)
                if (latest is None or latest.status not in UNRESOLVED
                        or latest.readiness_origin != current.readiness_origin
                        or latest.attempt != current.attempt
                        or latest.execution_started_at != current.execution_started_at):
                    return
                bootstrap = await asyncio.to_thread(runtime._bootstrap_binding_for_thread, thread_id)
                if bootstrap is not None and (
                        bootstrap.assignment_id != assignment_id
                        or bootstrap.execution_id != current.execution_id
                        or bootstrap.execution_workspace_id != event.get('execution_workspace_id')):
                    return
                manager, session = await asyncio.to_thread(runtime._session_for_assignment, assignment_id)
                assignment = await asyncio.to_thread(session.validate_current)
                if (assignment.id, assignment.execution_id, assignment.assigned_worker_id,
                        assignment.fence, assignment.execution_workspace_id) != (
                        assignment_id, current.execution_id, origin.worker_id, origin.fence,
                        event.get('execution_workspace_id')):
                    return
                active = ActiveThreadTurn(thread_id=thread_id, turn_id=event['turn_id'],
                    assignment_id=assignment_id, execution_id=current.execution_id,
                    worker_id=origin.worker_id, fence=origin.fence,
                    execution_workspace_id=assignment.execution_workspace_id,
                    started_at=observed, updated_at=observed)
                task = asyncio.current_task()
                runtime.assignment_completion_tasks[assignment_id] = task
                runtime.thread_completion_tasks[thread_id] = task
                # The existing callback re-reads current canonical custody,
                # authority, pause policy and its durable turn recovery marker.
                await runtime._settle_post_turn_readiness(active, manager)
        except Exception as exc:
            runtime.host._append_bot_event({'type': 'post_turn_readiness_deferred',
                'assignment_id': assignment_id, 'intent_id': intent.id,
                'error_type': type(exc).__name__})
        finally:
            task = asyncio.current_task()
            if runtime.assignment_completion_tasks.get(assignment_id) is task:
                runtime.assignment_completion_tasks.pop(assignment_id, None)
            if thread_id and runtime.thread_completion_tasks.get(thread_id) is task:
                runtime.thread_completion_tasks.pop(thread_id, None)
            if runtime.terminal_recovery_tasks.get(assignment_id) is task:
                runtime.terminal_recovery_tasks.pop(assignment_id, None)

    runtime.terminal_recovery_tasks[assignment_id] = asyncio.create_task(
        follow(), name='late-ready-completion-' + assignment_id)
