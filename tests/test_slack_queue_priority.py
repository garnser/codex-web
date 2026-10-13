from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from codex_web.models import Project
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_turn_execution as fixtures


class SlackQueuePriorityTests(unittest.IsolatedAsyncioTestCase):
    def service(self, directory, retained=True):
        host = fixtures._Host()
        host._project = lambda _id: Project(id='p', name='Project', path='/workspace/project')
        service = TurnExecutionService(host)
        service.start_thread_turn_now = AsyncMock(return_value={'turn': {'id':'native'}})
        service.schedule_queue_drain = Mock()
        if retained:
            service.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
        return host, service

    async def test_stable_priority_projection_preserves_durable_fifo_and_snapshots(self):
        for retained in (False, True):
            with self.subTest(retained=retained), tempfile.TemporaryDirectory() as directory:
                host, service = self.service(directory, retained)
                items = [service.enqueue_turn(thread_id='t', project_id='p', message=str(i), source=source)
                    for i, source in enumerate(('gitlab', 'slack', 'web', 'queued:steer:slack', 'work-item-handoff'))]
                before = {q.id:q.model_dump(mode='json') for q in items}
                self.assertEqual([q.id for q in TurnQueuePolicy.admission_candidates(items)],
                                 [items[i].id for i in (1,3,0,2,4)])
                self.assertEqual([q.id for q in host.queues['t']], [q.id for q in items])
                for index in (1,3,0,2,4):
                    await service.drain_thread_queue('t')
                    self.assertEqual(service.start_thread_turn_now.await_args.kwargs['message'], items[index].message)
                    self.assertEqual([q.model_dump(mode='json') for q in host._thread_queue('t')],
                        [before[q.id] for q in items if q.id in {v.id for v in host._thread_queue('t')}])

    async def test_unknown_priority_request_does_not_block_eligible_work_or_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            host, service = self.service(directory)
            lower = service.enqueue_turn(thread_id='t', project_id='p', message='ready automation', source='gitlab')
            unknown = service.enqueue_turn(thread_id='t', project_id='p', message='unknown human', source='slack')
            preflight = service.queued_preflight
            attempt = preflight.retain_queued(unknown, organization_id='local', workspace_id='default')
            _, claim = preflight.claim_queued_admission(attempt.id)
            preflight.begin_queued_start(attempt.id, claim_id=claim)
            before = preflight.store.get(attempt.id).model_dump(mode='json')
            await service.drain_thread_queue('t')
            self.assertEqual(service.start_thread_turn_now.await_args.kwargs['message'], lower.message)
            self.assertEqual([q.id for q in host.queues['t']], [unknown.id])
            self.assertEqual(preflight.store.get(attempt.id).model_dump(mode='json'), before)

    async def test_priority_is_thread_local_and_never_interrupts_active_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            host, service = self.service(directory)
            first = service.enqueue_turn(thread_id='t', project_id='p', message='local', source='gitlab')
            remote = service.enqueue_turn(thread_id='other', project_id='p', message='other human', source='slack')
            await service.drain_thread_queue('t')
            self.assertEqual(service.start_thread_turn_now.await_args.kwargs['message'], first.message)
            self.assertEqual([q.id for q in host.queues['other']], [remote.id])
            service.mark_thread_active('other', turn_id='existing')
            host._release_stale_active_turn = Mock()
            service.start_thread_turn_now.reset_mock()
            await service.drain_thread_queue('other')
            service.start_thread_turn_now.assert_not_awaited()
            self.assertEqual([q.id for q in host.queues['other']], [remote.id])

    async def test_pause_and_actual_native_executor_preserve_human_priority_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            host, _binding, sessions, service = fixtures.TurnExecutionStartTests()._service()
            host._project = lambda _id: Project(id='p1', name='Project', path='/workspace/project')
            service.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
                SQLiteStateStore(Path(directory) / 'state.sqlite')))
            service.automated_admission_allowed = lambda: False
            service.schedule_queue_drain = Mock()
            automatic = service.enqueue_turn(thread_id='t1', project_id='p1', message='automatic', source='gitlab')
            web = service.enqueue_turn(thread_id='t1', project_id='p1', message='web human', source='web')
            slack = service.enqueue_turn(thread_id='t1', project_id='p1', message='Slack human', source='slack')
            before = [q.model_dump(mode='json') for q in (automatic, web)]
            await service.drain_thread_queue('t1')
            self.assertEqual([q.model_dump(mode='json') for q in host.queues['t1']], before)
            native = [params for method, params in sessions.session.requests if method=='turn/start']
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0]['threadId'], 't1')
            self.assertIn(slack.message, native[0]['input'][0]['text'])
            self.assertEqual(service.queued_preflight.store.for_thread('t1')[0].queued_turn_snapshot,
                             slack.model_dump(mode='json') | {'attempts':0})

    async def test_maintenance_deferral_restores_original_middle_position_and_counter(self):
        for retained in (False, True):
            with self.subTest(retained=retained), tempfile.TemporaryDirectory() as directory:
                host, service = self.service(directory, retained)
                items = [service.enqueue_turn(thread_id='t', project_id='p', message=str(i), source=source)
                         for i,source in enumerate(('gitlab','slack','web'))]
                before = [q.model_dump(mode='json') for q in items]
                service.start_thread_turn_now = AsyncMock(side_effect=HTTPException(status_code=409,
                    detail={'code':'upgrade_maintenance','message':'maintenance'}))
                await service.drain_thread_queue('t')
                self.assertEqual([q.model_dump(mode='json') for q in host.queues['t']], before)
                self.assertEqual(service.start_thread_turn_now.await_args.kwargs['source'], 'queued:slack')
                if retained:
                    attempt = service.queued_preflight.store.for_thread('t')[0]
                    self.assertEqual(attempt.status, 'queued')
                    self.assertEqual(attempt.queued_turn_snapshot, before[1])

    def test_protocol_origin_only_grants_slack_scheduling_preference(self):
        for source in ('slack','queued:slack','steer:queued:slack'):
            self.assertEqual(TurnQueuePolicy.admission_priority(source), 'human_slack')
        for source in ('gitlab','work-item-handoff','slack:bot','conversation-channel:slack:instance','queued:slack:other'):
            self.assertEqual(TurnQueuePolicy.admission_priority(source), 'standard')

    async def test_restart_uses_the_same_priority_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            host, service = self.service(directory)
            automatic = service.enqueue_turn(thread_id='t', project_id='p', message='automatic', source='gitlab')
            human = service.enqueue_turn(thread_id='t', project_id='p', message='human', source='slack')
            restarted = TurnExecutionService(host)
            restarted.queued_preflight = service.queued_preflight
            restarted.start_thread_turn_now = AsyncMock(return_value={'turn':{'id':'native'}})
            restarted.schedule_queue_drain = Mock()
            await restarted.resume_active_threads_after_startup()
            restarted.schedule_queue_drain.assert_called_with('t')
            await restarted.drain_thread_queue('t')
            self.assertEqual(restarted.start_thread_turn_now.await_args.kwargs['message'], human.message)
            self.assertEqual([q.id for q in host.queues['t']], [automatic.id])

    async def test_worker_capacity_deferral_keeps_priority_request_at_original_position(self):
        from codex_web.services.local_execution_worker import LocalExecutionWorkerCapacityError
        with tempfile.TemporaryDirectory() as directory:
            host, service = self.service(directory)
            items = [service.enqueue_turn(thread_id='t', project_id='p', message=str(i), source=source)
                     for i,source in enumerate(('gitlab','slack','web'))]
            before = [q.model_dump(mode='json') for q in items]
            service.start_thread_turn_now = AsyncMock(side_effect=LocalExecutionWorkerCapacityError('worker full'))
            await service.drain_thread_queue('t')
            self.assertEqual([q.model_dump(mode='json') for q in host.queues['t']], before)
            self.assertEqual(service.queued_preflight.store.for_thread('t')[0].status, 'queued')

    async def test_priority_keeps_full_delivery_profile_repository_and_execution_fields(self):
        from codex_web.models import BotReplyTarget
        with tempfile.TemporaryDirectory() as directory:
            host, service = self.service(directory)
            service.actor_resolver = Mock(return_value=object())
            automatic = service.enqueue_turn(thread_id='t', project_id='p', message='automatic', source='gitlab')
            human = service.enqueue_turn(thread_id='t', project_id='p', message='human', source='slack',
                reply_target=BotReplyTarget(provider='slack',external_conversation_id='channel',thread_id='t',updated_at=1.0),
                repository_resource_id='repo',writable_repository_resource_ids=('repo',),
                read_only_repository_resource_ids=('docs',),execution_profile_id='repository-write',
                agent_profile_id='profile',agent_profile_revision=7,agent_profile_actor_id='actor',
                work_item_ref='issue-1',handoff_requested_at=123.0)
            snapshot=human.model_dump(mode='json')
            await service.drain_thread_queue('t')
            call=service.start_thread_turn_now.await_args.kwargs
            for field in ('reply_target','repository_resource_id','writable_repository_resource_ids',
                          'read_only_repository_resource_ids','execution_profile_id','agent_profile_id',
                          'agent_profile_revision','agent_profile_actor_id','execution_id','work_item_ref',
                          'handoff_requested_at'):
                self.assertEqual(call[field], getattr(human,field))
            self.assertEqual(service.queued_preflight.store.for_thread('t')[0].queued_turn_snapshot,snapshot)
            self.assertEqual([q.id for q in host.queues['t']], [automatic.id])
            service.actor_resolver.assert_called_once()

    async def test_slack_live_and_backfill_bot_messages_never_reach_priority_intake(self):
        from tests import test_slack_provider as slack_fixtures
        with tempfile.TemporaryDirectory() as directory:
            host=slack_fixtures._Host(Path(directory))
            routing=slack_fixtures._Routing()
            intake=slack_fixtures._service(host,slack_fixtures._Slack(),routing)
            for index,bot_fields in enumerate(({'bot_id':'B1'},{'subtype':'bot_message'})):
                event={'type':'message','channel':'C1','text':'claim human priority','ts':str(index+1),
                       'user':'UBOT',**bot_fields}
                result=await intake.handle_webhook(slack_fixtures._request({'type':'event_callback','event':event}))
                self.assertTrue(result['ignored'])
                dispatched=await intake._dispatch_backfill_message(host.connection,'C1',event,
                    recent_message_ids=set())
                self.assertFalse(dispatched)
            self.assertEqual(routing.messages,[])
            await intake.handle_webhook(slack_fixtures._request({'type':'event_callback','event':{
                'type':'message','channel':'C1','text':'human request','ts':'3','user':'UHUMAN'}}))
            self.assertEqual(len(routing.messages),1)
            self.assertEqual(TurnQueuePolicy.admission_priority(routing.messages[0].provider),'human_slack')
