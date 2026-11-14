"""Lossless bounded canonical history through the real assignment broker."""
import copy
import json
import unittest
from urllib.parse import quote

from codex_web.models import WorkItemEvent
from codex_web.work_item_execution_models import WorkItemCheckpointCreate
from tests import test_control_plane_broker as broker_fixture
from tests import test_work_item_checkpoint_provenance as work_fixture


class WorkHistoryBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = broker_fixture.ControlPlaneBrokerTests()
        await self.env.asyncSetUp()
        self.addAsyncCleanup(self.env.asyncTearDown)
        self.work = work_fixture.WorkItemCheckpointProvenanceTests()
        self.work.setUp(); self.addCleanup(self.work.tearDown)
        state = self.work.host.states.pop(self.work.ref)
        self.work.ref = self.env.ref
        state.ref = self.env.ref; state.project_id = 'project-a'
        state.notes = ['Unicode 漢🙂 and "quoted" history. ' * 200, 'last note']
        self.work.host.states[self.work.ref] = state
        self.env.states[self.env.ref] = state
        self.env.operator.lifecycle = self.work.service
        # Exercise multiple chunks without changing the production rate policy.
        self.env.broker.limits = self.env.broker.limits.model_copy(update={'max_requests_per_minute': 100})
        self.base = '/api/work-items/' + quote(self.work.ref, safe='')

    async def read(self, suffix):
        return await self.env._request('GET', self.base + suffix)

    async def test_actual_worker_reassembles_oversized_unicode_notes_and_last_page(self):
        before = copy.deepcopy(self.work.host.states[self.work.ref])
        chunks = []; cursor = '?view=history&section=notes&offset=0&byte_offset=0'
        while True:
            status, _, page = await self.read(cursor)
            self.assertEqual(status, 200, page)
            self.assertEqual(page['_broker']['operation'], 'work_item.read')
            self.assertEqual(page['count'], 2)
            self.assertLessEqual(len(page['chunk'].encode('ascii')), 4096)
            self.assertNotIn('_projection', page)  # No coordination compactor.
            chunks.append(page['chunk'])
            if page['record_complete']:
                break
            self.assertTrue(page['incomplete'])
            cursor = ('?view=history&section=notes&offset=0&byte_offset='
                + str(page['next_byte_offset']) + '&sha256=' + quote(page['sha256']))
        self.assertEqual(json.loads(''.join(chunks)), before.notes[0])
        self.assertEqual(page['record_sha256'], self.work.service._hash_value(before.notes[0]))
        status, _, last = await self.read('?view=history&section=notes&offset=1&sha256=' + quote(page['sha256']))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(last['chunk']), before.notes[1])
        self.assertFalse(last['incomplete']); self.assertIsNone(last['next_offset'])
        self.assertEqual(self.work.host.states[self.work.ref], before)
        declaration = self.env.service.public_operation(next(
            op for op in self.env.service.operations() if op.id == 'work_item.read'))
        self.assertIn('sha256', declaration['query_parameters']['names'])

    async def test_absolute_old_event_payload_and_checkpoint_are_worker_retrievable(self):
        for i in range(503):
            self.work.machine._append_work_item_event(WorkItemEvent(ref=self.work.ref,
                event_type='test_observation', created_at=i + 1, payload={'ordinal': i}))
        status, _, page = await self.read('?view=history&section=events&offset=1')
        self.assertEqual(status, 200)
        self.assertEqual(page['count'], 503)
        self.assertEqual(json.loads(page['chunk'])['payload'], {'ordinal': 1})
        self.assertEqual(page['next_offset'], 2)
        self.work.service.checkpoint(self.work.ref, WorkItemCheckpointCreate(summary='Canonical historical input'))
        status, _, checkpoint = await self.read('?view=history&section=checkpoints')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(checkpoint['chunk'])['summary'], 'Canonical historical input')

    async def test_drift_and_bad_or_mixed_pagination_fail_without_partial_data(self):
        _, _, page = await self.read('?view=history&section=notes')
        self.work.host.states[self.work.ref].notes.append('new canonical observation')
        status, _, stale = await self.read('?view=history&section=notes&offset=1&sha256=' + quote(page['sha256']))
        self.assertEqual(status, 409); self.assertNotIn('chunk', stale)
        for query in ('view=history', 'view=history&section=notes&offset=-1',
                'view=history&section=notes&offset=', 'view=history&section=notes&offset=0&offset=1',
                'view=history&section=notes&byte_offset=1', 'view=history&section=notes&offset=999',
                'view=history&section=notes&limit=10', 'view=coordination&section=notes',
                'section=notes', 'view=unknown'):
            with self.subTest(query=query):
                status, _, failure = await self.read('?' + query)
                self.assertIn(status, (400, 422)); self.assertNotIn('chunk', failure)

    async def test_history_preserves_foreign_scope_and_current_fence_guards(self):
        status, _, denied = await self.env._request('GET', '/api/work-items/'
            + quote(self.env.other_ref, safe='') + '?view=history&section=notes')
        self.assertEqual(status, 403); self.assertNotIn('chunk', denied)
        self.env.current_assignment = self.env.assignment.model_copy(update={'fence': 2})
        status, _, denied = await self.read('?view=history&section=notes')
        self.assertEqual(status, 403); self.assertNotIn('chunk', denied)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(WorkHistoryBrokerTests)
