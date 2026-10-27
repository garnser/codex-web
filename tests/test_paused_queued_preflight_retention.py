"""Paused queue retention is inspection provenance, never START authority."""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from codex_web.definitions import DefinitionReference
from codex_web.models import BotBinding
from codex_web.owner_routing import OwnerRoutingBinding, OwnerRoutingPin
from codex_web.services.bot_event_dispatch import BotEventDispatchService
from codex_web.services.queued_operator_admission import QueuedOperatorAdmissionService
from codex_web.services.projects import ProjectNotFoundError
from codex_web.storage.turn_queue import TurnQueueRepository
from tests import test_notification_batch_runtime as batch_fixtures
from tests import test_turn_execution as turn_fixtures


class PausedQueuedPreflightRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        f = batch_fixtures.NotificationBatchRuntimeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.host, _, self.sessions, self.runtime = turn_fixtures.TurnExecutionStartTests()._service()
        self.queues = TurnQueueRepository(f.state, Path(f.temp.name) / 'queues.json')
        self.host._thread_queue = self.queues.get
        self.host._thread_queue_record = self.queues.get
        self.host._update_thread_queue_record = self.queues.update
        self.host._project = lambda _: f.project
        self.host._release_stale_active_turn = lambda *args: False
        self.runtime.queued_preflight = f.preflight
        self.runtime.automated_admission_allowed = lambda: False
        self.scheduled_drains = []
        schedule = self.runtime.schedule_queue_drain
        def observe_schedule(thread):
            schedule(thread)
            self.scheduled_drains.append(self.runtime.queue_drain_tasks[thread])
        self.runtime.schedule_queue_drain = observe_schedule
        self.addAsyncCleanup(self.settle_drains)
        self.operator = QueuedOperatorAdmissionService(preflight=f.preflight,
            runtime=self.runtime, ownership=None, authority=f.authority,
            resources=f.resources, actor_resolver=f.controls.actor_resolver,
            replacement_resolver=lambda *args, **kwargs: None)

    async def settle_drains(self):
        tasks = set(self.scheduled_drains)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)

    def enqueue(self, **updates):
        values = dict(thread_id='thread-a', project_id=self.f.project.id,
            message='Fresh meaningful current Work facts', source='work-item-progress',
            work_item_ref=self.f.items[0].work_item_ref,
            repository_resource_id=self.f.repo.id,
            writable_repository_resource_ids=(self.f.repo.id,),
            execution_profile_id=self.f.profile.execution_profile_id,
            agent_profile_id=self.f.profile.profile_id,
            agent_profile_revision=self.f.profile.revision,
            agent_profile_actor_id=self.f.actor.identity_id)
        values.update(updates)
        return self.runtime.enqueue_turn(**values)

    def attempt(self, original):
        return self.f.preflight.store.get('preflight-queued-' + original.id)

    def preview(self, original, **updates):
        values = dict(thread_id='thread-a', project=self.f.project, actor=self.f.actor)
        values.update(updates)
        return self.operator.preview('preflight-queued-' + original.id, **values)

    async def test_pinned_work_dispatch_schedules_real_drain_and_operator_preview(self):
        # The dispatch owner supplies intent/profile metadata; this pin grants no
        # authority and no native transport is invoked anywhere in this fixture.
        binding = BotBinding(id='fixture-binding', provider='slack',
            external_conversation_id='fixture-conversation', thread_id='thread-a',
            project_id=self.f.project.id, created_at=1, updated_at=1)
        pin = OwnerRoutingPin(binding=OwnerRoutingBinding(
            organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id, project_id=self.f.project.id,
            configured_owner='master', canonical_owner='orchestrator',
            definition=DefinitionReference(definition_id='execution-roles.default',
                kind='execution-role-catalog', revision=1, record_id='fixture-role-record',
                checksum='a' * 64, definition_schema_version='1.0')),
            work_item_ref=self.f.items[0].work_item_ref, stage='implementation_active',
            retry_attempt=3, bot_binding_id=binding.id, original_thread_id='thread-a')
        dispatch = BotEventDispatchService(
            projects=SimpleNamespace(get=lambda _: self.f.project),
            settings=SimpleNamespace(get=lambda _: SimpleNamespace(model=None,
                reasoning_effort=None, execution_profile_id=self.f.profile.execution_profile_id)),
            targets=SimpleNamespace(event_reply_target=lambda _: None,
                conversation_target=lambda _: None), bindings=SimpleNamespace(upsert=Mock()),
            execution=self.runtime, queue_policy=SimpleNamespace(
                depth=lambda thread: len(self.queues.get(thread)), queue=self.queues.get),
            recovery=SimpleNamespace(), resume=SimpleNamespace(),
            telemetry=SimpleNamespace(append=Mock()), publish_event=AsyncMock(),
            binding_name=lambda _: 'orchestrator', execution_profiles=self.f.execution_profiles)
        dispatch.owner_routing_context = lambda *_: (self.f.profile, self.f.actor)
        result = await dispatch.dispatch(binding, 'Fresh Work facts with governed scope',
            'work-item-progress', work_item_ref=pin.work_item_ref,
            repository_resource_id=self.f.repo.id,
            writable_repository_resource_ids=(self.f.repo.id,),
            require_idle=True, owner_routing_pin=pin)
        self.assertTrue(result['queued'])
        # Exercise the producer's actual scheduling, not a direct private drain.
        self.assertEqual(len(self.scheduled_drains), 1)
        task = self.scheduled_drains[0]
        await asyncio.wait_for(asyncio.shield(task), 5)
        original = self.queues.get('thread-a')[0]
        attempt = self.attempt(original)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.queued_turn_snapshot, original.model_dump(mode='json'))
        preview = self.preview(original)
        self.assertEqual(preview['queued_turn_id'], original.id)
        self.assertEqual(preview['original_actor_id'], self.f.actor.identity_id)
        self.assertFalse(preview['automation_will_resume'])
        self.assertEqual(original.source, 'work-item-progress')
        self.assertEqual(original.owner_routing_pin, pin)
        self.assertEqual(original.work_item_ref, pin.work_item_ref)
        self.assertEqual(original.attempts, 0)
        self.assertFalse(self.sessions.session.requests)

    async def test_pause_precedes_batch_claim_and_unknown_reconciliation(self):
        old = self.f.items[0]
        attempt = self.f.attempts[old.id]
        _, claim = self.f.preflight.claim_queued_admission(attempt.id)
        self.f.preflight.begin_queued_start(attempt.id, claim_id=claim)
        before = self.f.preflight.store.get(attempt.id).model_dump(mode='json')
        self.queues.put('thread-a', [old])
        original = self.enqueue()
        queue = [item.model_dump(mode='json') for item in self.queues.get('thread-a')]
        self.runtime.notification_batch_handler = SimpleNamespace(try_drain=AsyncMock())
        claim_spy = Mock(wraps=self.f.preflight.claim_queued_admission)
        self.f.preflight.claim_queued_admission = claim_spy
        await self.runtime.drain_thread_queue('thread-a')
        self.assertEqual(self.f.preflight.store.get(attempt.id).model_dump(mode='json'), before)
        self.assertEqual([item.model_dump(mode='json') for item in self.queues.get('thread-a')], queue)
        self.assertIsNotNone(self.attempt(original))
        self.runtime.notification_batch_handler.try_drain.assert_not_called()
        claim_spy.assert_not_called()
        self.assertFalse(self.sessions.session.requests)

    async def test_unknown_project_creates_no_attempt_or_queue_mutation(self):
        original = self.enqueue(project_id='missing-project')
        for error in (HTTPException(status_code=404, detail='Project unavailable'),
                      ProjectNotFoundError('Project not found')):
            with self.subTest(error=type(error).__name__):
                def missing(_, error=error):
                    raise error
                self.host._project = missing
                await self.runtime.drain_thread_queue('thread-a')
                self.assertIsNone(self.attempt(original))
                self.assertEqual(self.queues.get('thread-a')[0], original)
        self.host._project = Mock(side_effect=RuntimeError('Unexpected storage failure'))
        with self.assertRaisesRegex(RuntimeError, 'Unexpected storage failure'):
            await self.runtime.drain_thread_queue('thread-a')
        self.assertIsNone(self.attempt(original))
        self.assertEqual(self.queues.get('thread-a')[0], original)
        self.assertFalse(self.sessions.session.requests)

    async def test_retained_pause_does_not_bypass_tenant_or_changed_copy_guards(self):
        original = self.enqueue()
        await self.runtime.drain_thread_queue('thread-a')
        before = self.attempt(original).model_dump(mode='json')
        with self.assertRaises(HTTPException):
            self.preview(original, actor=self.f.actor.model_copy(update={'organization_id': 'foreign'}))
        self.queues.update('thread-a', lambda items: [item.model_copy(
            update={'agent_profile_revision': item.agent_profile_revision + 1}) for item in items])
        changed_queue = [item.model_dump(mode='json') for item in self.queues.get('thread-a')]
        await self.runtime.drain_thread_queue('thread-a')
        self.assertEqual([item.model_dump(mode='json') for item in self.queues.get('thread-a')], changed_queue)
        with self.assertRaises(HTTPException):
            self.preview(original)
        self.assertEqual(self.attempt(original).model_dump(mode='json'), before)
        self.assertFalse(self.sessions.session.requests)

    async def test_repeated_drain_preserves_snapshot_order_and_requires_original_profile(self):
        first = self.enqueue(message='First exact original')
        second = self.enqueue(message='Second exact original', agent_profile_actor_id=None)
        await self.runtime.drain_thread_queue('thread-a')
        before = [self.attempt(item).model_dump(mode='json') for item in (first, second)]
        await self.runtime.drain_thread_queue('thread-a')
        self.assertEqual([self.attempt(item).model_dump(mode='json') for item in (first, second)], before)
        self.assertEqual([item.id for item in self.queues.get('thread-a')], [first.id, second.id])
        self.assertFalse(self.preview(first)['automation_will_resume'])
        with self.assertRaises(HTTPException):
            self.preview(second)
        self.assertFalse(self.sessions.session.requests)

    async def test_unexpected_retention_storage_error_propagates_without_admission(self):
        original = self.enqueue()
        self.f.preflight.store.retain_queued = Mock(
            side_effect=ValueError('Unexpected retention storage failure'))
        with self.assertRaisesRegex(ValueError, 'Unexpected retention storage failure'):
            await self.runtime.drain_thread_queue('thread-a')
        self.assertIsNone(self.attempt(original))
        self.assertEqual(self.queues.get('thread-a')[0], original)
        self.assertFalse(self.sessions.session.requests)
