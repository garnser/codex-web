"""Real transport and canonical private storage; never execute a native agent."""
import asyncio
import io
import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import (AssignmentClaimRequest, AssignmentStartRequest,
    AssignmentCancelRequest, ExecutionWorkerState, BootstrapCreateObservation, NativeCredentialCustody)
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.bootstrap_native_create import BootstrapNativeCreateCapture, record_joined_cleanup
from codex_web.services.execution_workers import AssignmentNotFoundError
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.storage.execution_workers import ExecutionWorkerStore
from tests import test_execution_workers as worker_fixtures
from tests.test_codex_runtime import _Host


class BootstrapCreateObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = worker_fixtures.ExecutionWorkerServiceTests()
        self.f.setUp()
        self.addCleanup(self.f.temp.cleanup)
        created = self.f._assignment(work_item_ref=None,
            subject=ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-original'),
            execution_id='thread-bootstrap-original')
        claimed = self.f.service.claim(self.f.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=self.f.worker_actor)
        self.original = self.f.service.start(self.f.worker.id, created.id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token),
            actor=self.f.worker_actor)
        # This fixture owns a private canonical assignment; workspace provision
        # is irrelevant to the transport/record boundary under test.
        def workspace(state):
            state.assignments = [state.assignments[0].model_copy(update={'execution_workspace_id': 'workspace-original'})]
            return state
        self.f.service.store.transition_assignment(self.f.worker.id, self.original.id, workspace)
        self.original = self.saved()
        self.capture = BootstrapNativeCreateCapture(self.f.service, self.f.admin).bind_assignment(
            self.original, worker_id=self.f.worker.id, fence=self.original.fence, validator=self.saved)
        self.runtime = CodexRuntime(_Host(), restart_on_timeout=False)
        self.runtime.ensure_started = AsyncMock()
        self.pipe = io.StringIO()
        self.process = SimpleNamespace(pid=12345, stdin=self.pipe, poll=lambda: None)
        self.runtime.proc = self.process
        self.sent = asyncio.Event()
        actual_send = self.runtime._send
        async def send(*args, **kwargs):
            await actual_send(*args, **kwargs)
            self.sent.set()
        self.runtime._send = send

    def saved(self):
        return self.f.service.store.assignment(self.original.id)

    async def request(self):
        task = asyncio.create_task(self.runtime.request('thread/start', {'cwd': '/private/workspace'},
            start_capture=self.capture))
        await asyncio.wait_for(self.sent.wait(), 5)  # Fixture deadlock guard.
        return task, json.loads(self.pipe.getvalue())['id']

    def cancel_assignment(self):
        return self.f.service.cancel_bootstrap(self.original.id,
            AssignmentCancelRequest(expected_fence=self.original.fence, reason='private cancellation'),
            actor=self.f.admin)

    async def response(self, rpc_id, native_id='native-original'):
        await self.runtime._dispatch_message({'id': rpc_id, 'result': {'thread': {'id': native_id}}})

    async def test_cancelled_waiter_late_create_survives_restart_without_replay_or_custody_change(self):
        task, rpc_id = await self.request()
        self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'unknown')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        cancelled = self.cancel_assignment()
        before = cancelled.model_dump(mode='json', exclude={'bootstrap_create_observation'})
        await self.response(rpc_id)
        saved = self.saved()
        self.assertEqual(saved.model_dump(mode='json', exclude={'bootstrap_create_observation'}), before)
        self.assertEqual(saved.bootstrap_create_observation.outcome, 'created')
        self.assertEqual(saved.bootstrap_create_observation.provider_native_session_id, 'native-original')
        restarted = ExecutionWorkerStore(self.f.service.store.store).assignment(self.original.id)
        self.assertEqual(restarted, saved)
        self.assertEqual(len(self.pipe.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(self.pipe.getvalue())['method'], 'thread/start')
        self.assertNotIn('secret-ref-1', saved.bootstrap_create_observation.model_dump_json())
        self.assertIsNone(saved.bootstrap_create_observation.cleanup_joined_at)

    async def test_cancellation_during_preparation_joins_original_writer(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        actual_prepare = BootstrapNativeCreateCapture.prepare
        def prepare(capture, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return actual_prepare(capture, **kwargs)
        with patch.object(BootstrapNativeCreateCapture, 'prepare', prepare):
            task = asyncio.create_task(self.runtime.request('thread/start', {'cwd': '/private/workspace'},
                start_capture=self.capture))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)  # Deliver cancellation, not a latency assertion.
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'unknown')
                self.assertEqual(self.pipe.getvalue(), '')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_post_create_guard_or_binding_failure_preserves_creation_fact(self):
        task, rpc_id = await self.request()
        await self.response(rpc_id)
        await task
        # Both failures happen after create returns, before adoption. They use
        # the same canonical cancellation and may not erase its response.
        for reason in ('postcreate admission failed', 'bootstrap binding persistence failed'):
            with self.subTest(reason=reason):
                self.cancel_assignment()
                self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'created')
                self.assertEqual(len(self.pipe.getvalue().splitlines()), 1)

    async def test_real_controller_binding_failure_after_captured_adapter_create(self):
        from tests import test_thread_bootstrap_routing as fixtures
        fixture = fixtures.ThreadBootstrapCreateTests()
        host, planner, manager, bindings, controller = fixture._service()
        # The ordinary controller/adapter runs; only sandbox/provider transport
        # and bootstrap binding storage failure are private fixtures.
        manager.session.bootstrap_create_capture = lambda: self.capture
        async def captured_request(method, params, capture):
            return await self.runtime.request(method, params, start_capture=capture)
        manager.session.request_with_start_capture = captured_request
        bindings.bind = lambda **kwargs: (_ for _ in ()).throw(ValueError('private binding failure'))
        task = asyncio.create_task(controller.create(project_id='p1'))
        try:
            await asyncio.wait_for(self.sent.wait(), 5)
            rpc_id = json.loads(self.pipe.getvalue())['id']
            await self.response(rpc_id)
            with self.assertRaisesRegex(ValueError, 'private binding failure'):
                await task
            self.assertTrue(manager.completed)
            self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'created')
            self.assertEqual(len(self.pipe.getvalue().splitlines()), 1)
            self.assertEqual(host.settings, [])
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_real_controller_postcreate_guard_failure_preserves_response(self):
        from tests import test_thread_bootstrap_routing as fixtures
        _, _, manager, _, controller = fixtures.ThreadBootstrapCreateTests()._service()
        manager.session.bootstrap_create_capture = lambda: self.capture
        async def captured_request(method, params, capture):
            return await self.runtime.request(method, params, start_capture=capture)
        manager.session.request_with_start_capture = captured_request
        def guard():
            observation = self.saved().bootstrap_create_observation
            if observation is not None and observation.outcome == 'created':
                raise ValueError('postcreate guard revoked')
        task = asyncio.create_task(controller.create(project_id='p1', admission_guard=guard))
        try:
            await asyncio.wait_for(self.sent.wait(), 5)
            await self.response(json.loads(self.pipe.getvalue())['id'])
            with self.assertRaisesRegex(ValueError, 'postcreate guard revoked'):
                await task
            self.assertTrue(manager.completed)
            self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'created')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_supported_flat_thread_id_and_malformed_response_controls(self):
        task, rpc_id = await self.request()
        correlation = self.saved().bootstrap_create_observation
        for message in (
                {'id': rpc_id, 'result': {'thread': None}},
                {'id': rpc_id, 'result': {'thread': {'id': 'one', 'threadId': 'two'}}},
                {'id': rpc_id, 'result': {'threadId': 'one'}, 'error': None},
                {'id': rpc_id, 'error': {'code': True}}):
            with self.subTest(message=message):
                self.assertFalse(self.capture.observe(correlation,
                    generation=correlation.runtime_generation, message=message))
                self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'unknown')
        await self.runtime._dispatch_message({'id': rpc_id, 'result': {'threadId': 'native-flat'}})
        await task
        self.assertEqual(self.saved().bootstrap_create_observation.provider_native_session_id, 'native-flat')

    async def test_observation_transition_preserves_another_assignment(self):
        other = self.f._assignment(execution_id='unrelated-execution')
        before = other.model_dump(mode='json')
        task, rpc_id = await self.request()
        await self.response(rpc_id)
        await task
        self.assertEqual(self.f.service.store.assignment(other.id).model_dump(mode='json'), before)

    async def test_manager_cleanup_waits_for_actual_executor_owner(self):
        task, rpc_id = await self.request()
        await self.response(rpc_id)
        await task
        original = self.saved()
        release, entered = threading.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        producer = executor.submit(release.wait)
        class OwnedExecutor:
            def shutdown(self, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                executor.shutdown(**kwargs)
        async def stop():
            self.process.poll = lambda: 0
        status = lambda: SimpleNamespace(running=False, pending_executor_work=0,
            worker_id=original.assigned_worker_id, fence=original.fence)
        session = SimpleNamespace(runtime=self.runtime, _owned_process=self.process,
            _bootstrap_create_capture_enabled=True,
            fence=original.fence, worker_id=original.assigned_worker_id,
            _supervision_executor=OwnedExecutor(), stop=stop, status=status)
        manager = AssignmentBoundAgentProcessSessionManager.__new__(AssignmentBoundAgentProcessSessionManager)
        manager._assignment_locks = weakref.WeakValueDictionary()
        manager.sessions = {original.id: session}
        manager.local_worker = SimpleNamespace(worker_service=self.f.service,
            control_actor=self.f.admin, _pending_assignment=lambda assignment_id: self.saved())
        cleanup = asyncio.create_task(manager.cancel_bootstrap(original.id, reason='private cleanup'))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertFalse(cleanup.done())
            self.assertIsNone(self.saved().bootstrap_create_observation.cleanup_joined_at)
            release.set()
            await asyncio.wait_for(cleanup, 5)
            self.assertTrue(producer.done())
            self.assertIsNotNone(self.saved().bootstrap_create_observation.cleanup_joined_at)
        finally:
            release.set()
            await asyncio.gather(cleanup, return_exceptions=True)
            executor.shutdown(wait=True, cancel_futures=True)

    async def test_prepared_original_cannot_acquire_another_native_request(self):
        task, _ = await self.request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            await self.runtime.request('thread/start', {'cwd': '/private/workspace'}, start_capture=self.capture)
        self.assertEqual(len(self.pipe.getvalue().splitlines()), 1)

    async def test_failed_stop_never_records_joined_cleanup(self):
        task, rpc_id = await self.request()
        await self.response(rpc_id)
        await task
        async def failed_stop():
            raise RuntimeError('owned process cleanup failed')
        session = SimpleNamespace(runtime=self.runtime, _owned_process=self.process,
            _bootstrap_create_capture_enabled=True, fence=self.original.fence,
            worker_id=self.original.assigned_worker_id, stop=failed_stop)
        manager = AssignmentBoundAgentProcessSessionManager.__new__(AssignmentBoundAgentProcessSessionManager)
        manager._assignment_locks = weakref.WeakValueDictionary()
        manager.sessions = {self.original.id: session}
        manager.local_worker = SimpleNamespace(worker_service=self.f.service,
            control_actor=self.f.admin, _pending_assignment=lambda assignment_id: self.saved())
        with self.assertRaisesRegex(RuntimeError, 'owned process cleanup failed'):
            await manager.cancel_bootstrap(self.original.id, reason='private failed stop')
        saved = self.saved()
        self.assertEqual(saved.status, 'cancelled')
        self.assertEqual(saved.bootstrap_create_observation.outcome, 'created')
        self.assertIsNone(saved.bootstrap_create_observation.cleanup_joined_at)

    async def test_changed_response_generation_assignment_and_scope_refuse(self):
        task, rpc_id = await self.request()
        correlation = self.saved().bootstrap_create_observation
        self.assertFalse(self.capture.observe(correlation, generation='other-generation',
            message={'id': rpc_id, 'result': {'thread': {'id': 'native-original'}}}))
        self.assertFalse(self.capture.observe(correlation, generation=correlation.runtime_generation,
            message={'id': 'other-rpc', 'result': {'thread': {'id': 'native-original'}}}))
        for change in ({'id': 'foreign-assignment'}, {'organization_id': 'foreign'},
                {'execution_workspace_id': 'foreign-workspace'}, {'resource_ids': ('foreign',)},
                {'assigned_worker_id': 'foreign-worker'}, {'fence': self.original.fence + 1},
                {'execution_profile_id': 'changed-profile'},
                {'native_credential_custody': NativeCredentialCustody(runtime_secret_ref='changed-ref')}):
            with self.subTest(change=change), self.assertRaises((ValueError, AssignmentNotFoundError)):
                replace(self.capture, assignment=self.original.model_copy(update=change)).observe(
                    correlation, generation=correlation.runtime_generation,
                    message={'id': rpc_id, 'result': {'thread': {'id': 'native-original'}}})
        await self.response(rpc_id)
        await task
        self.capture.observe(correlation, generation=correlation.runtime_generation,
            message={'id': rpc_id, 'result': {'thread': {'id': 'native-original'}}})
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.capture.observe(correlation, generation=correlation.runtime_generation,
                message={'id': rpc_id, 'result': {'thread': {'id': 'different'}}})

    async def test_unknown_send_and_protocol_error_never_become_no_effects(self):
        task, rpc_id = await self.request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.saved().bootstrap_create_observation.outcome, 'unknown')
        await self.runtime._dispatch_message({'id': rpc_id,
            'error': {'code': -32603, 'message': 'private-secret-provider-details'}})
        observation = self.saved().bootstrap_create_observation
        self.assertEqual((observation.outcome, observation.provider_error_code), ('error_response', -32603))
        self.assertNotIn('private-secret', observation.model_dump_json())

    async def test_cleanup_is_separate_and_requires_original_joined_process(self):
        task, rpc_id = await self.request()
        await self.response(rpc_id)
        await task
        original = self.saved()
        self.cancel_assignment()
        status = SimpleNamespace(running=False, pending_executor_work=1,
            worker_id=original.assigned_worker_id, fence=original.fence)
        self.process.poll = lambda: 0
        record_joined_cleanup(self.f.service, original, self.f.admin,
            runtime_generation=self.runtime.generation, process=self.process, status=status)
        self.assertIsNone(self.saved().bootstrap_create_observation.cleanup_joined_at)
        status.pending_executor_work = 0
        with self.assertRaises(ValueError):
            record_joined_cleanup(self.f.service, original, self.f.admin,
                runtime_generation='changed', process=self.process, status=status)
        record_joined_cleanup(self.f.service, original, self.f.admin,
            runtime_generation=self.runtime.generation, process=self.process, status=status)
        saved = self.saved()
        self.assertEqual(saved.bootstrap_create_observation.outcome, 'created')
        self.assertEqual(saved.bootstrap_create_observation.process_exit_code, 0)
        self.assertIsNotNone(saved.bootstrap_create_observation.cleanup_joined_at)
        self.assertEqual((saved.status, saved.fence), ('cancelled', original.fence + 1))

    def test_legacy_serialization_and_explicit_reader_boundary(self):
        old = self.saved()
        self.assertNotIn('bootstrap_create_observation', old.model_dump(mode='json'))
        self.assertEqual(ExecutionWorkerState(assignments=[old]).schema_version, '1.14')
        self.assertEqual(ExecutionWorkerState(schema_version='1.17', assignments=[old]).schema_version, '1.17')
        for change in ({'outcome': 'created'}, {'outcome': 'error_response'},
                {'outcome': 'unknown', 'provider_native_session_id': 'made-up'}, {'process_exit_code': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                BootstrapCreateObservation(assignment_id=old.id, execution_id=old.execution_id,
                    execution_workspace_id=old.execution_workspace_id, worker_id=old.assigned_worker_id,
                    fence=old.fence, attribution_sha256='a' * 64, runtime_generation='generation',
                    rpc_request_id='rpc', prepared_at=1, **change)

    def test_legacy_cancelled_original_acquires_no_cleanup_backfill(self):
        original = self.saved()
        self.cancel_assignment()
        status = SimpleNamespace(running=False, pending_executor_work=0,
            worker_id=original.assigned_worker_id, fence=original.fence)
        record_joined_cleanup(self.f.service, original, self.f.admin,
            runtime_generation='unproven', process=SimpleNamespace(pid=123, poll=lambda: 0), status=status)
        self.assertNotIn('bootstrap_create_observation', self.saved().model_dump(mode='json'))
