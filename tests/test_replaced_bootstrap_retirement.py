"""Real private worker/workspace lifecycle; native transport is the existing fixture seam."""
import asyncio
import contextlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from codex_web.execution_workers import AssignmentCancelRequest
from codex_web.models import QueuedTurn, Project
from codex_web.runtime.execution import TurnExecutionService, ExecutionPrestartRefusal
from tests.test_turn_execution import _Host
from codex_web.services.execution_workers import AssignmentReadinessOriginError
from tests import test_procedure_assignment_renewal as renewal
from tests.test_thread_recovery_service import recovery_service


class ReplacedBootstrapRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = renewal.AssignmentRenewalTests()
        self.env.addCleanup = self.addCleanup
        self.env.addAsyncCleanup = self.addAsyncCleanup
        await self.env.asyncSetUp()
        e = self.env
        self.old = e.old
        self.session = e.real_manager.get(self.old.id)
        self.created = AsyncMock(return_value={'thread': {'id': 'fresh-native'}})
        e.host._retarget_bot_thread_state = Mock()
        e.host._archive_replaced_bot_thread = AsyncMock(return_value=True)
        e.host.hub = type('Hub', (), {'publish': AsyncMock()})()
        self.recovery = recovery_service(e.host, project=e.f.project, thread_creator=self.created)
        # Baseline has neither hook wiring nor the owner method. Its existing
        # replacement still executes, making OLD fail on real retained custody.
        self.recovery.predecessor_retirement = getattr(e.actual,
            'retire_replacement_predecessor', self.legacy_context)
        self.workspace = self.session.workspace_path
        (self.workspace/'unique-unpublished.txt').write_text('preserve unique bytes')
        self.before_files = {str(p.relative_to(self.workspace)): p.read_bytes()
            for p in self.workspace.rglob('*') if p.is_file() and '.git' not in p.parts}
        self.before_assignment = e.f.workers.store.assignment(self.old.id)
        self.queued = QueuedTurn(id='protected-original', thread_id=e.native_thread_id,
            project_id=e.f.project.id, message='retained original', source='web', created_at=1.)
        e.host._put_thread_queue_record(e.native_thread_id, [self.queued])
        attempt = e.turn_service.preflight.retain_queued(self.queued,
            organization_id=e.actor.organization_id, workspace_id=e.actor.workspace_id)
        self.unknown = e.turn_service.preflight.store.update(attempt.id,
            lambda row: row.model_copy(update={'status':'reconciliation_required'}))

    @contextlib.asynccontextmanager
    async def legacy_context(self, *args, **kwargs):
        yield

    async def replace(self):
        return await self.recovery.replace_stale_web_thread(
            self.env.native_thread_id, self.env.f.project, 'retained stale native response')

    async def assert_refused(self, message):
        with self.assertRaises(HTTPException) as caught:
            await self.replace()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn(message, str(caught.exception.detail))
        self.created.assert_not_awaited()
        self.assertEqual(self.env.f.workers.store.assignment(self.old.id), self.before_assignment)
        self.assertFalse(self.session.stopped)
        self.env.host._retarget_bot_thread_state.assert_not_called()

    async def test_replacement_retires_owned_idle_predecessor_before_creating_successor(self):
        async def create(**kwargs):
            retired = self.env.f.workers.store.assignment(self.old.id)
            self.assertEqual(retired.status, 'cancelled')
            self.assertIsNone(retired.lease)
            self.assertEqual(retired.fence, self.old.fence + 1)
            self.assertTrue(self.session.stopped)
            return {'thread': {'id': 'fresh-native'}}
        self.created.side_effect = create
        result = await self.replace()
        self.assertEqual(result, 'fresh-native')
        self.created.assert_awaited_once()
        self.assertIsNone(self.env.real_manager.get(self.old.id))
        self.assertEqual(self.before_files, {str(p.relative_to(self.workspace)): p.read_bytes()
            for p in self.workspace.rglob('*') if p.is_file() and '.git' not in p.parts})
        self.assertEqual(self.env.native_inputs, [])
        self.assertEqual(self.env.host._thread_queue(self.env.native_thread_id), [self.queued])
        self.assertEqual(self.env.turn_service.preflight.store.get(self.unknown.id), self.unknown)
        self.assertEqual(self.recovery.replacement_thread_id(self.env.native_thread_id), 'fresh-native')

    async def test_active_turn_or_completion_and_handoff_refuse_before_creation(self):
        e = self.env
        with patch.object(e.actual, 'thread_is_active', return_value=True):
            await self.assert_refused('native turn/completion')
        with patch.object(e.actual, 'thread_handoff_in_progress', return_value=True):
            await self.assert_refused('handoff')

    async def test_pending_executor_prevents_retirement(self):
        self.session.pending = 1
        await self.assert_refused('executor custody')

    async def test_running_validation_preserves_predecessor(self):
        report = dict(schema_version='1.0', run_id='a'*32, command_digest='b'*64,
            sources=[{'path':'source.txt','sha256':'c'*64}], started_at=1.,
            finished_at=None, exit_code=None, state='running')
        (self.session.managed_validation.directory/('report-'+'a'*32+'.json')).write_text(json.dumps(report))
        await self.assert_refused('Managed validation')

    async def test_ready_origin_guard_remains_in_real_canonical_cancel_owner(self):
        e = self.env
        with patch.object(e.f.workers, '_require_settled_readiness_origin',
                side_effect=AssignmentReadinessOriginError(self.old.id, ('attempted-ready',))):
            await self.assert_refused('readiness')

    async def test_changed_bootstrap_at_final_check_refuses_without_touching_files(self):
        e = self.env
        original = e.actual._bootstrap_binding_for_thread(e.native_thread_id)
        with patch.object(e.actual, '_bootstrap_binding_for_thread',
                side_effect=[original, original.model_copy(update={'assignment_id':'foreign-assignment'})]):
            await self.assert_refused('binding or activity changed')

    async def test_missing_owned_process_is_not_retirement_permission(self):
        e = self.env
        with patch.object(e.actual, '_session_for_assignment',
                side_effect=HTTPException(409, 'registered process unavailable')):
            await self.assert_refused('registered process unavailable')

    async def test_creator_failure_keeps_terminal_predecessor_and_preserved_files(self):
        self.created.side_effect = RuntimeError('provider bootstrap refused')
        with self.assertRaisesRegex(RuntimeError, 'provider bootstrap refused'):
            await self.replace()
        retired = self.env.f.workers.store.assignment(self.old.id)
        self.assertEqual(retired.status, 'cancelled')
        self.assertEqual(retired.fence, self.old.fence + 1)
        self.assertTrue(self.session.stopped)
        self.assertTrue((self.workspace/'unique-unpublished.txt').exists())
        self.assertIsNone(self.recovery.replacement_thread_id(self.env.native_thread_id))
        self.env.host._retarget_bot_thread_state.assert_not_called()

    async def test_admission_lock_is_held_until_replacement_graph_commits(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def create(**kwargs):
            entered.set()
            await release.wait()
            return {'thread': {'id': 'fresh-native'}}
        self.created.side_effect = create
        replacement = asyncio.create_task(self.replace())
        await entered.wait()
        e = self.env
        e.host._raise_if_thread_replaced = self.recovery.raise_if_thread_replaced
        waiting = asyncio.create_task(e.actual._start_thread_turn_now_admitted(
            e.native_thread_id, project=e.f.project, message='concurrent original',
            sandbox='workspace-write', approval_policy='on-request', actor=e.actor,
            execution_profile_id='project-governor'))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        release.set()
        await replacement
        with self.assertRaises(HTTPException) as caught:
            await waiting
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail['code'], 'thread_replaced')
        self.assertEqual(e.native_inputs, [])

    def cancel_canonically(self):
        return self.env.f.workers.cancel_bootstrap(self.old.id,
            AssignmentCancelRequest(expected_fence=self.old.fence, reason='normal operator retirement'),
            actor=self.env.actor)

    async def test_terminal_assignment_with_registered_process_joins_before_creator(self):
        terminal = self.cancel_canonically()
        self.assertIs(self.env.real_manager.get(self.old.id), self.session)
        self.assertFalse(self.session.stopped)
        async def create(**kwargs):
            self.assertTrue(self.session.stopped)
            self.assertIsNone(self.env.real_manager.get(self.old.id))
            self.assertEqual(self.env.f.workers.store.assignment(self.old.id), terminal)
            return {'thread': {'id': 'fresh-native'}}
        self.created.side_effect = create
        await self.replace()
        self.assertEqual(self.env.turn_service.preflight.store.get(self.unknown.id), self.unknown)

    async def test_inflight_manager_stop_is_joined_before_successor_creation(self):
        self.cancel_canonically()
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = self.session.stop
        async def stop():
            entered.set()
            await release.wait()
            await original_stop()
        self.session.stop = stop
        stopping = asyncio.create_task(self.env.real_manager.stop(self.old.id))
        await entered.wait()
        replacement = asyncio.create_task(self.replace())
        try:
            await asyncio.sleep(0)
            self.created.assert_not_awaited()
            self.assertIs(self.env.real_manager.get(self.old.id), self.session)
        finally:
            release.set()
        await stopping
        await replacement
        self.assertTrue(self.session.stopped)

    async def test_failed_stop_keeps_owner_until_later_join(self):
        self.cancel_canonically()
        original_stop = self.session.stop
        self.session.stop = AsyncMock(side_effect=RuntimeError('cleanup unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'cleanup unavailable'):
            await self.replace()
        self.created.assert_not_awaited()
        self.assertIs(self.env.real_manager.get(self.old.id), self.session)
        self.session.stop = original_stop
        await self.replace()
        self.assertTrue(self.session.stopped)
        self.assertIsNone(self.env.real_manager.get(self.old.id))

    async def test_terminal_cleanup_uses_existing_fallback_manager(self):
        self.cancel_canonically()
        self.env.actual.session_managers = {}
        self.env.actual.session_manager = self.env.real_manager
        await self.replace()
        self.assertTrue(self.session.stopped)
        self.assertIsNone(self.env.real_manager.get(self.old.id))

    async def test_terminal_fence_change_is_refused_under_manager_lock(self):
        terminal = self.cancel_canonically()
        original_stop = self.env.real_manager.stop
        async def changed(assignment_id, **kwargs):
            def update(state):
                state.assignments[0] = terminal.model_copy(update={'fence':terminal.fence + 1})
                return state
            self.env.f.workers.store.update_assignment(self.env.f.worker.id, terminal.id, update)
            return await original_stop(assignment_id, **kwargs)
        with patch.object(self.env.real_manager, 'stop', side_effect=changed):
            with self.assertRaises(HTTPException) as caught:
                await self.replace()
        self.assertEqual(caught.exception.status_code, 409)
        self.created.assert_not_awaited()
        self.assertFalse(self.session.stopped)


class LegacyQueuedReplacementPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_or_failed_replacement_retains_exact_original_without_reschedule(self):
        for error in (ExecutionPrestartRefusal(status_code=409,
                detail={'code':'thread_replacement_predecessor_unsettled'}), RuntimeError('provider creation failed'),
                asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                host = _Host()
                project = Project(id='p1', name='Project', path='/private/project')
                host._project = lambda _: project
                host._is_stale_thread_error = lambda _: True
                host._bindings_for_thread = lambda _: []
                host._replace_stale_web_thread = AsyncMock(side_effect=error)
                service = TurnExecutionService(host)
                service.start_thread_turn_now = AsyncMock(side_effect=RuntimeError('thread not found'))
                service.publish_queue_status = AsyncMock()
                service.schedule_queue_drain = Mock()
                first = service.enqueue_turn(thread_id='old', project_id='p1', message='exact original')
                second = service.enqueue_turn(thread_id='old', project_id='p1', message='untouched peer')
                before = [row.model_dump(mode='json') for row in host._thread_queue('old')]
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await service.drain_thread_queue('old')
                else:
                    await service.drain_thread_queue('old')
                self.assertEqual([row.model_dump(mode='json') for row in host._thread_queue('old')], before)
                self.assertEqual([row.id for row in host._thread_queue('old')], [first.id, second.id])
                service.schedule_queue_drain.assert_not_called()
                if not isinstance(error, asyncio.CancelledError):
                    self.assertEqual(host.events[-1]['type'], 'queued_thread_replacement_held')
                    self.assertFalse(host.events[-1]['native_outcome_changed'])


class ManagerOwnerEnumerationTests(unittest.TestCase):
    def test_unhashable_equal_managers_remain_distinct_and_aliases_are_visited_once(self):
        lookup = Mock(return_value=None)
        first = SimpleNamespace(get=lookup)
        second = SimpleNamespace(get=lookup)
        fallback = SimpleNamespace(get=lookup)
        self.assertEqual(first, second)  # Equal metadata is not shared custody.
        service = TurnExecutionService(SimpleNamespace(), session_manager=fallback,
            session_managers={('openai', 'codex'): first, ('alias', 'first'): first,
                              ('other', 'runtime'): second})
        with self.assertRaises(HTTPException) as caught:
            service._session_for_assignment('absent-assignment')
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(lookup.call_count, 3)
        self.assertEqual([id(owner) for owner in service._assignment_session_managers()],
                         [id(first), id(second), id(fallback)])
