from __future__ import annotations

import time
import tempfile
from pathlib import Path
import unittest

from codex_web.models import WorkItemHandoff, WorkItemHandoffDelivery, WorkItemState
from codex_web.services.autonomy import AutonomyService
from tests.test_autonomy_service import _Host
from tests import test_work_continuity_recovery as continuity_fixtures
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


class HandoffDeliveryDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def state(self, *, requested_at, delivery):
        return WorkItemState(ref='group/project#78',project_id='project-a',
            current_owner='nora',next_owner='quinn',current_stage='ready_for_validation',
            handoff=WorkItemHandoff(from_agent='nora',to_agent='quinn',requested_at=requested_at,
                delivery=delivery),created_at=1,updated_at=1,last_meaningful_update_at=1)

    async def test_queue_wait_does_not_consume_recipient_ack_window(self):
        host=_Host();host.states={}
        state=self.state(requested_at=time.time()-60,delivery=WorkItemHandoffDelivery())
        host.states[state.ref]=state
        await AutonomyService(host).run_work_item_sla_cycle()
        self.assertIsNotNone(host.states[state.ref].handoff)
        self.assertIsNone(host.states[state.ref].handoff.acknowledged_at)
        self.assertEqual(host.states[state.ref].handoff.delivery.status,'awaiting_dispatch')
        self.assertFalse(any(e['event_type']=='handoff_expired' for e in host.events))

    async def test_ack_window_starts_from_actual_delivery(self):
        host=_Host()
        state=self.state(requested_at=time.time()-60,delivery=WorkItemHandoffDelivery(
            status='delivered',started_at=time.time()-1,thread_id='quinn-thread',
            turn_id='actual-turn',execution_id='actual-execution'))
        host.states={state.ref:state}
        await AutonomyService(host).run_work_item_sla_cycle()
        self.assertIsNotNone(host.states[state.ref].handoff)
        self.assertIsNone(host.states[state.ref].handoff.acknowledged_at)

    async def test_legacy_unknown_delivery_keeps_conservative_expiry(self):
        host=_Host();state=self.state(requested_at=time.time()-60,delivery=None)
        host.states={state.ref:state}
        await AutonomyService(host).run_work_item_sla_cycle()
        self.assertIsNone(host.states[state.ref].handoff)
        self.assertEqual(host.events[-1]['event_type'],'handoff_expired')

    async def test_undelivered_dispatch_has_a_separate_bounded_expiry(self):
        host = _Host()
        state = self.state(requested_at=time.time()-3700, delivery=WorkItemHandoffDelivery())
        host.states = {state.ref: state}
        await AutonomyService(host).run_work_item_sla_cycle()
        self.assertIsNone(host.states[state.ref].handoff)
        self.assertEqual(host.events[-1]['event_type'], 'handoff_delivery_expired')
        self.assertNotIn('acknowledge', host.states[state.ref].next_action.lower())

    async def test_delivered_recipient_still_has_a_bounded_ack_deadline(self):
        host = _Host()
        state = self.state(requested_at=time.time()-100, delivery=WorkItemHandoffDelivery(
            status='delivered', started_at=time.time()-60, thread_id='quinn-thread',
            turn_id='actual-turn', execution_id='actual-execution'))
        host.states = {state.ref: state}
        await AutonomyService(host).run_work_item_sla_cycle()
        self.assertIsNone(host.states[state.ref].handoff)
        self.assertEqual(host.events[-1]['event_type'], 'handoff_expired')


class HandoffDeliveryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.database = root / 'state.sqlite3'
        self.mirror = root / 'states.json'
        self.repository = self.reopen()
        self.state = WorkItemState(ref='group/project#78', organization_id='org',
            workspace_id='workspace', project_id='project-a', current_owner='nora',
            next_owner='quinn', current_stage='ready_for_validation',
            handoff=WorkItemHandoff(from_agent='nora', to_agent='quinn', requested_at=100,
            delivery=WorkItemHandoffDelivery()), created_at=1, updated_at=1,
            last_meaningful_update_at=1)
        self.repository.put(self.state.ref, self.state)
        self.events = []
        self.service = continuity_fixtures.WorkItemContinuityTests()._service(
            states={}, events=self.events, deliveries=[])
        self.service.update_state = self.repository.update
        self.evidence = dict(expected_requested_at=100, recipient='Quinn',
            organization_id='org', workspace_id='workspace', project_id='project-a',
            thread_id='quinn-thread', turn_id='actual-turn', execution_id='actual-execution',
            started_at=200)

    def reopen(self):
        return ModelMapRepository(SQLiteStateStore(self.database), namespace='work_item_states',
            legacy_path=self.mirror, model=WorkItemState)

    def record(self, **overrides):
        return self.service.record_handoff_delivery(self.state.ref, **(self.evidence | overrides))

    def test_successful_delivery_survives_restart_without_ack_or_owner_change(self):
        self.assertTrue(self.record())
        current = self.reopen().load()[self.state.ref]
        self.assertEqual(current.handoff.delivery.started_at, 200)
        self.assertEqual(current.handoff.delivery.execution_id, 'actual-execution')
        self.assertIsNone(current.handoff.acknowledged_at)
        self.assertEqual(current.current_owner, 'nora')
        self.assertFalse(self.record(started_at=300))
        self.assertEqual(len(self.events), 1)

    def test_wrong_nonce_recipient_tenant_and_missing_start_evidence_are_rejected(self):
        for overrides in [dict(expected_requested_at=99), dict(recipient='carl'),
            dict(organization_id='other'), dict(workspace_id='other'),
            dict(project_id='other'), dict(thread_id=''), dict(turn_id=''),
            dict(execution_id=''), dict(started_at=99)]:
            with self.subTest(overrides=overrides):
                self.assertFalse(self.record(**overrides))
        self.assertEqual(self.repository.load()[self.state.ref].handoff.delivery.status,
            'awaiting_dispatch')
        self.assertEqual(self.events, [])

    def test_late_delivery_cannot_replace_new_intake_or_acknowledged_intake(self):
        for change in [dict(requested_at=101), dict(status='accepted', acknowledged_at=150)]:
            current = self.state.model_copy(deep=True)
            current.handoff = current.handoff.model_copy(update=change)
            self.repository.put(current.ref, current)
            self.assertFalse(self.record())
            self.assertEqual(self.repository.load()[current.ref].handoff, current.handoff)

    def test_closed_or_historical_untracked_work_is_not_retrofitted(self):
        for change in [dict(current_stage='closed', closed_at=150),
            dict(handoff=self.state.handoff.model_copy(update={'delivery': None}))]:
            current = self.state.model_copy(deep=True, update=change)
            self.repository.put(current.ref, current)
            self.assertFalse(self.record())
            self.assertEqual(self.repository.load()[current.ref].handoff, current.handoff)
