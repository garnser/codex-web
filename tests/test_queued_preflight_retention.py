from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

from codex_web.models import QueuedTurn, BotReplyTarget
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class QueuedAdmissionDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_diagnostic_leaves_fresh_request_queued_and_unclaimed(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            host = _Host()
            host._project = lambda _id: Project(id='project', name='Project', path='/workspace')
            runtime = TurnExecutionService(host)
            preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory)/'state.sqlite')),
                replacement_resolver=lambda *_args, **_kwargs: None)
            old = runtime.enqueue_turn(thread_id='old', project_id='project', message='original')
            retained = preflight.retain_queued(old, organization_id='local', workspace_id='default')
            before = preflight.store.get(retained.id).model_dump(mode='json')
            host.queues.clear()
            host.queues['new'] = [old.model_copy(update={'thread_id':'new'})]
            fresh = runtime.enqueue_turn(thread_id='new', project_id='project', message='fresh')
            queued_before = [q.model_dump(mode='json') for q in host.queues['new']]
            runtime.queued_preflight = preflight
            runtime.start_thread_turn_now = AsyncMock()
            runtime.schedule_queue_drain = Mock()
            entered = asyncio.Event()
            async def blocked_publish(event):
                if event['type']=='queue.error':
                    entered.set()
                    await asyncio.Event().wait()
            host.hub.publish = blocked_publish
            task = asyncio.create_task(runtime.drain_thread_queue('new'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            runtime.start_thread_turn_now.assert_not_awaited()
            self.assertEqual([q.model_dump(mode='json') for q in host.queues['new']], queued_before)
            self.assertEqual(preflight.store.get(retained.id).model_dump(mode='json'), before)
            new_attempt = preflight.store.get('preflight-queued-'+fresh.id)
            self.assertEqual(new_attempt.status, 'queued')
            self.assertIsNone(new_attempt.queued_admission_claim_id)

    async def test_refused_replacement_preserves_unknown_and_admits_independent_work(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        for refusal in ('unverified', 'identity', 'snapshot', 'admitting'):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.sqlite'
                host = _Host()
                host._project = lambda _id: Project(id='project', name='Project', path='/workspace')
                runtime = TurnExecutionService(host)
                service = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(path)),
                    replacement_resolver=lambda *_args, **_kwargs: None if refusal == 'unverified' else 'new')
                original = runtime.enqueue_turn(thread_id='old', project_id='project', message='unknown original')
                attempt = service.retain_queued(original, organization_id='local', workspace_id='default')
                _, claim = service.claim_queued_admission(attempt.id)
                if refusal != 'admitting':
                    service.begin_queued_start(attempt.id, claim_id=claim)
                if refusal == 'identity':
                    service.store.update(attempt.id, lambda value: value.model_copy(update={'organization_id':'other'}))
                before = service.store.get(attempt.id).model_dump(mode='json')
                host.queues.clear()
                moved = original.model_copy(update={'thread_id':'new',
                    **({'message':'changed'} if refusal == 'snapshot' else {})})
                host.queues['new'] = [moved]
                fresh = runtime.enqueue_turn(thread_id='new', project_id='project', message='eligible fresh')
                runtime.queued_preflight = service
                runtime.start_thread_turn_now = AsyncMock(return_value={'turn':{'id':'actual-fresh'}})
                runtime.schedule_queue_drain = Mock()
                await runtime.drain_thread_queue('new')
                runtime.start_thread_turn_now.assert_awaited_once()
                self.assertEqual(runtime.start_thread_turn_now.await_args.kwargs['message'], fresh.message)
                self.assertEqual([q.model_dump(mode='json') for q in host._thread_queue('new')],
                    [moved.model_dump(mode='json')])
                reopened = ExecutionPreflightStore(SQLiteStateStore(path))
                self.assertEqual(reopened.get(attempt.id).model_dump(mode='json'), before)
                notices = [e for e in host.events if e['type']=='queued_retention_refused']
                self.assertEqual(len(notices), 1)
                self.assertEqual(notices[0]['code'], 'retained_queued_route_unverified')
                self.assertFalse(notices[0]['retryable'])

    async def test_all_refused_queues_do_not_spin_and_diagnostics_are_bounded(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            host = _Host()
            host._project = lambda _id: Project(id='project', name='Project', path='/workspace')
            runtime = TurnExecutionService(host)
            service = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory)/'state.sqlite')),
                replacement_resolver=lambda *_args, **_kwargs: None)
            originals = []
            for index in range(15):
                original = runtime.enqueue_turn(thread_id='old', project_id='project', message=f'original {index}')
                attempt = service.retain_queued(original, organization_id='local', workspace_id='default')
                originals.append((original, attempt))
            before = {a.id: service.store.get(a.id).model_dump(mode='json') for _,a in originals}
            host.queues.clear()
            host.queues['new'] = [q.model_copy(update={'thread_id':'new'}) for q,_ in originals]
            queued_before = [q.model_dump(mode='json') for q in host.queues['new']]
            runtime.queued_preflight = service
            runtime.start_thread_turn_now = AsyncMock()
            runtime.schedule_queue_drain = Mock()
            for _ in range(3):
                await runtime.drain_thread_queue('new')
            runtime.start_thread_turn_now.assert_not_awaited()
            runtime.schedule_queue_drain.assert_not_called()
            self.assertEqual([q.model_dump(mode='json') for q in host.queues['new']], queued_before)
            self.assertEqual({a.id:service.store.get(a.id).model_dump(mode='json') for _,a in originals}, before)
            notices = [e for e in host.events if e['type']=='queued_retention_refused']
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]['count'], 15)
            self.assertEqual(len(notices[0]['queued_ids']), 10)
            # The throttle is bounded telemetry, never a cached admission decision.
            for index in range(300):
                await runtime._publish_queued_retention_refusals(str(index), 1, ['sample'])
            self.assertLessEqual(len(runtime._queued_retention_notice_at), 256)

    async def test_verified_replacement_skips_unknown_and_starts_later_request(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from codex_web.storage.thread_index import ThreadIndexRepository
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteStateStore(Path(directory) / "state.sqlite")
            routes = ThreadIndexRepository(state, Path(directory) / "threads.json")
            routes.remember_replacement("old", "middle", project_id="project",
                provenance={"organization_id":"local", "workspace_id":"default"})
            routes.remember_replacement("middle", "new", project_id="project",
                provenance={"organization_id":"local", "workspace_id":"default"})
            host = _Host()
            host._project = lambda _id: Project(id="project", name="Project", path="/workspace/project")
            runtime = TurnExecutionService(host)
            events = []
            service = ExecutionPreflightService(ExecutionPreflightStore(state),
                replacement_resolver=routes.replacement_thread_id, event_sink=events.append)
            first = runtime.enqueue_turn(thread_id="old", project_id="project", message="uncertain original")
            original = service.retain_queued(first, organization_id="local", workspace_id="default")
            _, claim = service.claim_queued_admission(original.id)
            service.begin_queued_start(original.id, claim_id=claim)
            # Persist the historical pre-ingress schema without rewriting authority.
            service.store.update(original.id, lambda current: current.model_copy(update={
                "queued_turn_snapshot":{k:v for k,v in current.queued_turn_snapshot.items()
                    if k not in {"canonical_ingress_id", "ingress_execution_profile_definition"}}}))
            before = service.store.get(original.id).model_dump(mode="json")
            host.queues.clear()
            moved = first.model_copy(update={"thread_id":"new"})
            host.queues["new"] = [moved]
            second = runtime.enqueue_turn(thread_id="new", project_id="project", message="ready later")
            runtime.queued_preflight = service
            runtime.start_thread_turn_now = AsyncMock(return_value={"turn":{"id":"actual"}})
            runtime.schedule_queue_drain = Mock()
            await runtime.drain_thread_queue("new")
            runtime.start_thread_turn_now.assert_awaited_once()
            self.assertEqual(runtime.start_thread_turn_now.await_args.kwargs["message"], second.message)
            self.assertEqual([q.id for q in host._thread_queue("new")], [first.id])
            saved = service.store.get(original.id).model_dump(mode="json")
            self.assertEqual({k:v for k,v in saved.items() if k not in
                {"replacement_thread_id","updated_at"}}, {k:v for k,v in before.items() if k not in
                {"replacement_thread_id","updated_at"}})
            self.assertEqual(saved["replacement_thread_id"], "new")
            self.assertIsNone(saved["queued_start_receipt"])
            self.assertEqual(events[0]["type"], "queued_preflight_replacement_route_restored")
            reopened = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / "state.sqlite")),
                replacement_resolver=routes.replacement_thread_id)
            self.assertEqual(reopened.retain_queued(moved, organization_id="local",
                workspace_id="default").status, "reconciliation_required")
            self.assertTrue(any(a.id==original.id for a in reopened.store.for_thread("new")))

    async def test_legacy_queue_without_execution_id_uses_retained_intent_correlation(self):
        from codex_web.models import Project
        from tests.test_turn_execution import TurnExecutionStartTests
        with tempfile.TemporaryDirectory() as directory:
            host, binding, sessions, runtime = TurnExecutionStartTests()._service()
            project = Project(id='p1', name='Project', path='/workspace/project',
                sandbox='workspace-write', approval_policy='on-request')
            host._project = lambda _id: project
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            runtime.schedule_queue_drain = Mock()
            original = runtime.enqueue_turn(thread_id='t1', project_id='p1', message='legacy original')
            original.execution_id = None
            await runtime.drain_thread_queue('t1')
            saved = runtime.queued_preflight.store.for_thread('t1')[0]
            self.assertEqual(saved.status, 'started')
            self.assertIsNone(saved.queued_turn_snapshot['execution_id'])
            self.assertEqual(saved.queued_start_receipt.requested_execution_id,
                             'reserved-queue-' + original.id)
            self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, 'turn-1')
            self.assertEqual([method for method, _ in sessions.session.requests].count('turn/start'), 1)

    async def test_preempted_admission_restores_original_identity_before_next_drain(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            host = _Host()
            project = Project(id='project', name='Project', path='/workspace/project')
            host._project = lambda _id: project
            runtime = TurnExecutionService(host)
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            runtime.start_thread_turn_now = AsyncMock(return_value={'turn': {'id':'actual-turn'}})
            runtime.schedule_queue_drain = Mock()
            original = runtime.enqueue_turn(thread_id='thread', project_id='project', message='original')
            snapshot = original.model_dump(mode='json')
            begin = runtime.queued_preflight.begin_queued_start
            def preempt(attempt_id, *, claim_id):
                _, acquired = runtime.queued_preflight.store.claim_retry(attempt_id,
                    claim_id='explicit-recovery', now=200)
                self.assertTrue(acquired)
                runtime.queued_preflight.queue_retry_committed(attempt_id, claim_id='explicit-recovery')
                return begin(attempt_id, claim_id=claim_id)
            runtime.queued_preflight.begin_queued_start = preempt
            await runtime.drain_thread_queue('thread')
            runtime.start_thread_turn_now.assert_not_awaited()
            self.assertEqual(host._thread_queue('thread')[0].model_dump(mode='json'), snapshot)
            runtime.queued_preflight.begin_queued_start = begin
            await runtime.drain_thread_queue('thread')
            runtime.start_thread_turn_now.assert_awaited_once()
            self.assertEqual(host._thread_queue('thread'), [])
            self.assertEqual(runtime.queued_preflight.store.for_thread('thread')[0].status, 'started')

    async def test_native_receipt_survives_post_start_bookkeeping_failure(self):
        from contextlib import nullcontext
        from tests.test_execution_preflight import actor
        from codex_web.models import Project
        from tests.test_turn_execution import TurnExecutionStartTests
        for receipt_write_fails in (False, True):
            with self.subTest(receipt_write_fails=receipt_write_fails), tempfile.TemporaryDirectory() as directory:
                host, binding, sessions, runtime = TurnExecutionStartTests()._service(bootstrap_thread_id='t1')
                runtime.bootstrap_bindings.initial_turn_pending = True
                await sessions.session.create_initial_native_session('t1')
                runtime.bootstrap_bindings.mark_initial_turn_started = Mock(side_effect=RuntimeError('bookkeeping unavailable'))
                project = Project(id='p1', name='Project', path='/workspace/project',
                    sandbox='workspace-write', approval_policy='on-request')
                runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                    SQLiteStateStore(Path(directory) / 'state.sqlite')))
                queued = QueuedTurn(id='original', thread_id='t1', project_id='p1',
                    execution_id='original-execution', message='original', created_at=1)
                retained = runtime.queued_preflight.retain_queued(queued,
                    organization_id=project.organization_id, workspace_id=project.workspace_id)
                _, claim = runtime.queued_preflight.claim_queued_admission(retained.id)
                runtime.queued_preflight.begin_queued_start(retained.id, claim_id=claim)
                if receipt_write_fails:
                    runtime.queued_preflight.record_queued_native_start = Mock(side_effect=RuntimeError('receipt storage unavailable'))
                # The real reader commits observation before projecting its
                # receipt, independently of the later controller bookkeeping.
                # A receipt-write outage must fail BOTH receipt boundaries;
                # preserve the positive observation rather than bypass capture.
                projection = (patch('codex_web.services.queued_native_start.reconcile_captured_start',
                    side_effect=RuntimeError('reader receipt storage unavailable'))
                    if receipt_write_fails else nullcontext())
                with projection, self.assertRaises(RuntimeError):
                    await runtime.start_thread_turn_now('t1', project=project, message='original',
                        sandbox='workspace-write', approval_policy='on-request',
                        execution_id='original-execution', source='queued:web',
                        queued_admission_attempt_id=retained.id, queued_admission_claim_id=claim)
                saved = ExecutionPreflightStore(SQLiteStateStore(Path(directory) / 'state.sqlite')).get(retained.id)
                self.assertEqual(saved.status, 'reconciliation_required' if receipt_write_fails else 'started')
                if not receipt_write_fails:
                    self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, 'turn-1')
                    self.assertEqual(saved.queued_start_receipt.requested_execution_id, 'original-execution')
                    self.assertEqual(saved.queued_start_receipt.execution_id, 'bootstrap-exec')
                self.assertEqual(host.active['t1'].turn_id, 'turn-1')
                self.assertEqual(sessions.completed, [])
                self.assertEqual([method for method, _ in sessions.session.requests].count('turn/start'), 1)
                self.assertIsNotNone(saved.queued_start_correlation)
                self.assertEqual(saved.queued_start_evidence.provider_native_turn_id, 'turn-1')
                self.assertEqual(saved.queued_turn_snapshot, retained.queued_turn_snapshot)
                if receipt_write_fails:
                    self.assertIsNone(saved.queued_start_receipt)
                    admin=actor(admin=True).model_copy(update={
                        'organization_id':project.organization_id,'workspace_id':project.workspace_id})
                    reconciled=runtime.queued_preflight.reconcile_prestart_refusal(retained.id,actor=admin)
                    self.assertEqual(reconciled.status,'started')
                    self.assertEqual(reconciled.queued_start_receipt.provider_native_turn_id,'turn-1')
                    self.assertEqual(reconciled.queued_start_evidence,saved.queued_start_evidence)
                    self.assertEqual(runtime.queued_preflight.reconcile_prestart_refusal(retained.id,actor=admin),
                        reconciled)
                    self.assertEqual([method for method, _ in sessions.session.requests].count('turn/start'),1)
                    self.assertEqual(sessions.completed,[])

    async def test_restored_queue_copy_is_reconciled_only_with_actual_start_receipt(self):
        from codex_web.models import Project, QueuedNativeStartReceipt
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            host = _Host()
            project = Project(id='project', name='Project', path='/workspace/project')
            host._project = lambda _id: project
            runtime = TurnExecutionService(host)
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            runtime.start_thread_turn_now = AsyncMock()
            runtime.schedule_queue_drain = Mock()
            queued = runtime.enqueue_turn(thread_id='thread', project_id='project', message='original')
            attempt = runtime.queued_preflight.retain_queued(queued,
                organization_id=project.organization_id, workspace_id=project.workspace_id)
            _, claim = runtime.queued_preflight.claim_queued_admission(attempt.id)
            runtime.queued_preflight.begin_queued_start(attempt.id, claim_id=claim)
            runtime.queued_preflight.record_queued_native_start(attempt.id, QueuedNativeStartReceipt(
                admission_claim_id=claim, requested_execution_id=queued.execution_id,
                execution_id='actual-execution', thread_id='thread',
                provider_native_session_id='native-session', provider_native_turn_id='actual-turn',
                provider_id='openai', runtime_id='codex', observed_at=200))
            await runtime.drain_thread_queue('thread')
            self.assertEqual(host._thread_queue('thread'), [])
            runtime.start_thread_turn_now.assert_not_awaited()
            self.assertEqual(runtime.queued_preflight.store.get(attempt.id).status, 'started')
            self.assertEqual(host.events[-1]['type'], 'queued_original_started_receipt_reconciled')

    async def test_empty_start_response_never_emits_started_receipt(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            host = _Host()
            events = []
            host._append_bot_event = events.append
            host._project = lambda _id: Project(id='project', name='Project', path='/workspace/project')
            runtime = TurnExecutionService(host)
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            runtime.start_thread_turn_now = AsyncMock(return_value={'turn': {}})
            runtime.schedule_queue_drain = Mock()
            runtime.enqueue_turn(thread_id='thread', project_id='project', message='original')
            await runtime.drain_thread_queue('thread')
            self.assertEqual(runtime.queued_preflight.store.for_thread('thread')[0].status,
                             'reconciliation_required')
            self.assertNotIn('queued_turn_started', [event['type'] for event in events])
            self.assertIn('queued_turn_start_outcome_unknown', [event['type'] for event in events])

    async def test_early_duplicate_lookup_requires_matching_original_identity(self):
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        runtime = TurnExecutionService(_Host())
        original = runtime.enqueue_turn(thread_id='thread', project_id='project', message='same',
            source='handoff', handoff_requested_at=99, repository_resource_id='repo',
            agent_profile_actor_id='actor', agent_profile_revision=6)
        identity = dict(project_id='project', handoff_requested_at=99,
            repository_resource_id='repo', agent_profile_actor_id='actor', agent_profile_revision=6)
        self.assertIs(runtime.find_duplicate_queued_turn('thread', message='same', source='handoff',
                                                       **identity), original)
        for field, changed in [('handoff_requested_at',100), ('repository_resource_id','other'),
                               ('agent_profile_actor_id','other'), ('agent_profile_revision',7)]:
            self.assertIsNone(runtime.find_duplicate_queued_turn('thread', message='same', source='handoff',
                                                                **{**identity,field:changed}))
        self.assertIsNone(runtime.find_duplicate_queued_turn('thread', message='same', source='handoff'))

    async def test_concurrent_drains_start_original_once(self):
        import asyncio
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite'
            host = _Host()
            host._project = lambda _id: Project(id='project', organization_id='org',
                workspace_id='workspace', name='Project', path='/workspace/project')
            runtimes = [TurnExecutionService(host), TurnExecutionService(host)]
            start = AsyncMock(return_value={'turn':{'id':'actual-turn'}})
            for runtime in runtimes:
                runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(database)))
                runtime.start_thread_turn_now = start
                runtime.schedule_queue_drain = Mock()
            original = runtimes[0].enqueue_turn(thread_id='thread', project_id='project', message='original')
            await asyncio.gather(*(runtime.drain_thread_queue('thread') for runtime in runtimes))
            start.assert_awaited_once()
            saved = runtimes[0].queued_preflight.store.for_thread('thread')[0]
            self.assertEqual(saved.status, 'started')
            self.assertEqual(saved.queued_turn_snapshot['id'], original.id)

    async def test_crash_after_claim_before_queue_pop_retains_recoverable_pre_start_state(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite'
            host = _Host()
            host._project = lambda _id: Project(id='project', organization_id='org',
                workspace_id='workspace', name='Project', path='/workspace/project')
            runtime = TurnExecutionService(host)
            runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(database)))
            runtime.start_thread_turn_now = AsyncMock()
            original = runtime.enqueue_turn(thread_id='thread', project_id='project', message='original')
            runtime.pop_queued_turn = Mock(side_effect=RuntimeError('crash before pop'))
            with self.assertRaises(RuntimeError):
                await runtime.drain_thread_queue('thread')
            self.assertEqual(host._thread_queue('thread'), [original])
            saved = ExecutionPreflightStore(SQLiteStateStore(database)).for_thread('thread')[0]
            self.assertEqual(saved.status, 'admitting')
            self.assertIsNone(saved.started_at)
            runtime.start_thread_turn_now.assert_not_awaited()

    async def test_retry_requeues_original_scope_and_actor_without_starting_or_replaying_unknown(self):
        from fastapi import HTTPException
        from tests.test_execution_preflight import actor
        from tests import test_turn_service as fixtures
        from codex_web.services.turns import TurnService
        with tempfile.TemporaryDirectory() as directory:
            preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            execution = SimpleNamespace(requeue_turn_front=Mock(), schedule_queue_drain=Mock(),
                active_execution_id=Mock(return_value='intended-execution'))
            service = TurnService(projects=fixtures._Projects(), settings=fixtures._Settings(),
                recovery=fixtures._Recovery(), resume_runtime=fixtures._Resume(),
                bindings=fixtures._Bindings(), queue_policy=fixtures._QueuePolicy(),
                execution=execution, event_sink=lambda _event:None,
                truncate_text=lambda value,limit:str(value)[:limit],
                binding_public=lambda binding:binding.model_dump(), preflight=preflight)
            queued = QueuedTurn(id='original', thread_id='thread', project_id='home',
                execution_id='intended-execution', message='original request', created_at=1,
                agent_profile_id='original-profile', agent_profile_revision=6,
                agent_profile_actor_id='original-requester', work_item_ref='group/repo#78',
                handoff_requested_at=99, repository_resource_id='repo',
                writable_repository_resource_ids=('repo',))
            attempt = preflight.retain_queued(queued, organization_id='org', workspace_id='ws')
            preflight.queued_disposition(attempt.id, status='blocked')
            result = await service.retry_preflight('thread', attempt.id, actor=actor(admin=True))
            self.assertTrue(result['queued'])
            self.assertEqual(execution.requeue_turn_front.call_args.args[0], queued)
            self.assertEqual(preflight.store.get(attempt.id).status, 'queued')
            self.assertIsNone(preflight.store.get(attempt.id).started_at)
            # An active marker matching the intended execution cannot turn
            # unknown admission into a successful start or a new retry.
            preflight.queued_disposition(attempt.id, status='reconciliation_required')
            with self.assertRaises(HTTPException) as caught:
                await service.retry_preflight('thread', attempt.id, actor=actor(admin=True))
            self.assertEqual(caught.exception.detail['code'], 'queued_start_reconciliation_required')
            self.assertEqual(execution.requeue_turn_front.call_count, 1)

    async def test_unresolved_original_does_not_block_later_eligible_request(self):
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        with tempfile.TemporaryDirectory() as directory:
            preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            host = _Host()
            host._project = lambda _id: Project(id='project', organization_id='org',
                workspace_id='workspace', name='Project', path='/workspace/project')
            runtime = TurnExecutionService(host)
            runtime.queued_preflight = preflight
            runtime.start_thread_turn_now = AsyncMock(return_value={'turn':{'id':'actual-turn'}})
            runtime.schedule_queue_drain = Mock()
            first = runtime.enqueue_turn(thread_id='thread', project_id='project', message='uncertain')
            second = runtime.enqueue_turn(thread_id='thread', project_id='project', message='eligible')
            attempt = preflight.retain_queued(first, organization_id='org', workspace_id='workspace')
            preflight.queued_disposition(attempt.id, status='reconciliation_required')
            await runtime.drain_thread_queue('thread')
            self.assertEqual(host._thread_queue('thread'), [first])
            self.assertEqual(runtime.start_thread_turn_now.await_args.kwargs['message'], second.message)
            self.assertEqual(preflight.store.get(attempt.id).status, 'reconciliation_required')

    async def test_known_failure_and_unknown_start_preserve_original_request(self):
        from fastapi import HTTPException
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _Host
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        errors = [(ExecutionPrestartRefusal(status_code=503, detail={
            'code':'execution_preflight_blocked','message':'source alias missing',
            'blockers':[{'code':'workspace_provisioning_blocked','message':'source alias missing',
                         'retryable':False}]}), 'blocked'),
            (HTTPException(status_code=503, detail={'code':'execution_preflight_blocked'}),
             'reconciliation_required'),
            (HTTPException(status_code=409, detail={'code':'thread_agent_profile_immutable'}),
             'reconciliation_required'),
            (RuntimeError('native start response unavailable'), 'reconciliation_required')]
        for error, expected in errors:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / 'state.sqlite'
                store = ExecutionPreflightStore(SQLiteStateStore(database))
                preflight = ExecutionPreflightService(store)
                host = _Host()
                host._project = lambda _id: Project(id='project', organization_id='org',
                    workspace_id='workspace', name='Project', path='/workspace/project')
                runtime = TurnExecutionService(host, actor_resolver=lambda *_args: SimpleNamespace(
                    identity_id='original-requester'))
                runtime.queued_preflight = preflight
                runtime.start_thread_turn_now = AsyncMock(side_effect=error)
                runtime.schedule_queue_drain = Mock()
                queued = runtime.enqueue_turn(thread_id='thread', project_id='project',
                    message='original request', work_item_ref='group/repo#78',
                    handoff_requested_at=99, repository_resource_id='repo',
                    agent_profile_id='original-profile', agent_profile_revision=6,
                    agent_profile_actor_id='original-requester', execution_id='intended-execution')
                original = queued.model_dump(mode='json')
                await runtime.drain_thread_queue('thread')
                saved = ExecutionPreflightStore(SQLiteStateStore(database)).for_thread('thread')[0]
                self.assertEqual(saved.queued_turn_snapshot, original)
                self.assertEqual(saved.status, expected)
                self.assertIsNone(saved.started_at)
                self.assertEqual(host._thread_queue_depth('thread'), 0)
                runtime.start_thread_turn_now.assert_awaited_once()

    async def test_failed_retention_leaves_original_queue_intact_and_never_starts(self):
        from codex_web.runtime.execution import TurnExecutionService
        from codex_web.models import Project
        from tests.test_turn_execution import _Host
        host = _Host()
        host._project = lambda _id: Project(id='project', name='Project', path='/workspace/project')
        runtime = TurnExecutionService(host)
        runtime.start_thread_turn_now = AsyncMock()
        queued = runtime.enqueue_turn(thread_id='thread', project_id='project', message='original')
        runtime.queued_preflight = SimpleNamespace(retain_queued=Mock(side_effect=RuntimeError('storage down')))
        with self.assertRaises(RuntimeError):
            await runtime.drain_thread_queue('thread')
        self.assertEqual(host._thread_queue('thread'), [queued])
        runtime.start_thread_turn_now.assert_not_awaited()


