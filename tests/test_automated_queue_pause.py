from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from codex_web.models import Project
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_turn_execution as fixtures


class AutomatedQueuePauseTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, directory, *, retained=True):
        host = fixtures._Host()
        host._project = lambda _id: Project(id='p', name='Project', path='/workspace/project')
        runtime = TurnExecutionService(host)
        runtime.automated_admission_allowed = lambda: False
        runtime.schedule_queue_drain = Mock()
        runtime.start_thread_turn_now = AsyncMock(return_value={'turn': {'id': 'native'}})
        if retained:
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
        return host, runtime

    async def test_paused_drain_keeps_exact_snapshot_without_preflight_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='automatic', source='gitlab')
            before = item.model_dump(mode='json')
            runtime.queued_preflight.retain_queued = Mock(side_effect=AssertionError('must not write'))
            await runtime.drain_thread_queue('t')
            await runtime.drain_thread_queue('t')
            self.assertEqual(host.queues['t'][0].model_dump(mode='json'), before)
            runtime.start_thread_turn_now.assert_not_awaited()
            runtime.schedule_queue_drain.assert_not_called()
            self.assertEqual(runtime.queue_admission_status('t')['automationWaiting'], 1)

    async def test_mixed_queue_allows_human_without_reordering_automation(self):
        for retained in (False, True):
            for human_source in ('web', 'slack', 'telegram', 'operator-ui', 'operator-retry'):
                with self.subTest(retained=retained, human_source=human_source), tempfile.TemporaryDirectory() as directory:
                    host, runtime = self.runtime(directory, retained=retained)
                    first = runtime.enqueue_turn(thread_id='t', project_id='p', message='first', source='work-item-handoff')
                    human = runtime.enqueue_turn(thread_id='t', project_id='p', message='human', source=human_source)
                    last = runtime.enqueue_turn(thread_id='t', project_id='p', message='last', source='gitlab')
                    before = [item.model_dump(mode='json') for item in (first, last)]
                    await runtime.drain_thread_queue('t')
                    self.assertEqual([item.model_dump(mode='json') for item in host.queues['t']], before)
                    self.assertEqual(runtime.start_thread_turn_now.await_args.kwargs['message'], human.message)
                    if retained:
                        self.assertEqual(len(runtime.queued_preflight.store.for_thread('t')), 1)

    async def test_runtime_marker_and_environment_block_until_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            policy = RuntimePolicy(Path(directory))
            runtime.automated_admission_allowed = policy.autonomy_enabled
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='automatic', source='unknown-producer')
            marker = Path(directory) / 'AUTONOMY_DISABLED'
            marker.write_text('operator hold')
            await runtime.drain_thread_queue('t')
            marker.unlink()
            with patch.dict('os.environ', {'CODEX_WEB_AUTONOMY_ENABLED': 'off'}):
                await runtime.drain_thread_queue('t')
            self.assertEqual(host.queues['t'][0].id, item.id)
            with patch.dict('os.environ', {'CODEX_WEB_AUTONOMY_ENABLED': '1'}):
                await runtime.drain_thread_queue('t')
            runtime.start_thread_turn_now.assert_awaited_once()

    async def test_existing_unknown_is_unchanged_by_paused_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='unknown', source='gitlab')
            service = runtime.queued_preflight
            attempt = service.retain_queued(item, organization_id='local', workspace_id='default')
            _, claim = service.claim_queued_admission(attempt.id)
            service.begin_queued_start(attempt.id, claim_id=claim)
            before = service.store.get(attempt.id).model_dump(mode='json')
            await runtime.drain_thread_queue('t')
            self.assertEqual(service.store.get(attempt.id).model_dump(mode='json'), before)
            runtime.start_thread_turn_now.assert_not_awaited()

    async def test_pause_during_setup_restores_snapshot_order_and_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            host, _binding, sessions, runtime = fixtures.TurnExecutionStartTests()._service()
            project = Project(id='p1', name='Project', path='/workspace/project')
            host._project = lambda _id: project
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            runtime.schedule_queue_drain = Mock()
            enabled = {'value': True}
            runtime.automated_admission_allowed = lambda: enabled['value']
            items = [runtime.enqueue_turn(thread_id='t1', project_id='p1', message=str(i), source='gitlab') for i in range(3)]
            before = [item.model_dump(mode='json') for item in items]
            original_start = sessions.start
            async def pause_on_start(assignment_id):
                session = await original_start(assignment_id)
                enabled['value'] = False
                return session
            sessions.start = pause_on_start
            await runtime.drain_thread_queue('t1')
            self.assertEqual([item.model_dump(mode='json') for item in host.queues['t1']], before)
            self.assertFalse(runtime.thread_is_active('t1'))
            self.assertFalse(any(method == 'turn/start' for method, _ in sessions.session.requests))
            attempt = runtime.queued_preflight.store.for_thread('t1')[0]
            self.assertEqual(attempt.status, 'queued')
            self.assertIsNone(attempt.queued_start_receipt)
            self.assertEqual(attempt.queued_turn_snapshot, before[0])
            runtime.schedule_queue_drain.assert_not_called()
            enabled['value'] = True
            sessions.start = original_start
            await runtime.drain_thread_queue('t1')
            self.assertEqual(runtime.queued_preflight.store.get(attempt.id).status, 'started')
            self.assertEqual(sum(method == 'turn/start' for method, _ in sessions.session.requests), 1)

    async def test_direct_automation_is_refused_before_setup_human_is_admitted(self):
        host, binding, sessions, runtime = fixtures.TurnExecutionStartTests()._service()
        runtime.automated_admission_allowed = lambda: False
        project = Project(id='p1', name='Project', path='/workspace/project')
        with self.assertRaises(HTTPException) as caught:
            await runtime.start_thread_turn_now('t1', project=project, message='auto',
                sandbox=None, approval_policy=None, source='gitlab')
        self.assertEqual(caught.exception.detail['code'], 'autonomy_paused')
        self.assertEqual(sessions.started, [])
        await runtime.start_thread_turn_now('t1', project=project, message='human',
            sandbox=None, approval_policy=None, source='web')
        self.assertTrue(runtime.thread_is_active('t1'))

    async def test_restart_and_control_wakeup_recheck_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='auto', source='gitlab')
            await runtime.resume_active_threads_after_startup()
            runtime.schedule_queue_drain.assert_called_with('t')
            await runtime.drain_thread_queue('t')
            self.assertEqual(host.queues['t'][0].id, item.id)
            runtime.resume_automated_queues()
            await runtime.drain_thread_queue('t')
            runtime.start_thread_turn_now.assert_not_awaited()
            runtime.automated_admission_allowed = lambda: True
            runtime.resume_automated_queues()
            await runtime.drain_thread_queue('t')
            runtime.start_thread_turn_now.assert_awaited_once()

    async def test_provider_pause_shaped_error_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            runtime.automated_admission_allowed = lambda: True
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='auto', source='gitlab')
            runtime.start_thread_turn_now = AsyncMock(side_effect=HTTPException(
                status_code=409, detail={'code': 'autonomy_paused'}))
            await runtime.drain_thread_queue('t')
            attempt = runtime.queued_preflight.store.for_thread('t')[0]
            self.assertEqual(attempt.status, 'reconciliation_required')
            self.assertEqual(host._thread_queue('t'), [])

    async def test_canonical_paused_and_killed_controls_block_then_resume(self):
        from codex_web.autonomy import AutonomyControl
        from codex_web.storage.autonomy import AutonomyStateStore
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            control = AutonomyStateStore(SQLiteStateStore(Path(directory) / 'autonomy.sqlite'))
            policy = RuntimePolicy(Path(directory))
            runtime.automated_admission_allowed = lambda: (
                policy.autonomy_enabled() and control.control().mode == 'active')
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='auto', source='gitlab')
            for mode in ('paused', 'killed'):
                control.set_control(AutonomyControl(mode=mode), actor_id='operator')
                await runtime.drain_thread_queue('t')
                self.assertEqual(host.queues['t'][0].id, item.id)
                runtime.start_thread_turn_now.assert_not_awaited()
            control.set_control(AutonomyControl(mode='active'), actor_id='operator')
            await runtime.drain_thread_queue('t')
            runtime.start_thread_turn_now.assert_awaited_once()

    async def test_pause_after_claim_returns_claim_without_popping_or_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            enabled = {'value': True}
            runtime.automated_admission_allowed = lambda: enabled['value']
            item = runtime.enqueue_turn(thread_id='t', project_id='p', message='auto', source='gitlab')
            before = item.model_dump(mode='json')
            claim = runtime.queued_preflight.claim_queued_admission
            def pause_on_claim(attempt_id):
                result = claim(attempt_id)
                enabled['value'] = False
                return result
            runtime.queued_preflight.claim_queued_admission = pause_on_claim
            await runtime.drain_thread_queue('t')
            self.assertEqual(host.queues['t'][0].model_dump(mode='json'), before)
            self.assertEqual(runtime.queued_preflight.store.for_thread('t')[0].status, 'queued')
            runtime.start_thread_turn_now.assert_not_awaited()

    def test_control_api_wakes_queues_only_after_authorized_state_change(self):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.autonomy import build_autonomy_router
        from codex_web.identity import PrincipalKind
        from codex_web.services.autonomy_controller import AutonomyController
        from codex_web.storage.autonomy import AutonomyStateStore
        with tempfile.TemporaryDirectory() as directory:
            control = AutonomyStateStore(SQLiteStateStore(Path(directory) / 'control.sqlite'))
            actor = SimpleNamespace(principal_kind=PrincipalKind.SERVICE,
                                    service_scopes=(), identity_id='operator')
            app = FastAPI()
            @app.middleware('http')
            async def identity(request, call_next):
                request.state.identity_actor = actor
                return await call_next(request)
            observed = []
            app.include_router(build_autonomy_router(AutonomyController(control),
                control_changed=lambda: observed.append(control.control().mode)))
            with TestClient(app) as client:
                self.assertEqual(client.post('/api/autonomy/pause').status_code, 403)
                self.assertEqual(observed, [])
                actor.service_scopes = ('autonomy:admin',)
                for action, mode in (('pause', 'paused'), ('kill', 'killed'), ('resume', 'active')):
                    self.assertEqual(client.post('/api/autonomy/' + action).status_code, 200)
                    self.assertEqual(observed[-1], mode)
                self.assertEqual(client.patch('/api/autonomy/control', json={'mode':'paused'}).status_code, 200)
                self.assertEqual(observed, ['paused', 'killed', 'active', 'paused'])

    async def test_prestart_pause_restores_middle_position_ahead_of_new_arrivals(self):
        from codex_web.runtime.execution import AutomatedAdmissionPaused
        with tempfile.TemporaryDirectory() as directory:
            host, runtime = self.runtime(directory)
            runtime.automated_admission_allowed = lambda: True
            first, selected, last = [runtime.enqueue_turn(thread_id='t', project_id='p',
                message=str(i), source='gitlab') for i in range(3)]
            original = [item.model_dump(mode='json') for item in (first, selected, last)]
            preflight = runtime.queued_preflight
            unknown = preflight.retain_queued(first, organization_id='local', workspace_id='default')
            _, claim = preflight.claim_queued_admission(unknown.id)
            preflight.begin_queued_start(unknown.id, claim_id=claim)
            unknown_before = preflight.store.get(unknown.id).model_dump(mode='json')
            arrival = []
            async def pause_before_start(*args, **kwargs):
                arrival.append(runtime.enqueue_turn(thread_id='t', project_id='p',
                    message='new arrival', source='gitlab'))
                runtime.automated_admission_allowed = lambda: False
                raise AutomatedAdmissionPaused(status_code=409,
                    detail={'code':'autonomy_paused', 'message':'paused before start'})
            runtime.start_thread_turn_now = AsyncMock(side_effect=pause_before_start)
            await runtime.drain_thread_queue('t')
            self.assertEqual([item.model_dump(mode='json') for item in host.queues['t']],
                             original + [arrival[0].model_dump(mode='json')])
            self.assertEqual(preflight.store.get(unknown.id).model_dump(mode='json'), unknown_before)
