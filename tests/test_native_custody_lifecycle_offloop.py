"""Actual terminal, cleanup and renewal paths retain owned off-loop validation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.agent_runtime import AgentRuntimeResult
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import ActiveThreadTurn
from tests import test_turn_execution as turns
from tests import test_procedure_assignment_renewal as renewal


class NativeCustodyLifecycleOffloopTests(unittest.IsolatedAsyncioTestCase):
    def gate(self, session, *, at_call=1):
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='private-custody-validation')
        session._supervision_executor = executor
        session._executor_work = set()
        entered, release = asyncio.Event(), threading.Event()
        violations, order = [], []
        original = session.validate_current
        calls = 0
        def validate():
            nonlocal calls
            calls += 1
            if calls == at_call:
                loop.call_soon_threadsafe(entered.set)
                if threading.get_ident() == loop_thread:
                    # Completion suppresses lookup errors. Keep the violation
                    # observable rather than throwing an assertion it can swallow.
                    violations.append('validation ran on the event-loop thread')
                else:
                    release.wait()
                    order.append('validation_returned')
            return original()
        session.validate_current = validate
        self.addCleanup(executor.shutdown, wait=True)
        return entered, release, violations, order

    def active(self, *, assignment='assignment-1'):
        return ActiveThreadTurn(thread_id='t1', turn_id='turn-1', project_id='p1',
            execution_id='bootstrap-exec', assignment_id=assignment,
            execution_workspace_id='workspace-1', worker_id='worker-1', fence=7,
            started_at=1, updated_at=1)

    async def release_from_loop(self, entered, release, violations, order):
        await asyncio.wait_for(entered.wait(), 30)  # Fixture deadlock guard, not SLA.
        self.assertEqual(violations, [])
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await progressed.wait()
        self.assertFalse(release.is_set())
        order.append('independent_loop_progress')
        release.set()

    async def test_actual_terminal_activity_owns_validation_and_deduplicates_completion(self):
        outcomes = []
        host, _, sessions, service = turns.TurnExecutionStartTests._service(self,
            bootstrap_thread_id='t1', work_item_outcome_recorder=lambda *args: outcomes.append(args))
        sessions.session.work_item_ref = 'group/app#531'
        active = self.active()
        host.active['t1'] = active
        entered, release, violations, order = self.gate(sessions.session)
        message = {'method': 'turn/completed', 'params': {'threadId': 't1', 'turn': {'id': 'turn-1'}}}
        owned = None
        try:
            service.record_thread_activity(message)
            self.assertEqual(violations, [], 'OLD completion lookup blocked the event loop')
            owned = service.assignment_completion_tasks['assignment-1']
            self.assertIs(service.thread_completion_tasks['t1'], owned)
            self.assertTrue(service.thread_is_active('t1'))
            await asyncio.wait_for(entered.wait(), 30)
            service._schedule_assignment_completion(active, succeeded=True, message=message)
            self.assertIs(service.assignment_completion_tasks['assignment-1'], owned)
            self.assertEqual(len(sessions.session._executor_work), 1)
            await self.release_from_loop(entered, release, violations, order)
            await owned
            self.assertLess(order.index('independent_loop_progress'), order.index('validation_returned'))
            self.assertEqual(outcomes, [('group/app#531', 'bootstrap-exec', 'succeeded')])
            self.assertEqual(service.assignment_completion_tasks, {})
            self.assertEqual(service.thread_completion_tasks, {})
            self.assertEqual(sessions.session._executor_work, set())
        finally:
            release.set()
            await asyncio.gather(*list(service.assignment_completion_tasks.values()), return_exceptions=True)
            if owned is not None:
                await asyncio.gather(owned, return_exceptions=True)

    async def test_cancelled_completion_keeps_barrier_until_actual_validation_joins(self):
        outcomes = []
        host, _, sessions, service = turns.TurnExecutionStartTests._service(self,
            bootstrap_thread_id='t1', work_item_outcome_recorder=lambda *args: outcomes.append(args))
        host.active['t1'] = self.active()
        entered, release, violations, _ = self.gate(sessions.session)
        owned = None
        try:
            service.record_thread_activity({'method': 'turn/completed',
                'params': {'threadId': 't1', 'turn': {'id': 'turn-1'}}})
            self.assertEqual(violations, [])
            owned = service.assignment_completion_tasks['assignment-1']
            await asyncio.wait_for(entered.wait(), 30)
            owned.cancel()
            progressed = asyncio.Event()
            asyncio.get_running_loop().call_soon(progressed.set)
            await progressed.wait()
            self.assertFalse(owned.done())
            self.assertTrue(service.thread_is_active('t1'))
            self.assertEqual(len(sessions.session._executor_work), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await owned
            self.assertEqual(outcomes, [])
            self.assertEqual(sessions.completed, [])
            self.assertEqual(service.thread_completion_tasks, {})
            self.assertEqual(sessions.session._executor_work, set())
        finally:
            release.set()
            if owned is not None:
                await asyncio.gather(owned, return_exceptions=True)

    async def test_changed_active_turn_during_completion_validation_is_preserved(self):
        outcomes = []
        host, _, sessions, service = turns.TurnExecutionStartTests._service(self,
            bootstrap_thread_id='t1', work_item_outcome_recorder=lambda *args: outcomes.append(args))
        host.active['t1'] = self.active()
        entered, release, violations, _ = self.gate(sessions.session)
        owned = None
        try:
            service.record_thread_activity({'method': 'turn/completed',
                'params': {'threadId': 't1', 'turn': {'id': 'turn-1'}}})
            self.assertEqual(violations, [])
            owned = service.assignment_completion_tasks['assignment-1']
            await asyncio.wait_for(entered.wait(), 30)
            newer = self.active().model_copy(update={'turn_id': 'newer-turn'})
            host.active['t1'] = newer
            release.set()
            await owned
            self.assertEqual(host.active['t1'], newer)
            self.assertEqual(outcomes, [])
            self.assertEqual(sessions.completed, [])
        finally:
            release.set()
            if owned is not None:
                await asyncio.gather(owned, return_exceptions=True)

    async def retained_terminal(self, *, retired):
        _, _, sessions, service = turns.TurnExecutionStartTests._service(self, bootstrap_thread_id='t1')
        entered, release, violations, order = self.gate(sessions.session)
        active = self.active(assignment='retired-assignment' if retired else 'assignment-1')
        recorder, disposition = AsyncMock(), []
        service.retained_turn_outcome_recorder = recorder
        task = asyncio.create_task(service.reconcile_retained_terminal(active, succeeded=True,
            recorder=recorder, retired_disposition=(lambda current: disposition.append(current.id)) if retired else None))
        try:
            await self.release_from_loop(entered, release, violations, order)
            self.assertTrue(await task)
            recorder.assert_awaited_once()
            self.assertEqual(disposition, ['assignment-1'] if retired else [])
            self.assertEqual(service.thread_completion_tasks, {})
        finally:
            release.set()
            await asyncio.gather(task, *list(service.assignment_completion_tasks.values()), return_exceptions=True)

    async def test_same_custody_retained_terminal_validation_is_offloop(self):
        await self.retained_terminal(retired=False)

    async def test_retired_custody_terminal_validation_is_offloop(self):
        await self.retained_terminal(retired=True)

    async def test_actual_sessionless_cleanup_validation_is_offloop(self):
        host, _, sessions, service = turns.TurnExecutionStartTests._service(self, bootstrap_thread_id='t1')
        binding = ExecutionRuntimeBinding(provider_id='mammouth-ai', runtime_id='mammouth-cli', capability_revision=1)
        sessions.session = turns._AlternateSession(binding)
        sessions.session.runtime.native_session_id = None
        service.runtime_adapter_factory = lambda *_: SimpleNamespace(
            read_session=AsyncMock(return_value=AgentRuntimeResult(payload={'active': False})))
        host.active['t1'] = self.active()
        entered, release, violations, order = self.gate(sessions.session)
        task = asyncio.create_task(service.release_unviable_active_turn('t1'))
        try:
            await self.release_from_loop(entered, release, violations, order)
            self.assertTrue(await task)
            self.assertNotIn('t1', host.active)
            self.assertEqual(len(sessions.completed), 1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


class NativeRenewalValidationOffloopTests(unittest.IsolatedAsyncioTestCase):
    gate = NativeCustodyLifecycleOffloopTests.gate
    release_from_loop = NativeCustodyLifecycleOffloopTests.release_from_loop

    async def asyncSetUp(self):
        self.f = renewal.AssignmentRenewalTests()
        self.f.addCleanup = self.addCleanup
        self.f.addAsyncCleanup = self.addAsyncCleanup
        await self.f.asyncSetUp()

    async def renewal_validation(self, at_call):
        f = self.f
        session = f.real_manager.get(f.old.id)
        inspected = []
        members = {Path(member.workspace_path).resolve(): member
                   for member in f.old_workspace.repository_members}
        def inspect_metadata(assignment, *, workspace_path, source_path):
            # This lifecycle test controls only the sandbox Git observation.
            # Keep LocalExecutionWorkerRuntime.inspect_git_metadata and its
            # canonical live worker/lease/fence validation in the real path.
            self.assertEqual(assignment.id, f.old.id)
            self.assertEqual(assignment.fence, f.old.fence)
            self.assertEqual(assignment.repository_scope, f.old.repository_scope)
            member = members[Path(workspace_path).resolve()]
            self.assertEqual(Path(source_path).resolve(), Path(member.source_path).resolve())
            inspected.append(member.resource_id)
            return {'head': member.base_revision,
                    'branch': member.branch_name or '', 'dirty': ''}
        original_inspect = f.local_worker.backend.inspect_git_metadata
        f.local_worker.backend.inspect_git_metadata = inspect_metadata
        entered, release, violations, order = self.gate(session, at_call=at_call)
        task = asyncio.create_task(f.actual.renew_exhausted_assignment(
            'native-governor', project=f.f.project, actor=f.actor, expected_assignment_id=f.old.id))
        try:
            await self.release_from_loop(entered, release, violations, order)
            renewed = await task
            self.assertNotEqual(renewed.assignment_id, f.old.id)
            self.assertLess(order.index('independent_loop_progress'), order.index('validation_returned'))
            self.assertEqual(f.f.workers.store.assignment(f.old.id).status, 'cancelled')
            self.assertEqual(f.native_inputs, [])
            self.assertEqual([method for method, _ in f.native_requests], ['thread/read'])
            self.assertEqual(inspected, [member.resource_id
                                        for member in f.old_workspace.repository_members])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            f.local_worker.backend.inspect_git_metadata = original_inspect

    async def test_actual_idle_renewal_initial_validation_is_offloop(self):
        await self.renewal_validation(at_call=1)

    async def test_actual_idle_renewal_post_provider_read_validation_is_offloop(self):
        # This transport fixture also validates inside its thread/read seam;
        # the third call is the runtime's real post-read custody validation.
        await self.renewal_validation(at_call=3)
