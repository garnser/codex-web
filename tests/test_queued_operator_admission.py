import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import QueuedOperatorAdmissionRequest
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.queued_operator_admission import QueuedOperatorAdmissionService
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_notification_batch_runtime as batch_fixtures
from tests import test_turn_execution as turn_fixtures


class QueuedOperatorAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        f = batch_fixtures.NotificationBatchRuntimeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.addCleanup(f.temp.cleanup)
        self.f = f
        self.queues = TurnQueueRepository(f.state, Path(f.temp.name)/'queues.json')
        self.queues.put('thread-a', f.items)
        self.host, _, self.sessions, self.runtime = turn_fixtures.TurnExecutionStartTests()._service()
        self.host._thread_queue = self.queues.get
        self.host._thread_queue_record = self.queues.get
        self.host._update_thread_queue_record = self.queues.update
        self.host._project = lambda _: f.project
        self.host._release_stale_active_turn = lambda *args: False
        self.runtime.queued_preflight = f.preflight
        self.runtime.actor_resolver = f.controls.actor_resolver
        self.runtime.automated_admission_allowed = lambda: False
        self.runtime.schedule_queue_drain = MagicMock()
        self.ownership = ReplicatedOwnershipService(StateStoreCoordinationBackend(f.state), instance_id='test-original')
        self.runtime.ownership = self.ownership
        profile = AgentProfileExecutionBinding(profile_id=f.profile.profile_id,
            profile_revision=f.profile.revision, profile_record_id=f.profile.record_id,
            selected_provider_id='openai', selected_runtime_id='codex')
        route = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex', capability_revision=1)
        self.runtime._select_runtime_binding = AsyncMock(return_value=(route, profile))
        assignment = self.sessions.session.validate_current()
        assignment.agent_profile = profile
        assignment.runtime_binding = route
        self.sessions.session.validate_current = MagicMock(return_value=assignment)
        self.attempt = f.preflight.retain_queued(f.items[1], organization_id='local', workspace_id='default')
        self.service = QueuedOperatorAdmissionService(preflight=f.preflight, runtime=self.runtime,
            ownership=self.ownership, authority=f.authority, resources=f.resources,
            actor_resolver=f.controls.actor_resolver, replacement_resolver=lambda *args, **kwargs:None)

    def preview(self, **updates):
        args=dict(thread_id='thread-a', project=self.f.project, actor=self.f.actor);args.update(updates)
        return self.service.preview(self.attempt.id, **args)

    def request(self):
        preview=self.preview()
        return QueuedOperatorAdmissionRequest(expected_snapshot_sha256=preview['snapshot_sha256'],
            expected_queue_sha256=preview['queue_sha256'], reason='Reviewed synthetic original')

    async def admit(self, payload=None, **updates):
        args=dict(thread_id='thread-a', project=self.f.project, actor=self.f.actor);args.update(updates)
        return await self.service.admit(self.attempt.id, payload or self.request(), **args)

    def starts(self):
        return [params for method, params in self.sessions.session.requests if method=='turn/start']

    async def test_one_real_runtime_start_preserves_source_scope_and_other_unknowns(self):
        unknown = self.f.preflight.retain_queued(self.f.items[0], organization_id='local', workspace_id='default')
        _, claim = self.f.preflight.claim_queued_admission(unknown.id)
        self.f.preflight.begin_queued_start(unknown.id, claim_id=claim)
        before=self.f.preflight.store.get(unknown.id).model_dump(mode='json')
        original=self.attempt.queued_turn_snapshot
        result=await self.admit()
        self.assertTrue(result['ok'])
        self.assertFalse(result['automationResumed'])
        self.assertEqual(len(self.starts()),1)
        self.assertIn(self.f.items[1].message, self.starts()[0]['input'][0]['text'])
        self.assertEqual([q.id for q in self.queues.get('thread-a')], [self.f.items[0].id,self.f.items[2].id])
        actual=self.f.preflight.store.get(self.attempt.id)
        self.assertEqual(actual.queued_turn_snapshot, original)
        self.assertEqual(actual.operator_admission.requested_by, self.f.actor.identity_id)
        self.assertEqual(self.host.active['thread-a'].agent_profile.profile_revision,self.f.profile.revision)
        self.assertEqual(self.f.preflight.store.get(unknown.id).model_dump(mode='json'),before)
        self.runtime.schedule_queue_drain.assert_not_called()
        with self.assertRaises(HTTPException):await self.admit()
        self.assertEqual(len(self.starts()),1)

    async def test_preview_is_read_only_and_ordinary_drain_remains_paused(self):
        before=self.f.preflight.store.get(self.attempt.id).model_dump(mode='json')
        self.assertFalse(self.preview()['automation_will_resume'])
        await self.runtime.drain_thread_queue('thread-a')
        self.assertEqual(self.f.preflight.store.get(self.attempt.id).model_dump(mode='json'),before)
        self.assertEqual(len(self.starts()),0)
        self.assertEqual(len(self.queues.get('thread-a')),3)

    async def test_denied_mfa_tenant_project_and_current_authority_do_not_consume(self):
        request=self.request()
        for actor in (self.f.actor.model_copy(update={'assurance':'primary'}),
                      self.f.actor.model_copy(update={'organization_id':'foreign'}),
                      self.f.actor.model_copy(update={'roles':()})):
            with self.subTest(actor=actor):
                with self.assertRaises(HTTPException):await self.admit(request, actor=actor)
        with self.assertRaises(HTTPException):await self.admit(request, project=self.f.project.model_copy(update={'id':'foreign'}))
        original_resolver=self.service.actor_resolver
        self.service.actor_resolver=lambda *args:self.f.actor.model_copy(update={'roles':()})
        with self.assertRaises(HTTPException):await self.admit(request)
        self.service.actor_resolver=original_resolver
        self.assertEqual(len(self.queues.get('thread-a')),3)
        self.assertEqual(self.f.preflight.store.get(self.attempt.id).status,'queued')
        self.assertEqual(len(self.starts()),0)

    async def test_stale_queue_and_snapshot_refuse_without_mutation(self):
        request=self.request()
        self.queues.update('thread-a', lambda items:list(reversed(items)))
        with self.assertRaises(HTTPException):await self.admit(request)
        self.assertEqual(len(self.starts()),0)
        self.assertEqual(self.f.preflight.store.get(self.attempt.id).status,'queued')
        request=self.request().model_copy(update={'expected_snapshot_sha256':'0'*64})
        with self.assertRaises(HTTPException):await self.admit(request)

    async def test_changed_missing_duplicate_or_batch_owned_original_refuses(self):
        originals=self.queues.get('thread-a')
        for items in ([originals[0], originals[2]], originals+[originals[1]],
                      [originals[0],originals[1].model_copy(update={'source':'operator-retry'}),originals[2]]):
            self.queues.put('thread-a',items)
            with self.assertRaises(HTTPException):self.preview()
        self.queues.put('thread-a',originals)
        self.f.preflight.store.update(self.attempt.id, lambda a:a.model_copy(update={'notification_batch_id':'batch'}))
        with self.assertRaises(HTTPException):self.preview()
        self.assertEqual(len(self.starts()),0)

    async def test_competing_owner_refuses_without_claiming(self):
        other=ReplicatedOwnershipService(StateStoreCoordinationBackend(self.f.state),instance_id='other')
        other.acquire('thread-queue:thread-a')
        with self.assertRaises(HTTPException):await self.admit()
        self.assertEqual(self.f.preflight.store.get(self.attempt.id).status,'queued')
        self.assertEqual(len(self.starts()),0)

    async def test_revocation_during_setup_restores_original_without_native_start(self):
        request=self.request()
        before=[q.model_dump(mode='json') for q in self.queues.get('thread-a')]
        start=self.sessions.start
        async def revoke(assignment_id):
            session=await start(assignment_id)
            self.service.actor_resolver=lambda *args:self.f.actor.model_copy(update={'roles':()})
            return session
        self.sessions.start=revoke
        result=await self.admit(request)
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.starts()),0)
        self.assertEqual([q.model_dump(mode='json') for q in self.queues.get('thread-a')],before)
        self.assertEqual(result['attempt']['status'],'blocked')

    async def test_lost_response_and_restart_cannot_replay_operator_intent(self):
        async def lost_response(*args,**kwargs):raise TimeoutError('synthetic lost native response')
        self.sessions.session.request=lost_response
        result=await self.admit()
        self.assertFalse(result['ok'])
        self.assertEqual(result['attempt']['status'],'reconciliation_required')
        reopened=ExecutionPreflightStore(SQLiteStateStore(Path(self.f.temp.name)/'state.sqlite'))
        retained=reopened.get(self.attempt.id)
        self.assertEqual(retained.operator_admission,self.f.preflight.store.get(self.attempt.id).operator_admission)
        with self.assertRaises(HTTPException):self.preview()
        self.assertEqual(len(self.queues.get('thread-a')),2)

    async def test_concurrent_operator_requests_and_ordinary_drain_share_local_lock(self):
        request=self.request()
        preparing=asyncio.Event(); release=asyncio.Event()
        start=self.sessions.start
        async def pause(assignment_id):
            preparing.set(); await release.wait(); return await start(assignment_id)
        self.sessions.start=pause
        first=asyncio.create_task(self.admit(request))
        await preparing.wait()
        with self.assertRaises(HTTPException):await self.admit(request)
        ordinary=asyncio.create_task(self.runtime.drain_thread_queue('thread-a'))
        await asyncio.sleep(0)
        self.assertFalse(ordinary.done())
        release.set()
        result=await first
        await ordinary
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.starts()),1)
        self.assertEqual(len(self.queues.get('thread-a')),2)

    async def test_authority_revocation_at_actual_adapter_input_refuses(self):
        # The second automation gate runs before active/history projection; the
        # adapter-input check must re-evaluate authority after that projection.
        original=self.runtime.record_user_message
        def revoke(*args,**kwargs):
            original(*args,**kwargs)
            self.service.actor_resolver=lambda *args:self.f.actor.model_copy(update={'roles':()})
        self.runtime.record_user_message=revoke
        result=await self.admit()
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.starts()),0)
        self.assertEqual(len(self.queues.get('thread-a')),3)
        self.assertEqual(result['attempt']['status'],'blocked')

    async def test_atomic_removal_refuses_a_changed_copy_without_reordering(self):
        request=self.request()
        update=self.host._update_thread_queue_record
        fired=False
        def change(thread, updater):
            nonlocal fired
            if not fired:
                fired=True
                self.queues.update(thread, lambda items:[item.model_copy(update={'message':'Changed copy'})
                    if item.id==self.f.items[1].id else item for item in items])
            return update(thread, updater)
        self.host._update_thread_queue_record=change
        with self.assertRaises(HTTPException):await self.admit(request)
        self.assertEqual(len(self.starts()),0)
        self.assertEqual([q.id for q in self.queues.get('thread-a')],[q.id for q in self.f.items])
        self.assertEqual(self.f.preflight.store.get(self.attempt.id).status,'queued')

    async def test_restart_after_reservation_retains_unknown_without_permission(self):
        async def crash(*args,**kwargs):raise asyncio.CancelledError()
        self.runtime.start_thread_turn_now=crash
        with self.assertRaises(asyncio.CancelledError):await self.admit()
        reopened=ExecutionPreflightStore(SQLiteStateStore(Path(self.f.temp.name)/'state.sqlite'))
        current=reopened.get(self.attempt.id)
        self.assertEqual(current.status,'reconciliation_required')
        self.assertIsNotNone(current.operator_admission)
        with self.assertRaises(HTTPException):self.preview()
        self.assertEqual(len(self.queues.get('thread-a')),3)
        self.assertEqual(len(self.starts()),0)

    async def test_replacement_route_must_be_canonical_and_preserves_original_snapshot(self):
        self.service.replacement_resolver=lambda *args,**kwargs:'replacement'
        with self.assertRaises(HTTPException):self.preview()
        attempt=self.f.preflight.store.update(self.attempt.id,lambda a:a.model_copy(update={'replacement_thread_id':'replacement'}))
        queued=self.f.items[1].model_copy(update={'thread_id':'replacement'})
        self.queues.put('replacement',[queued])
        result=await self.admit(QueuedOperatorAdmissionRequest(
            expected_snapshot_sha256=self.f.preflight.queued_snapshot_digest(attempt.queued_turn_snapshot),
            expected_queue_sha256=self.service.preview(self.attempt.id,thread_id='replacement',project=self.f.project,actor=self.f.actor)['queue_sha256'],
            reason='Reviewed canonical replacement'),thread_id='replacement')
        self.assertTrue(result['ok'])
        self.assertEqual(self.f.preflight.store.get(self.attempt.id).queued_turn_snapshot['thread_id'],'thread-a')
        self.assertEqual(result['attempt']['queued_start_receipt']['thread_id'],'replacement')

    async def test_operator_is_recorded_separately_from_original_runtime_actor(self):
        operator=self.f.actor.model_copy(update={'identity_id':'reviewing-operator'})
        self.service.actor_resolver=lambda identity,project:operator if identity==operator.identity_id else self.f.actor
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        active=self.f.authority.catalog_record(actor=self.f.actor,project_id=self.f.project.id)
        binding={**active.payload['bindings'][0], 'id':'reviewing-operator-binding',
            'subject_id':operator.identity_id, 'project_ids':[self.f.project.id]}
        draft=self.f.definitions.create_draft(DefinitionDraftCreate(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND,
            definition_schema_version='1.0',payload={**active.payload,
                'bindings':[*active.payload['bindings'],binding]},actor='fixture-author'))
        self.f.definitions.approve_publication(draft.record_id,actor='fixture-reviewer',
            reference='Synthetic reviewed grant',reason='Explicit test operator authority')
        self.f.definitions.publish(draft.record_id,DefinitionPublishRequest(
            actor='fixture-author',expected_active_revision=active.revision))
        request=self.request()
        result=await self.admit(request,actor=operator)
        self.assertTrue(result['ok'])
        self.assertEqual(result['attempt']['operator_admission']['requested_by'],operator.identity_id)
        self.assertEqual(self.host.active['thread-a'].agent_profile_actor_id,self.f.actor.identity_id)
        self.assertEqual(result['attempt']['queued_turn_snapshot']['agent_profile_actor_id'],self.f.actor.identity_id)

    async def test_changed_pause_refuses_at_setup_without_submitting(self):
        request=self.request()
        start=self.sessions.start
        async def enable(assignment_id):
            session=await start(assignment_id)
            self.runtime.automated_admission_allowed=lambda:True
            return session
        self.sessions.start=enable
        result=await self.admit(request)
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.starts()),0)
        self.assertEqual(len(self.queues.get('thread-a')),3)
