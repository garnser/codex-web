from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi import HTTPException

from codex_web.models import Project, QueuedTurn, BotReplyTarget
from codex_web.identity import AuthenticationActor
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests.test_turn_execution import _Host


class MaintenanceQueueOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'state.sqlite'
        self.mirror = Path(self.temp.name) / 'queues.json'
        state = SQLiteStateStore(self.database)
        self.queue = TurnQueueRepository(state, self.mirror)
        self.host = _Host()
        self.project = Project(id='project-fixture', name='Fixture', path='/workspace/fixture')
        self.host._project = lambda _id: self.project
        self.host._thread_queue_record = self.queue.get
        self.host._update_thread_queue_record = self.queue.update
        self.host._thread_queue = self.queue.get
        self.host._thread_queue_depth = lambda thread: len(self.queue.get(thread))
        self.runtime = TurnExecutionService(self.host)
        self.runtime.schedule_queue_drain = Mock()
        self.actor = AuthenticationActor(identity_id="fixture-admin", principal_kind="human",
            organization_id="local", workspace_id="default", roles=("admin",), assurance="local_trusted")
        self.runtime.actor_resolver = lambda identity_id, project: self.actor
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(state))
        self.runtime.queued_preflight = self.preflight
        # Actual incident identifiers and creation times; bodies and scope are
        # sanitized fixtures. Their injected dispositions are not live evidence.
        self.held = QueuedTurn(id='b843d0a3504e', thread_id='thread-fixture',
            project_id=self.project.id, message='Sanitized held original',
            execution_id='held-fixture', created_at=1791778009.4276829)
        self.ready = QueuedTurn(id='bbf52558a87e', thread_id='thread-fixture',
            project_id=self.project.id, message='Sanitized later original',
            execution_id='ready-fixture', created_at=1791782056.4454837)
        for name in ('held', 'ready'):
            item = getattr(self, name)
            setattr(self, name, item.model_copy(update={
                'agent_profile_id':'fixture-profile', 'agent_profile_revision':7,
                'agent_profile_actor_id':self.actor.identity_id,
                'execution_profile_id':'fixture-execution',
                'repository_resource_id':'fixture-repository',
                'writable_repository_resource_ids':('fixture-repository',),
                'read_only_repository_resource_ids':('fixture-context',),
                'sandbox':'workspace-write', 'approval_policy':'on-request',
                'model':'fixture-model', 'reasoning_effort':'high',
                'work_item_ref':'fixture/project#1',
                'reply_target':BotReplyTarget(thread_id=item.thread_id, provider='slack',
                    external_conversation_id='fixture-channel', external_thread_id='fixture-reply',
                    message_id='fixture-message', updated_at=item.created_at)}))
        self.queue.put('thread-fixture', [self.held, self.ready])
        first = self.preflight.retain_queued(self.held, organization_id='local', workspace_id='default')
        _, claim = self.preflight.claim_queued_admission(first.id)
        self.preflight.begin_queued_start(first.id, claim_id=claim)
        self.held_attempt_id = first.id
        self.held_before = self.preflight.store.get(first.id).model_dump(mode='json')

    def tearDown(self):
        self.temp.cleanup()

    def reopened_queue(self):
        return TurnQueueRepository(SQLiteStateStore(self.database), self.mirror).get('thread-fixture')

    async def test_cancelled_later_admission_preserves_held_pair_across_restart(self):
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.runtime.start_thread_turn_now = pending
        task = asyncio.create_task(self.runtime.drain_thread_queue('thread-fixture'))
        await asyncio.wait_for(entered.wait(), timeout=5)
        self.assertEqual([q.id for q in self.queue.get('thread-fixture')], [self.held.id])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([q.model_dump(mode='json') for q in self.reopened_queue()],
            [self.held.model_dump(mode='json'), self.ready.model_dump(mode='json')])
        self.assertEqual(self.preflight.store.get(self.held_attempt_id).model_dump(mode='json'), self.held_before)
        later = self.preflight.store.get('preflight-queued-'+self.ready.id)
        self.assertEqual(later.status, 'reconciliation_required')
        self.assertIsNone(later.queued_start_receipt)
        _, claim = self.preflight.claim_queued_admission(later.id)
        self.assertIsNone(claim, 'Cancellation cannot prove a negative native outcome')

    async def test_deferral_preserves_concurrent_explicit_priority_and_new_arrival(self):
        priority = self.ready.model_copy(update={'id':'explicit-priority', 'message':'Authorized retry fixture',
            'execution_id':'priority-fixture'})
        async def refused(*args, **kwargs):
            self.runtime.requeue_turn_front(priority)
            self.late = self.runtime.enqueue_turn(thread_id='thread-fixture', project_id=self.project.id,
                message='New ordinary arrival')
            raise HTTPException(status_code=409, detail={'code':'upgrade_maintenance',
                'threadId':'thread-fixture', 'message':'Canonical controller pre-start refusal'})
        self.runtime.start_thread_turn_now = refused
        await self.runtime.drain_thread_queue('thread-fixture')
        self.assertEqual([q.model_dump(mode='json') for q in self.reopened_queue()],
            [q.model_dump(mode='json') for q in (priority, self.held, self.ready, self.late)])
        self.assertEqual(self.preflight.store.get(self.held_attempt_id).model_dump(mode='json'), self.held_before)
        later = self.preflight.store.get('preflight-queued-'+self.ready.id)
        self.assertEqual(later.status, 'queued')
        self.assertIsNone(later.queued_start_receipt)
        self.assertEqual(later.queued_turn_snapshot, self.ready.model_dump(mode='json'))

    async def test_single_deferred_original_stays_ahead_of_new_ordinary_arrival(self):
        self.queue.put('thread-fixture', [self.ready])
        async def refused(*args, **kwargs):
            self.late = self.runtime.enqueue_turn(thread_id='thread-fixture', project_id=self.project.id,
                message='New ordinary arrival')
            raise HTTPException(status_code=409, detail={'code':'upgrade_maintenance',
                'threadId':'thread-fixture', 'message':'Canonical pre-start refusal'})
        self.runtime.start_thread_turn_now = refused
        await self.runtime.drain_thread_queue('thread-fixture')
        self.assertEqual([q.id for q in self.reopened_queue()], [self.ready.id, self.late.id])

    async def test_single_deferred_original_keeps_authorized_retry_priority(self):
        from codex_web.identity import AuthenticationActor
        self.queue.put('thread-fixture', [self.ready])
        priority = self.ready.model_copy(update={'id':'explicit-priority', 'execution_id':'priority-fixture'})
        original = self.preflight.retain_queued(priority, organization_id='local', workspace_id='default')
        _, claim = self.preflight.claim_queued_admission(original.id)
        self.preflight.queued_disposition(original.id, status='blocked', claim_id=claim)
        actor = AuthenticationActor(identity_id='fixture-admin', principal_kind='human',
            organization_id='local', workspace_id='default', roles=('admin',), assurance='local_trusted')
        async def refused(*args, **kwargs):
            _, claim, acquired = self.preflight.claim_retry(original.id, actor=actor)
            self.assertTrue(acquired)
            self.runtime.requeue_turn_front(priority)
            self.preflight.queue_retry_committed(original.id, claim_id=claim)
            self.late = self.runtime.enqueue_turn(thread_id='thread-fixture', project_id=self.project.id,
                message='New ordinary arrival')
            raise HTTPException(status_code=409, detail={'code':'upgrade_maintenance',
                'threadId':'thread-fixture', 'message':'Canonical pre-start refusal'})
        self.runtime.start_thread_turn_now = refused
        await self.runtime.drain_thread_queue('thread-fixture')
        self.assertEqual([q.id for q in self.reopened_queue()], [priority.id, self.ready.id, self.late.id])
        self.assertTrue(self.preflight.has_explicit_queue_priority(priority))
        wrong = priority.model_copy(update={'project_id':'foreign-project'})
        self.assertFalse(self.preflight.has_explicit_queue_priority(wrong))

    async def test_candidate_arriving_between_queue_reads_uses_atomic_removal_order(self):
        self.runtime.queued_preflight = None
        self.queue.put('thread-fixture', [self.ready])
        reads = 0
        def queue_with_arrival(thread):
            nonlocal reads
            current = self.queue.get(thread)
            reads += 1
            if reads == 1:
                self.arrival = self.runtime.enqueue_turn(thread_id=thread, project_id=self.project.id,
                    message='Authenticated Slack fixture arrival', source='slack')
            return current
        self.host._thread_queue = queue_with_arrival
        async def refused(*args, **kwargs):
            self.assertEqual(kwargs['message'], self.arrival.message)
            raise HTTPException(status_code=409, detail={'code':'upgrade_maintenance',
                'threadId':'thread-fixture', 'message':'Canonical pre-start refusal'})
        self.runtime.start_thread_turn_now = refused
        await self.runtime.drain_thread_queue('thread-fixture')
        self.assertEqual([q.model_dump(mode='json') for q in self.reopened_queue()],
            [self.ready.model_dump(mode='json'), self.arrival.model_dump(mode='json')])
