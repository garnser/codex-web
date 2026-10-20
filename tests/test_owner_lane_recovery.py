from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from codex_web.autonomy import AutonomyControl
from codex_web.models import Project, QueuedTurn, WorkItemState
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.owner_lane_recovery import OwnerLaneRecoveryService
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class OwnerLaneRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStateStore(Path(self.temp.name) / 'state.db')
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.store))
        self.preflight.store.prepare_work_item_index()
        self.controller = AutonomyController(AutonomyStateStore(self.store))
        self.controller.store.set_control(AutonomyControl(mode='active'), actor_id='operator')
        self.project = Project(id='project', name='Project', path='/fixture', organization_id='org', workspace_id='ws')
        self.state = WorkItemState(ref='group/repo!52', organization_id='org', workspace_id='ws',
            project_id='project', current_owner='james', current_stage='implementation_active',
            resource_ids=['repo'], next_action='repair existing MR',
            created_at=1, updated_at=2, last_meaningful_update_at=2)
        self.binding = NS(id='binding', thread_id='thread', project_id='project', sandbox='workspace-write', approval_policy='never')
        self.profile = NS(profile_id='james', revision=6, execution_profile_id='repository')
        self.actor = NS(identity_id='actor', organization_id='org', workspace_id='ws')
        self.failed = NS(id='failed', fence=3, assigned_worker_id='worker', status='lost', lease=None, organization_id='org', workspace_id='ws',
            project_id='project', execution_id='execution', execution_workspace_id='workspace')
        self.bootstrap = NS(assignment_id='failed', execution_id='execution', execution_workspace_id='workspace')
        self.local = NS(assignment_id='failed', worker_id='worker', fence=3, running=False, pending_executor_work=0)
        self.held = QueuedTurn(id='uncertain', thread_id='thread', project_id='project',
            message='old uncertain request', source='web', created_at=1)
        self.queue = [self.held]
        retained = self.preflight.retain_queued(self.held, organization_id='org', workspace_id='ws')
        self.preflight.queued_disposition(retained.id, status='reconciliation_required')
        self.execution = NS(_bootstrap_binding_for_thread=lambda _: self.bootstrap,
            _assignment_record=lambda _: self.failed,
            thread_has_inflight_agent_runtime_assignment=lambda _: False,
            _session_for_assignment=lambda _: (None, NS(status=lambda: self.local)),
            automation_wait_reason=lambda _: None,
            _update_thread_queue=lambda thread, change: setattr(self, 'queue', change(self.queue)),
            schedule_queue_drain=Mock(), publish_queue_status=AsyncMock())
        self.dispatch = NS(projects=NS(get=lambda _: self.project),
            settings=NS(get=lambda _: NS(model=None, reasoning_effort=None)),
            agent_profile_resolver=lambda _: (self.profile, self.actor),
            execution_profiles=NS(resolve=lambda *a, **k: (NS(repository_access='write'), None)),
            targets=NS(event_reply_target=lambda _: None, conversation_target=lambda _: None),
            queue_policy=NS(queue=lambda _: self.queue))
        self.service = self.make_service()

    def make_service(self):
        return OwnerLaneRecoveryService(execution=self.execution, preflight=self.preflight,
            dispatch=self.dispatch, get_state=lambda _: self.state,
            load_bindings=lambda: [self.binding], binding_owner=lambda _: 'james',
            controller=self.controller, project_enabled=lambda _: True)

    async def recover(self):
        return await self.service.recover(self.binding, self.state, 'current MR repair')

    async def test_new_exact_scoped_request_keeps_historical_unknown_bytes_and_budgets(self):
        old = self.preflight.retain_queued(self.held, organization_id='org', workspace_id='ws')
        self.preflight.queued_disposition(old.id, status='reconciliation_required')
        before = self.preflight.store.get(old.id).model_dump(mode='json')
        held_bytes = self.held.model_dump_json()
        result = await self.recover()
        self.assertTrue(result['queued'])
        self.assertEqual(len(self.queue), 2)
        queued = self.queue[-1]
        self.assertEqual(queued.work_item_ref, self.state.ref)
        self.assertEqual(queued.writable_repository_resource_ids, ('repo',))
        self.assertEqual(queued.owner_lane_recovery.failed_assignment_id, 'failed')
        self.assertEqual(queued.agent_profile_revision, 6)
        self.assertEqual(self.held.model_dump_json(), held_bytes)
        self.assertEqual(self.preflight.store.get(old.id).model_dump(mode='json'), before)
        self.assertEqual(self.preflight.store.get(result['preflightAttemptId']).status, 'queued')

    async def test_restart_and_later_failed_assignment_do_not_create_a_second_request(self):
        first = await self.recover()
        self.service = self.make_service()
        self.failed.id = 'another-failed'
        self.bootstrap.assignment_id = self.failed.id
        second = await self.recover()
        self.assertTrue(second['deduplicated'])
        self.assertEqual(second['preflightAttemptId'], first['preflightAttemptId'])
        self.assertEqual(len(self.queue), 2)
        self.execution.schedule_queue_drain.assert_called_once()

    async def test_admitting_and_transient_unknown_are_retained_without_replay(self):
        first = await self.recover()
        for status in ('admitting', 'reconciliation_required', 'blocked', 'started'):
            with self.subTest(status=status):
                self.preflight.store.update(first['preflightAttemptId'], lambda current: current.model_copy(update={'status': status}))
                result = await self.recover()
                self.assertTrue(result['deduplicated'])
        self.execution.schedule_queue_drain.assert_called_once()

    async def test_same_work_unknown_blocks_but_another_work_does_not(self):
        original = self.held.model_copy(update={'id': 'same-work-unknown', 'work_item_ref': self.state.ref})
        old = self.preflight.retain_queued(original, organization_id='org', workspace_id='ws')
        self.preflight.queued_disposition(old.id, status='reconciliation_required')
        result = await self.recover()
        self.assertFalse(result['ok'])
        retained = self.preflight.store.get(result['preflightAttemptId'])
        self.assertEqual(retained.blockers[0].code, 'owner_recovery_original_unresolved')
        self.assertEqual(self.queue, [self.held])
        self.execution.schedule_queue_drain.assert_not_called()

    async def test_live_or_inflight_assignment_never_qualifies(self):
        for status in ('pending', 'claimed', 'running', 'succeeded'):
            self.failed.status = status
            self.assertFalse(self.service.candidate(self.binding, self.state))
            result = await self.recover()
            self.assertTrue(result['ignored'])
        self.failed.status = 'lost'
        self.execution.thread_has_inflight_agent_runtime_assignment = lambda _: True
        self.assertFalse(self.service.candidate(self.binding, self.state))
        self.assertEqual(self.queue, [self.held])

    async def test_process_and_executor_teardown_must_be_observed_settled(self):
        for attribute, value in (('running', True), ('pending_executor_work', 1)):
            with self.subTest(attribute=attribute):
                setattr(self.local, attribute, value)
                result = await self.recover()
                self.assertFalse(result['ok'])
                self.assertEqual(self.preflight.store.get(result['preflightAttemptId']).blockers[0].code,
                    'owner_recovery_teardown_unsettled')
                setattr(self.local, attribute, False if attribute == 'running' else 0)
                self.state.next_action += ' changed'
        self.execution.schedule_queue_drain.assert_not_called()

    async def test_no_local_process_observation_is_precise_blocker(self):
        self.execution._session_for_assignment = Mock(side_effect=HTTPException(status_code=503))
        result = await self.recover()
        self.assertEqual(self.preflight.store.get(result['preflightAttemptId']).blockers[0].code,
            'owner_recovery_process_unverified')
        self.execution.schedule_queue_drain.assert_not_called()

    async def test_admission_revalidates_owner_profile_fence_pause_and_work_evidence(self):
        await self.recover()
        queued = self.queue[-1]
        mutations = (
            (self.state, 'current_owner', 'carl', 'owner_recovery_work_changed'),
            (self.state, 'next_action', 'changed dependency', 'owner_recovery_work_changed'),
            (self.profile, 'revision', 7, 'owner_recovery_profile_changed'),
            (self.failed, 'fence', 4, 'owner_recovery_custody_changed'),
            (self.local, 'running', True, 'owner_recovery_teardown_unsettled'),
        )
        for target, attribute, value, code in mutations:
            with self.subTest(attribute=attribute):
                before = getattr(target, attribute)
                setattr(target, attribute, value)
                with self.assertRaises(HTTPException) as caught:
                    self.service.validate_queued(queued)
                self.assertEqual(caught.exception.detail['code'], code)
                setattr(target, attribute, before)
        self.controller.store.set_control(AutonomyControl(mode='paused'), actor_id='operator')
        with self.assertRaises(HTTPException) as caught:
            self.service.validate_queued(queued)
        self.assertEqual(caught.exception.detail['code'], 'owner_recovery_paused')

    async def test_published_scope_none_is_preserved_for_coordinator(self):
        self.dispatch.execution_profiles.resolve = lambda *a, **k: (NS(repository_access='none'), None)
        await self.recover()
        self.assertIsNone(self.queue[-1].repository_resource_id)
        self.assertEqual(self.queue[-1].writable_repository_resource_ids, ())

    async def test_changed_work_evidence_wakes_one_new_revision_but_projection_writes_do_not(self):
        await self.recover()
        self.state.updated_at += 1
        self.state.last_meaningful_update_at += 1
        self.state.notes.append('display-only bookkeeping')
        self.assertTrue((await self.recover())['deduplicated'])
        self.state.next_action = 'CI terminal: validate exact new head'
        self.state.last_meaningful_update_at += 1
        self.queue.pop()  # Prior fresh request is no longer queued after its terminal outcome.
        self.assertTrue((await self.recover())['queued'])
        self.assertEqual(len(self.queue), 2)

    async def test_concurrent_observations_reserve_at_most_one_original(self):
        results = await asyncio.gather(*(self.recover() for _ in range(5)), return_exceptions=True)
        self.assertEqual(len(self.queue), 2)
        self.execution.schedule_queue_drain.assert_called_once()
        self.assertFalse(any(isinstance(r, Exception) for r in results), results)
        self.assertTrue(any(isinstance(r, dict) and r.get('queued') for r in results))

    async def test_pending_human_input_prevents_automatic_recovery(self):
        human = self.held.model_copy(update={'id': 'new-human', 'message': 'please answer me', 'source': 'slack'})
        self.queue.append(human)
        self.assertFalse(self.service.candidate(self.binding, self.state))
        result = await self.recover()
        self.assertEqual(result['reason'], 'owner_lane_busy')
        self.assertEqual(self.queue, [self.held, human])
        self.execution.schedule_queue_drain.assert_not_called()

    async def test_racing_human_arrival_stays_ahead_of_new_automation(self):
        original = self.service.validate_queued
        human = self.held.model_copy(update={'id': 'human-racing', 'source': 'web'})
        def validate(queued):
            original(queued)
            self.queue.append(human)
        self.service.validate_queued = validate
        result = await self.recover()
        self.assertTrue(result['queued'])
        self.assertEqual([q.id for q in self.queue[:2]], [self.held.id, human.id])
        self.assertEqual(self.queue[-1].source, 'owner-lane-recovery')

    async def test_positive_teardown_proof_survives_ordinary_custody_handle_retirement(self):
        await self.recover()
        queued = self.queue[-1]
        self.assertIsNotNone(queued.owner_lane_recovery.teardown_observed_at)
        self.execution._session_for_assignment = Mock(side_effect=HTTPException(status_code=503))
        self.service.validate_queued(queued)
        altered = queued.model_copy(update={'message': 'a changed input'})
        with self.assertRaises(HTTPException) as caught:
            self.service.validate_queued(altered)
        self.assertEqual(caught.exception.detail['code'], 'owner_recovery_provenance_changed')

    async def test_failure_observation_requires_exact_worker_assignment_and_fence(self):
        for attr, value in (('worker_id', 'another-worker'), ('assignment_id', 'another-assignment'), ('fence', 9)):
            with self.subTest(attribute=attr):
                before = getattr(self.local, attr)
                setattr(self.local, attr, value)
                self.assertIsNone(self.service.observe_teardown(self.failed))
                setattr(self.local, attr, before)

    async def test_retained_index_failure_remains_visible_without_a_native_request(self):
        self.preflight.store.unresolved_work_item = Mock(side_effect=ValueError('index unavailable'))
        result = await self.recover()
        retained = self.preflight.store.get(result['preflightAttemptId'])
        self.assertEqual(retained.status, 'blocked')
        self.assertEqual(retained.blockers[0].code, 'owner_recovery_retained_index_unavailable')
        self.assertEqual(retained.queued_turn_snapshot['owner_lane_recovery']['action_owner'], 'actor')
        self.execution.schedule_queue_drain.assert_not_called()

    async def test_execution_bookkeeping_cannot_invalidate_its_own_admission_or_rearm_recovery(self):
        await self.recover()
        queued = self.queue[-1]
        self.state.updated_at += 50
        self.state.last_meaningful_update_at += 50
        self.state.execution.usage.input_tokens += 1
        self.service.validate_queued(queued)
        self.assertTrue((await self.recover())['deduplicated'])

    async def test_actual_controller_pause_prevents_recovery_mutations(self):
        import time
        import uuid
        from codex_web.compatibility import CanonicalEventEnvelope
        from codex_web.services.autonomy import AutonomyService
        async def ingest(**kw):
            return NS(inserted=True, event=CanonicalEventEnvelope(event_id=uuid.uuid4().hex,
                occurred_at=time.time(), event_type=kw['event_type'], source=kw['source'], correlation_id='test',
                tenant_id=kw['tenant_id'], workspace_id=kw['workspace_id'], payload=kw['payload']))
        runtime = NS(owner_lane_recovery=self.service, project_scope=lambda _: ('org', 'ws'),
            replace_nonperforming_thread=AsyncMock(), dispatch_event=AsyncMock(),
            thread_is_active=lambda _: False, thread_queue_depth=lambda _: len(self.queue))
        autonomy = AutonomyService(runtime=runtime, controller=self.controller, canonical_events=NS(ingest=ingest))
        for control in (AutonomyControl(mode='paused'), AutonomyControl(mode='killed'),
                AutonomyControl(mode='active', dry_run=True), AutonomyControl(mode='active', simulation=True)):
            self.controller.store.set_control(control, actor_id='operator')
            result = await autonomy._bounded_reasoning_dispatch(self.binding, 'continue current MR', 'owner-work-watchdog',
                work_item=self.state, cycle_key='recover', recovery_reason='terminal-runner',
                payload={'project_id': 'project'})
            self.assertTrue(result['ignored'])
            self.assertEqual(self.queue, [self.held])
        runtime.replace_nonperforming_thread.assert_not_awaited()
        runtime.dispatch_event.assert_not_awaited()
        self.execution.schedule_queue_drain.assert_not_called()

    async def test_admitted_current_owner_recovers_behind_unknowns_without_generic_replacement(self):
        import time
        from codex_web.compatibility import CanonicalEventEnvelope
        from codex_web.services.autonomy import AutonomyService
        async def ingest(**kw):
            return NS(inserted=True, event=CanonicalEventEnvelope(event_id='recovery-event',
                occurred_at=time.time(), event_type=kw['event_type'], source=kw['source'], correlation_id='test',
                tenant_id=kw['tenant_id'], workspace_id=kw['workspace_id'], payload=kw['payload']))
        runtime = NS(owner_lane_recovery=self.service, project_scope=lambda _: ('org', 'ws'),
            replace_nonperforming_thread=AsyncMock(), dispatch_event=AsyncMock(),
            thread_is_active=lambda _: False, thread_queue_depth=lambda _: len(self.queue))
        autonomy = AutonomyService(runtime=runtime, controller=self.controller, canonical_events=NS(ingest=ingest))
        result = await autonomy._bounded_reasoning_dispatch(self.binding, 'repair existing MR', 'owner-work-watchdog',
            work_item=self.state, cycle_key='recover-current', recovery_reason='terminal-runner',
            payload={'project_id': 'project'})
        self.assertTrue(result['queued'], result)
        self.assertEqual(self.queue[0], self.held)
        self.assertEqual(self.queue[1].work_item_ref, self.state.ref)
        runtime.replace_nonperforming_thread.assert_not_awaited()
        runtime.dispatch_event.assert_not_awaited()