class QueuedPreflightRecoveryCompatibilityTests(unittest.TestCase):
    def test_encrypted_snapshot_preserves_original_and_pre_transition_restore_has_no_new_payload(self):
        from tests.test_recovery import RecoveryTests
        fixture = RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        preflight = ExecutionPreflightService(ExecutionPreflightStore(fixture.state))
        original = QueuedTurn(id='original', thread_id='thread', project_id='project',
            message='Complete original request', execution_id='original-execution', created_at=1,
            agent_profile_id='original-profile', agent_profile_revision=6,
            agent_profile_actor_id='original-actor', repository_resource_id='repo',
            writable_repository_resource_ids=('repo',), handoff_requested_at=99)
        fixture.state.record_apply('turn_queues', upserts={'thread':[original.model_dump(mode='json')]})
        source_backup = fixture.service.create_backup(actor=fixture.actor)
        attempt = preflight.retain_queued(original, organization_id='org-a', workspace_id='ws-a')
        preflight.queued_disposition(attempt.id, status='reconciliation_required')
        fixture.state.record_apply('turn_queues', upserts={}, deletes=('thread',))
        target_backup = fixture.service.create_backup(actor=fixture.actor)
        with tempfile.TemporaryDirectory() as directory:
            target = SQLiteStateStore(Path(directory) / 'target.sqlite')
            verified = fixture.service.restore_into_isolated(target_backup.id, target, actor=fixture.actor)
            self.assertEqual(verified.status, 'pass')
            self.assertFalse(verified.side_effects_enabled)
            restored = ExecutionPreflightStore(target).for_thread('thread')[0]
            self.assertEqual(restored.queued_turn_snapshot, original.model_dump(mode='json'))
            self.assertEqual(restored.status, 'reconciliation_required')
            self.assertIsNone(restored.started_at)
            rollback = SQLiteStateStore(Path(directory) / 'source.sqlite')
            verified = fixture.service.restore_into_isolated(source_backup.id, rollback, actor=fixture.actor)
            self.assertEqual(verified.status, 'pass')
            self.assertIsNone(ExecutionPreflightStore(rollback).get(attempt.id))
            self.assertEqual(rollback.record_get('turn_queues', 'thread'),
                             [original.model_dump(mode='json')])


class QueuedPreflightRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'state.sqlite'
        self.store = ExecutionPreflightStore(SQLiteStateStore(self.database))
        self.service = ExecutionPreflightService(self.store, clock=lambda: 200)
        self.queued = QueuedTurn(id='original', thread_id='thread', project_id='project',
            message='The original complete request', execution_id='intended-execution',
            work_item_ref='group/repo#78', handoff_requested_at=99,
            repository_resource_id='repo', writable_repository_resource_ids=('repo',),
            read_only_repository_resource_ids=('context',), execution_profile_id='repository-write',
            agent_profile_id='original-profile', agent_profile_revision=6,
            agent_profile_actor_id='original-requester', sandbox='workspace-write',
            approval_policy='on-request', source='work-item-handoff', created_at=100,
            reply_target=BotReplyTarget(thread_id='thread', provider='slack',
                external_conversation_id='conversation', message_id='message', updated_at=100))

    def test_proven_maintenance_refusal_recovers_original_without_starting(self):
        from tests.test_execution_preflight import actor
        from codex_web.models import ExecutionPreflightBlocker
        from fastapi import HTTPException
        attempt = self.retain()
        admin = actor(admin=True).model_copy(update={'workspace_id':'workspace'})
        member = actor(admin=False).model_copy(update={'workspace_id':'workspace'})
        refusal = ExecutionPreflightBlocker(code='upgrade_maintenance',
            message='Native turn admission is paused during upgrade maintenance', threadId=attempt.thread_id)
        for status in ['queued', 'blocked']:
            self.store.update(attempt.id, lambda current: current.model_copy(update={
                'status':status, 'blockers':(refusal,)}))
            saved = self.service.get(attempt.id, actor=admin)
            self.assertEqual(self.service.public(saved, actor=admin)['can_reconcile_prestart'], status == 'queued')
            self.assertEqual(self.service.public(saved, actor=admin)['can_retry'], status == 'blocked')
            self.assertFalse(self.service.public(saved, actor=member)['can_reconcile_prestart'])
            with self.assertRaises(HTTPException):
                self.service.reconcile_prestart_refusal(attempt.id, actor=member)
            reconciled = self.service.reconcile_prestart_refusal(attempt.id, actor=admin)
            self.assertEqual(reconciled.status, 'blocked')
            self.assertEqual(reconciled.queued_turn_snapshot, self.queued.model_dump(mode='json'))
            self.assertIsNone(reconciled.queued_start_receipt)
            self.assertIsNone(reconciled.started_at)
        recovered, claim, acquired = self.service.claim_retry(attempt.id, actor=admin)
        self.assertTrue(acquired)
        self.assertIsNotNone(claim)
        self.assertEqual(recovered.queued_turn_snapshot, self.queued.model_dump(mode='json'))

    def test_maintenance_reconciliation_rejects_other_thread_uncertainty_and_active_admission(self):
        from tests.test_execution_preflight import actor
        from codex_web.models import ExecutionPreflightBlocker
        from fastapi import HTTPException
        attempt = self.retain()
        admin = actor(admin=True).model_copy(update={'workspace_id':'workspace'})
        for status, code, thread in [
            ('queued', 'native_start_outcome_unknown', attempt.thread_id),
            ('reconciliation_required', 'upgrade_maintenance', attempt.thread_id),
            ('reconciliation_required', 'upgrade_maintenance', 'unrelated'),
            ('admitting', 'upgrade_maintenance', attempt.thread_id),
            ('reconciliation_required', 'upgrade_turn_scope_unknown', attempt.thread_id),
        ]:
            refusal = ExecutionPreflightBlocker(code=code, message='refused', threadId=thread)
            self.store.update(attempt.id, lambda current: current.model_copy(update={
                'status':status, 'blockers':(refusal,)}))
            before = self.store.get(attempt.id)
            with self.assertRaises(HTTPException):
                self.service.reconcile_prestart_refusal(attempt.id, actor=admin)
            self.assertEqual(self.store.get(attempt.id), before)

    def test_new_start_fence_clears_prior_refusal_evidence(self):
        from codex_web.models import ExecutionPreflightBlocker
        attempt = self.retain()
        refusal = ExecutionPreflightBlocker(code='upgrade_maintenance', message='maintenance', threadId=attempt.thread_id)
        self.store.update(attempt.id, lambda current: current.model_copy(update={'blockers':(refusal,), 'last_error':'maintenance'}))
        claimed, claim = self.service.claim_queued_admission(attempt.id)
        self.assertTrue(self.service.begin_queued_start(attempt.id, claim_id=claim))
        unknown = self.store.get(attempt.id)
        self.assertEqual(unknown.status, 'reconciliation_required')
        self.assertEqual(unknown.blockers, ())
        self.assertIsNone(unknown.last_error)
        self.assertFalse(self.service.recorded_maintenance_refusal(unknown))

    def test_recorded_profile_refusal_reconciliation_is_authorized_and_never_starts(self):
        from tests.test_execution_preflight import actor
        from codex_web.models import ExecutionPreflightBlocker
        from fastapi import HTTPException
        attempt = self.retain()
        refusal = ExecutionPreflightBlocker(code='thread_agent_profile_immutable', message='profile refused',
            requestedAgentProfileId='original-profile', requestedAgentProfileRevision=6,
            effectiveAgentProfile={'profile_id':'original-profile', 'profile_revision':7})
        self.store.update(attempt.id, lambda current: current.model_copy(update={
            'status':'reconciliation_required', 'blockers':(refusal,)}))
        admin = actor(admin=True).model_copy(update={'workspace_id':'workspace'})
        member = actor(admin=False).model_copy(update={'workspace_id':'workspace'})
        current = self.store.get(attempt.id)
        self.assertTrue(self.service.public(current, actor=admin)['can_reconcile_prestart'])
        self.assertFalse(self.service.public(current, actor=member)['can_reconcile_prestart'])
        with self.assertRaises(HTTPException):
            self.service.reconcile_prestart_refusal(attempt.id, actor=member)
        saved = self.service.reconcile_prestart_refusal(attempt.id, actor=admin)
        self.assertEqual(saved.status, 'blocked')
        self.assertIsNone(saved.started_at)
        self.assertIsNone(saved.queued_start_receipt)
        self.assertEqual(saved.queued_turn_snapshot, self.queued.model_dump(mode='json'))
        self.assertEqual(self.service.reconcile_prestart_refusal(attempt.id, actor=admin).status, 'blocked')
        # A concurrent actual start is terminal; reconciliation cannot erase it.
        self.service.queued_disposition(attempt.id, status='started')
        self.assertEqual(self.service.reconcile_prestart_refusal(attempt.id, actor=admin).status, 'started')

    def test_unknown_or_mismatched_refusal_cannot_be_reconciled_as_unstarted(self):
        from tests.test_execution_preflight import actor
        from codex_web.models import ExecutionPreflightBlocker
        from fastapi import HTTPException
        attempt = self.retain()
        admin = actor(admin=True).model_copy(update={'workspace_id':'workspace'})
        base = dict(code='thread_agent_profile_immutable', message='profile refused',
            requestedAgentProfileId='original-profile', requestedAgentProfileRevision=6,
            effectiveAgentProfile={'profile_id':'original-profile', 'profile_revision':7})
        for changed in [dict(code='native_start_outcome_unknown'), dict(requestedAgentProfileRevision=5),
                        dict(effectiveAgentProfile={'profile_id':'original-profile'}),
                        dict(effectiveAgentProfile='malformed'),
                        dict(effectiveAgentProfile={'profile_id':'different-profile','profile_revision':7})]:
            blocker = ExecutionPreflightBlocker(**{**base, **changed})
            self.store.update(attempt.id, lambda current: current.model_copy(update={
                'status':'reconciliation_required','blockers':(blocker,)}))
            with self.assertRaises(HTTPException):
                self.service.reconcile_prestart_refusal(attempt.id, actor=admin)
            self.assertEqual(self.store.get(attempt.id).status, 'reconciliation_required')
            self.assertFalse(self.service.public(self.store.get(attempt.id), actor=admin)['can_reconcile_prestart'])

    def test_native_receipt_rejects_wrong_claim_execution_or_thread(self):
        from codex_web.models import QueuedNativeStartReceipt
        retained = self.retain()
        _, claim = self.service.claim_queued_admission(retained.id)
        self.service.begin_queued_start(retained.id, claim_id=claim)
        fields = dict(admission_claim_id=claim, requested_execution_id='intended-execution',
            execution_id='actual-assignment-execution', thread_id='thread',
            provider_native_session_id='native-session', provider_native_turn_id='actual-turn',
            provider_id='openai', runtime_id='codex', observed_at=201)
        for key in ['admission_claim_id','requested_execution_id','thread_id']:
            with self.assertRaises(ValueError):
                self.service.record_queued_native_start(retained.id, QueuedNativeStartReceipt(**{**fields,key:'wrong'}))
        receipt = QueuedNativeStartReceipt(**fields)
        self.service.record_queued_native_start(retained.id, receipt)
        self.service.record_queued_native_start(retained.id, receipt)
        saved = ExecutionPreflightStore(SQLiteStateStore(self.database)).get(retained.id)
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_start_receipt, receipt)
        self.service.queued_disposition(retained.id, status='reconciliation_required', claim_id=claim)
        self.assertEqual(self.store.get(retained.id).status, 'started')

    def retain(self, queued=None):
        return self.service.retain_queued(queued or self.queued,
            organization_id='org', workspace_id='workspace')

    def test_entire_original_request_survives_restart_without_start_evidence(self):
        attempt = self.retain()
        reopened = ExecutionPreflightStore(SQLiteStateStore(self.database))
        saved = reopened.get(attempt.id)
        self.assertEqual(saved.queued_turn_snapshot, self.queued.model_dump(mode='json'))
        self.assertEqual(saved.status, 'queued')
        self.assertIsNone(saved.started_at)
        self.assertEqual(reopened.for_thread('thread')[0].id, attempt.id)

    def test_repeated_retention_cannot_reset_unknown_outcome_or_original_identity(self):
        attempt = self.retain()
        self.store.update(attempt.id, lambda current: current.model_copy(
            update={'status':'reconciliation_required', 'last_error':'unknown native start'}))
        self.assertEqual(self.retain().status, 'reconciliation_required')
        with self.assertRaises(ValueError):
            self.retain(self.queued.model_copy(update={'agent_profile_revision':7}))
        self.assertEqual(self.store.get(attempt.id).queued_turn_snapshot,
            self.queued.model_dump(mode='json'))

    def test_queued_and_unknown_outcome_requests_cannot_be_claimed_for_retry(self):
        attempt = self.retain()
        for status in ('queued', 'reconciliation_required'):
            self.store.update(attempt.id, lambda current: current.model_copy(update={'status':status}))
            saved, claimed = self.store.claim_retry(attempt.id, claim_id='retry', now=1000)
            self.assertFalse(claimed)
            self.assertEqual(saved.status, status)

    def test_crash_before_index_write_can_repair_index_without_replacing_request(self):
        from unittest.mock import patch
        with patch.object(self.store.store, 'record_apply', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.retain()
        saved = self.retain()
        self.assertEqual(self.store.for_thread('thread')[0].id, saved.id)
        self.assertEqual(saved.queued_turn_snapshot, self.queued.model_dump(mode='json'))

    def test_preflight_translation_preserves_explicit_retryability(self):
        from codex_web.runtime.execution import _execution_preflight_blocked
        from codex_web.services.turn_execution_binding import TurnExecutionBindingError
        for flag in (True, False, 'false'):
            with self.subTest(flag=flag):
                error = TurnExecutionBindingError('lease conflict', code='lease_conflict',
                    blocker={'code':'lease_conflict','message':'lease conflict','retryable':flag})
                translated = _execution_preflight_blocked(error)
                self.assertIs(translated.detail['retryable'], flag is True)

    def test_concurrent_claims_admit_the_original_request_once(self):
        from concurrent.futures import ThreadPoolExecutor
        attempt = self.retain()
        def claim(_index):
            service = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(self.database)))
            return service.claim_queued_admission(attempt.id)[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, range(2)))
        winner = next(value for value in claims if value is not None)
        self.assertEqual(sum(value is not None for value in claims), 1)
        self.assertTrue(self.service.begin_queued_start(attempt.id, claim_id=winner))
        self.assertFalse(self.service.begin_queued_start(attempt.id, claim_id=winner))
        self.assertEqual(self.store.get(attempt.id).queued_turn_snapshot,
            self.queued.model_dump(mode='json'))

    def test_retained_identity_lookup_preserves_nonce_profile_and_scope_distinctions(self):
        attempt = self.retain()
        self.service.queued_disposition(attempt.id, status='blocked')
        original = self.queued.model_dump(mode='json')
        repeated = dict(original, id='another-id', execution_id='new-correlation', created_at=300)
        self.assertEqual(self.store.matching_queued(repeated).id, attempt.id)
        for change in [dict(handoff_requested_at=101), dict(agent_profile_revision=7),
            dict(repository_resource_id='another-repository'), dict(agent_profile_actor_id='another-actor')]:
            with self.subTest(change=change):
                self.assertIsNone(self.store.matching_queued(repeated | change))

    def test_interrupted_queue_recovery_is_inspectable_and_claim_fenced(self):
        attempt = self.retain()
        _, original_claim = self.service.claim_queued_admission(attempt.id)
        _, acquired = self.store.claim_retry(attempt.id, claim_id='first-recovery', now=1000)
        self.assertTrue(acquired)
        self.assertEqual(self.store.for_thread('thread')[0].id, attempt.id)
        self.service.queued_disposition(attempt.id, status='blocked', claim_id=original_claim)
        self.assertEqual(self.store.get(attempt.id).status, 'retrying')
        _, acquired = self.store.claim_retry(attempt.id, claim_id='second-recovery', now=2000)
        self.assertTrue(acquired)
        self.service.queue_retry_committed(attempt.id, claim_id='first-recovery')
        self.assertEqual(self.store.get(attempt.id).status, 'retrying')
        self.service.queue_retry_committed(attempt.id, claim_id='second-recovery')
        self.assertEqual(self.store.get(attempt.id).status, 'queued')
        self.assertIsNone(self.store.get(attempt.id).started_at)
        self.assertEqual(self.store.for_thread('thread')[0].queued_turn_snapshot,
                         self.queued.model_dump(mode='json'))

    def test_recovery_fences_pre_start_claim_without_replaying_unknown_start(self):
        attempt = self.retain()
        _, original_claim = self.service.claim_queued_admission(attempt.id)
        saved, acquired = self.store.claim_retry(attempt.id, claim_id='operator-recovery', now=1000)
        self.assertTrue(acquired)
        self.assertFalse(self.service.begin_queued_start(attempt.id, claim_id=original_claim))
        self.service.queue_retry_committed(attempt.id, claim_id='operator-recovery')
        _, new_claim = self.service.claim_queued_admission(attempt.id)
        self.assertNotEqual(new_claim, original_claim)
        self.assertTrue(self.service.begin_queued_start(attempt.id, claim_id=new_claim))
        _, acquired = self.store.claim_retry(attempt.id, claim_id='unsafe-replay', now=2000)
        self.assertFalse(acquired)
        self.service.queued_disposition(attempt.id, status='blocked', claim_id=original_claim)
        self.assertEqual(self.store.get(attempt.id).status, 'reconciliation_required')
