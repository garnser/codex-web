"""Black-box manager controls; canonical positive integration is a separate fixture.

Only preparation/current-fence/checkpoint/terminal/physical-stop ports are stubs.
No application bootstrap, storage, process, provider, or network is used here.
"""
import asyncio
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from codex_web.services.agent_process_session import (
    AssignmentBoundAgentProcessSessionManager,
    AssignmentBoundAgentProcessSessionStaleError,
)
from codex_web.storage.action_intents import ReadinessOriginIndexProgressing
from codex_web.storage.state_store import (
    StorageOperationBudget, storage_operation_budget, check_operation_budget,
)


class ManagedCompletionDenialControls(unittest.IsolatedAsyncioTestCase):
    def build(self, prepare):
        assignment = NS(id='private-assignment', assigned_worker_id='private-worker',
                        fence=7, lease=NS(lease_token='private-lease-token-000000000001'),
                        execution_workspace_id='private-workspace')
        calls = []
        def validate():
            calls.append('validate')
            return assignment
        def checkpoint(assignment_id):
            self.assertEqual(assignment_id, assignment.id)
            calls.append('checkpoint')
            return ()
        terminal = NS(status=NS(value='succeeded'), lease=None)
        def complete(worker_id, assignment_id, request, *, actor):
            self.assertEqual((worker_id, assignment_id, request.fence, request.lease_token),
                             ('private-worker', assignment.id, 7, 'private-lease-token-000000000001'))
            self.assertTrue(request.succeeded)
            calls.append('complete')
            return terminal
        service = NS(prepare_completion_index=Mock(side_effect=prepare),
                     complete=Mock(side_effect=complete))
        release = Mock()
        local = NS(worker_service=service, worker_actor=object(), control_actor=object(),
                   checkpoint_assignment=Mock(side_effect=checkpoint),
                   workspace_service=NS(release=release))
        session = NS(worker_id='private-worker', fence=7,
                     validate_current=Mock(side_effect=validate), stop=AsyncMock())
        manager = AssignmentBoundAgentProcessSessionManager(
            local, NS(), runtime_factory=None, credential_provider=None)
        manager.sessions[assignment.id] = session
        return NS(manager=manager, assignment=assignment, service=service,
                  local=local, session=session, release=release, calls=calls, terminal=terminal)

    def retained(self, f):
        f.service.complete.assert_not_called()
        f.local.checkpoint_assignment.assert_not_called()
        f.session.stop.assert_not_awaited()
        f.release.assert_not_called()
        self.assertIs(f.manager.sessions[f.assignment.id], f.session)
        self.assertEqual(f.assignment.lease.lease_token, 'private-lease-token-000000000001')

    async def test_advancing_preparation_then_ready_performs_one_terminal_transition(self):
        steps = iter([ReadinessOriginIndexProgressing('a', 'epoch'),
                      ReadinessOriginIndexProgressing('b', 'epoch'), None])
        def prepare(*args, **kwargs):
            step = next(steps)
            if step is not None:
                raise step
        f = self.build(prepare)
        result = await f.manager.complete(f.assignment.id, succeeded=True)
        self.assertIs(result, f.terminal)
        self.assertEqual(f.service.prepare_completion_index.call_count, 3)
        f.service.complete.assert_called_once()
        f.local.checkpoint_assignment.assert_called_once_with(f.assignment.id)
        f.session.stop.assert_awaited_once()
        f.release.assert_called_once()
        self.assertNotIn(f.assignment.id, f.manager.sessions)
        self.assertLess(f.calls.index('checkpoint'), f.calls.index('complete'))

    async def test_repeated_progress_refuses_without_terminal_or_custody_release(self):
        f = self.build(lambda *a, **k: (_ for _ in ()).throw(
            ReadinessOriginIndexProgressing('same', 'epoch')))
        with self.assertRaises(ReadinessOriginIndexProgressing):
            await f.manager.complete(f.assignment.id, succeeded=True)
        self.assertEqual(f.service.prepare_completion_index.call_count, 2)
        self.retained(f)

    async def test_unavailable_preparation_propagates_original_and_preserves_custody(self):
        error = RuntimeError('private preparation unavailable')
        def prepare(*a, **k):
            raise error
        f = self.build(prepare)
        with self.assertRaises(RuntimeError) as caught:
            await f.manager.complete(f.assignment.id, succeeded=True)
        self.assertIs(caught.exception, error)
        f.service.prepare_completion_index.assert_called_once()
        self.retained(f)

    async def test_advancing_forever_exhausts_finite_budget_without_terminal(self):
        progress = iter(str(i) for i in range(100))
        def prepare(*a, **k):
            raise ReadinessOriginIndexProgressing(next(progress), 'epoch')
        f = self.build(prepare)
        with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
            await f.manager.complete(f.assignment.id, succeeded=True)
        self.assertGreater(f.service.prepare_completion_index.call_count, 1)
        self.assertLessEqual(f.service.prepare_completion_index.call_count, 8)
        self.retained(f)

    async def test_changed_fence_between_preparation_steps_prevents_next_step(self):
        f = None
        def prepare(*a, **k):
            f.assignment.fence = 8
            raise ReadinessOriginIndexProgressing('advanced', 'epoch')
        f = self.build(prepare)
        with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
            await f.manager.complete(f.assignment.id, succeeded=True)
        f.service.prepare_completion_index.assert_called_once()
        self.retained(f)

    async def test_cancel_blocked_preparation_joins_original_without_next_step(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        observed = {}
        def prepare(*a, **k):
            observed['budget'] = storage_operation_budget.get()
            entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError('private test must release original preparation')
                raise ReadinessOriginIndexProgressing('a', 'epoch')
            finally:
                finished.set()
        f = self.build(prepare)
        task = asyncio.create_task(f.manager.complete(f.assignment.id, succeeded=True))
        try:
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3))
            task.cancel()
            self.assertTrue(await asyncio.wait_for(
                asyncio.to_thread(observed['budget'].cancelled.wait, 2), 3))
            self.assertFalse(task.done(), 'cancelled caller must still own blocked preparation')
            self.assertFalse(finished.is_set())
            self.retained(f)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 3)
            self.assertTrue(finished.is_set())
            f.service.prepare_completion_index.assert_called_once()
            self.retained(f)
        finally:
            release.set()
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 6)

    async def test_parent_retirement_refuses_guarded_write_and_joins_without_task_cancel(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        writes = []
        def prepare(*a, **k):
            entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError('private test must release original preparation')
                check_operation_budget()
                writes.append('guarded-write')
            finally:
                finished.set()
        f = self.build(prepare)
        parent = StorageOperationBudget(20)
        token = storage_operation_budget.set(parent)
        task = asyncio.create_task(f.manager.complete(f.assignment.id, succeeded=True))
        error = None
        try:
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3))
            parent.cancelled.set()
            self.assertFalse(task.cancelling(), 'parent retirement is not task cancellation')
            self.assertFalse(finished.is_set())
            release.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), 3)
            except TimeoutError as exc:
                error = exc
            self.assertTrue(finished.is_set(), 'original preparation must be positively joined')
            self.assertEqual(writes, [], 'retired parent must prevent the guarded write')
            self.assertIsInstance(error, TimeoutError)
            self.assertTrue(task.done())
            self.assertFalse(task.cancelled())
            f.service.prepare_completion_index.assert_called_once()
            self.retained(f)
        finally:
            release.set()
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 6)
            storage_operation_budget.reset(token)
