"""Canonical Goal reads must leave the application loop available for custody."""
import asyncio
import threading
import unittest

from codex_web.execution_workers import AssignmentRenewRequest, WorkerHeartbeatRequest
from codex_web.goals import GoalUpdate
from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
from codex_web.services.goal_execution_bindings import GoalExecutionBindingConflictError
from tests import test_project_goal_worker_admission as fixture


class GoalPreclaimPreparationTests(fixture.ProjectGoalWorkerAdmissionTests):
    async def test_canonical_preclaim_reads_allow_actual_heartbeat_and_renewal(self):
        loop = asyncio.get_running_loop()
        loop_thread = threading.current_thread()
        entered = asyncio.Event()
        held, released, selected = threading.Event(), threading.Event(), threading.Event()
        loop_reads, renewals, progressed_while_held, before_claim = [], [], [], []
        original = self.goals.store.load

        def load(*args, **kwargs):
            if threading.current_thread() is loop_thread:
                loop_reads.append(True)
            if not selected.is_set():
                selected.set()
                binding = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
                before_claim.append(binding.lease_owner_id is None and binding.project_continuation is None)
                # On the unchanged baseline, record the actual loop-thread
                # read without blocking that loop forever. On the candidate,
                # hold the same real read until actual custody work finishes.
                if threading.current_thread() is not loop_thread:
                    held.set()
                loop.call_soon_threadsafe(entered.set)
                if held.is_set():
                    released.wait()
            return original(*args, **kwargs)

        def renew():
            worker = self.f.workers.store.worker(self.target.assigned_worker_id)
            self.f.workers.heartbeat(worker.id, WorkerHeartbeatRequest(version=worker.version),
                actor=self.worker_actor)
            current = self.f.workers.store.assignment(self.target.id)
            return self.f.workers.renew(worker.id, current.id, AssignmentRenewRequest(
                lease_token=current.lease.lease_token, fence=current.fence, lease_seconds=120),
                actor=self.worker_actor)

        async def heartbeat():
            await entered.wait()
            try:
                renewals.append(await asyncio.to_thread(renew))
                progressed_while_held.append(held.is_set() and not released.is_set())
            finally:
                released.set()

        self.goals.store.load = load
        pulse = asyncio.create_task(heartbeat())
        dispatch = asyncio.create_task(self.goal_continuation.dispatch_once(
            self.goal_binding.id, scope=self.actor.tenant))
        try:
            result, _ = await asyncio.gather(dispatch, pulse)
        finally:
            released.set()
            await asyncio.gather(dispatch, pulse, return_exceptions=True)
            self.goals.store.load = original
        self.assertEqual(before_claim, [True], 'gate must observe real Goal metadata before any continuation claim')
        self.assertEqual(loop_reads, [], 'canonical pre-claim Goal reads blocked the application loop')
        self.assertEqual(progressed_while_held, [True])
        self.assertEqual(len(renewals), 1)
        self.assertEqual(renewals[0].fence, self.target.fence)
        self.assertEqual(renewals[0].lease.lease_token, self.target.lease.lease_token)
        self.assertGreater(renewals[0].lease.expires_at, self.target.lease.expires_at)
        self.assertEqual(result.outcome, 'queued', result.reason)
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)
        self.assertEqual(self.starts(), [])

    async def held_snapshot(self, change=None, *, cancel=False):
        """Hold the actual second Goal load, after the first policy read."""
        loop = asyncio.get_running_loop()
        entered, released = asyncio.Event(), threading.Event()
        original = self.goals.store.load
        reads = 0

        def load(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                loop.call_soon_threadsafe(entered.set)
                released.wait()
            return original(*args, **kwargs)

        self.goals.store.load = load
        dispatch = asyncio.create_task(self.goal_continuation.dispatch_once(
            self.goal_binding.id, scope=self.actor.tenant))
        try:
            await entered.wait()
            if change is not None:
                await asyncio.to_thread(change)
            if cancel:
                dispatch.cancel()
                checkpoint = loop.create_future()
                loop.call_soon(checkpoint.set_result, None)
                await checkpoint
                self.assertFalse(dispatch.done(), 'cancellation must join the held real read')
            released.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await dispatch
                return None
            return await dispatch
        finally:
            released.set()
            await asyncio.gather(dispatch, return_exceptions=True)
            self.goals.store.load = original

    def assert_no_delivery(self):
        self.assertEqual(len(self.queue_store.get(self.thread)), 0)
        self.assertEqual(self.starts(), [])
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertIsNone(current.lease_owner_id)
        self.assertIsNone(current.project_continuation)
        return current

    async def test_operator_pause_during_metadata_is_not_overwritten_or_claimed(self):
        paused = []
        result = await self.held_snapshot(lambda: paused.append(self.goal_bindings.operator_stop(
            self.goal_binding.id, scope=self.actor.tenant, actor_id=self.actor.identity_id,
            cancelled=False, reason='Actual operator pause while metadata is held')))
        self.assertEqual(result.outcome, 'not_claimed')
        self.assertEqual(self.assert_no_delivery(), paused[0])

    async def test_goal_revision_during_metadata_refuses_before_claim(self):
        result = await self.held_snapshot(lambda: self.goals.revise(self.goal.id,
            GoalUpdate(owner_identity_id=self.worker_actor.identity_id, reason='Actual owner revision'),
            scope=self.actor.tenant, actor_id=self.actor.identity_id))
        self.assertEqual(result.reason, 'goal_revision_changed')
        self.assertEqual(self.assert_no_delivery().status, 'blocked')

    async def test_exclusive_scope_change_during_metadata_refuses_before_claim(self):
        def change():
            exclusive = self.control.exclusive_goal_scope.model_copy(update={'project_id': 'home'})
            self.control = self.control.model_copy(update={'exclusive_goal_scope': exclusive})
        result = await self.held_snapshot(change)
        self.assertEqual(result.reason, 'exclusive_goal_scope_project:home')
        self.assertEqual(self.assert_no_delivery().status, 'blocked')

    async def test_current_session_authority_change_still_refuses_at_admission(self):
        def change():
            session = self.sessions.get(self.goal_binding.agent_session_id, self.actor)
            self.sessions.store.upsert(session.model_copy(update={'project_id': 'home'}))
        result = await self.held_snapshot(change)
        self.assertNotEqual(result.outcome, 'queued')
        self.assertEqual(len(self.queue_store.get(self.thread)), 0)
        self.assertEqual(self.starts(), [])

    async def test_cancelled_metadata_read_is_joined_without_late_claim(self):
        before = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        await self.held_snapshot(cancel=True)
        self.assertEqual(self.assert_no_delivery(), before)

    async def test_canonical_binding_cas_preserves_operator_pause_and_event(self):
        prepared = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        paused = self.goal_bindings.operator_stop(prepared.id, scope=self.actor.tenant,
            actor_id=self.actor.identity_id, cancelled=False, reason='Pause after metadata preparation')
        paused_state = self.goal_bindings.store.load()
        with self.assertRaisesRegex(GoalExecutionBindingConflictError, 'binding changed'):
            self.goal_bindings.update(prepared.id, GoalExecutionBindingUpdate(
                status='completed', stop_reason='Stale prepared completion', reason='Stale completion'),
                scope=self.actor.tenant, actor_id=self.goal_continuation.owner_id,
                application_only=True, expected_binding=prepared)
        self.assertEqual(self.goal_bindings.store.load(), paused_state)
        claimed = self.goal_bindings.claim_continuation(prepared.id, scope=self.actor.tenant,
            owner_id=self.goal_continuation.owner_id, lease_seconds=60, expected_binding=prepared)
        self.assertIsNone(claimed)
        self.assertEqual(self.goal_bindings.store.load(), paused_state)
        self.assertEqual(self.assert_no_delivery(), paused)


def load_tests(loader, standard_tests, pattern):
    # Imported fixture methods are prerequisites, not another collected suite.
    return unittest.TestSuite(GoalPreclaimPreparationTests(name)
        for name in GoalPreclaimPreparationTests.__dict__ if name.startswith('test_'))
