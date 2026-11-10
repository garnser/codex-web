"""Real private canonical delegation and broker; external native wire is synthetic."""
import asyncio
import copy
import json
import unittest
from urllib.parse import urlencode

from codex_web.identity import MembershipRole
from tests import test_home_child_terminal_event as child_fixture
from tests import test_home_operator_host as host_fixture

READ = 'operator.project.delegation.read'


class HomeDelegationStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.e = e = child_fixture.HomeChildTerminalTests()
        e.addCleanup = self.addCleanup
        e.addAsyncCleanup = self.addAsyncCleanup
        publish = e.publish
        self.declare_read = self._testMethodName != 'test_explicit_declaration_is_required'
        def declare(payload):
            if self.declare_read:
                next(p for p in payload['profiles'] if p['id'] == 'orchestration-only')['control_plane_operations'].append(READ)
            return publish(payload)
        e.publish = declare
        await e.asyncSetUp()
        e.coordination.preflight = e.execution.queued_preflight
        e.coordination.events = e.bus.store
        e.coordination.usage = e.usage
        e.coordination.sessions = e.sessions.store
        self.parent = None

    async def request(self, *, event_id=None, intent_id=None, extra=''):
        query = {'intent_id':intent_id or self.parent['item']['id']}
        if event_id is not None:
            query['event_id'] = event_id
        return await host_fixture.HomeOperatorHostTests._request(self.e, 'GET',
            '/api/operator/projects/delegation?' + urlencode(query) + extra)

    async def completed(self, *, usage=True):
        active = await self.e.start_child()
        self.parent = self.e.original
        if usage:
            self.e.final_usage(active)
        await self.e.complete_child(active)
        return active

    async def test_exact_completed_delegation_is_readable_through_real_broker(self):
        await self.completed()
        before = (len(self.e.starts()), self.e.intents.get(self.parent['item']['id'], self.e.actor).model_dump())
        status, _, value = await self.request(event_id=self.e.received[0].event_id)
        self.assertEqual(status, 200, value)  # Genuine OLD lacks this broker route.
        self.assertEqual(value['schema_version'], '1.0')
        self.assertTrue(value['admission']['accepted'])
        self.assertEqual(value['child']['start']['provider_native_turn_id'], 'canonical-home-delegated-turn')
        self.assertTrue(value['child']['terminal']['completion_succeeded'])
        self.assertEqual(value['child']['outcome'], 'succeeded')
        self.assertEqual(value['child']['terminal']['usage']['input_tokens'], 17)
        self.assertIsNone(value['verification']['verified'])
        self.assertFalse(value['execution_authority'])
        self.assertNotIn('message', json.dumps(value))
        self.assertNotIn('One bounded current Data review', json.dumps(value))
        self.assertEqual(before, (len(self.e.starts()), self.e.intents.get(self.parent['item']['id'], self.e.actor).model_dump()))

    async def test_admission_is_not_start_or_terminal(self):
        self.parent = await self.e.message()
        status, _, value = await self.request()
        self.assertEqual(status, 200, value)
        self.assertTrue(value['admission']['accepted'])
        self.assertIsNone(value['child']['start'])
        self.assertIsNone(value['child']['terminal'])
        self.assertEqual(value['child']['outcome'], 'unknown')
        self.assertEqual(self.e.starts(), [])

    async def test_start_is_not_terminal_and_missing_usage_stays_unknown(self):
        active = await self.e.start_child()
        self.parent = self.e.original
        status, _, value = await self.request()
        self.assertEqual(status, 200, value)
        self.assertIsNotNone(value['child']['start'])
        self.assertIsNone(value['child']['terminal'])
        await self.e.complete_child(active)
        status, _, value = await self.request()
        self.assertEqual(status, 200, value)
        self.assertTrue(value['child']['terminal']['completion_succeeded'])
        self.assertEqual(value['child']['outcome'], 'unknown')
        self.assertFalse(value['child']['terminal']['usage_available'])
        self.assertIsNone(value['child']['terminal']['usage'])

    async def test_retired_child_and_successor_binding_do_not_erase_historical_facts(self):
        await self.completed()
        target = self.e.target
        self.e.f.workers.store.update(lambda state:state.model_copy(update={'assignments':[
            item.model_copy(update={'status':'cancelled', 'fence':item.fence+1, 'lease':None}) if item.id==target.id else item
            for item in state.assignments]}))
        # Reading history must never resolve a current child bootstrap/lease.
        original = self.e.coordination.bindings.get_by_thread
        self.e.coordination.bindings.get_by_thread = lambda thread, actor: original(thread, actor) if thread=='operator-thread' else (_ for _ in ()).throw(AssertionError('current child binding read'))
        try:
            status, _, value = await self.request()
        finally:
            self.e.coordination.bindings.get_by_thread = original
        self.assertEqual(status, 200, value)
        self.assertEqual(value['child']['outcome'], 'succeeded')
        self.assertEqual(value['child']['original_fence'], target.fence)

    async def test_foreign_parent_actor_or_resource_is_refused(self):
        self.parent = await self.e.message()
        original = self.e.intents.get(self.parent['item']['id'], self.e.actor)
        for field, value in (('requested_by','another-admin'), ('resource_ids',('resource-other',)), ('project_id','data')):
            with self.subTest(field=field):
                self.e.intents.store.update(lambda state:state.model_copy(update={'intents':[
                    item.model_copy(update={field:value}) if item.id==original.id else item for item in state.intents]}), intent_ids=(original.id,))
                status, _, _ = await self.request()
                self.assertEqual(status, 403)
                self.e.intents.store.update(lambda state:state.model_copy(update={'intents':[
                    original if item.id==original.id else item for item in state.intents]}), intent_ids=(original.id,))

    async def test_changed_current_home_selection_and_role_refuse(self):
        self.parent = await self.e.message()
        self.e.select('another-operator-thread', 1)
        status, _, _ = await self.request()
        self.assertEqual(status, 403)

    async def test_explicit_declaration_is_required(self):
        self.parent = await self.e.message()
        status, _, value = await host_fixture.HomeOperatorHostTests._request(self.e, 'GET', '/api/operator/host')
        self.assertEqual(status, 200, value)  # Current valid pin and Home authority.
        status, _, _ = await self.request()
        self.assertEqual(status, 403)

    async def test_foreign_or_unknown_event_never_borrows_terminal_evidence(self):
        await self.completed()
        await self.e.observer.events.ingest(event_type='work.transition', source='other-source',
            idempotency_key='foreign', payload={'queued_turn_id':'other'},
            tenant_id=self.e.actor.organization_id, workspace_id=self.e.actor.workspace_id)
        foreign = self.e.received[-1]
        for event_id in (foreign.event_id, 'evt-missing'):
            with self.subTest(event_id=event_id):
                status, _, _ = await self.request(event_id=event_id)
                self.assertEqual(status, 403)

    async def test_changed_retained_proof_refuses_instead_of_accepting_event_alone(self):
        await self.completed()
        attempt = self.e.execution.queued_preflight.store.get(self.e.received[0].payload['admission_attempt_id'])
        changed = copy.deepcopy(attempt.queued_turn_snapshot)
        changed['message'] = 'substituted task'
        self.e.execution.queued_preflight.store.update(attempt.id, lambda row:row.model_copy(update={'queued_turn_snapshot':changed}))
        status, _, _ = await self.request(event_id=self.e.received[0].event_id)
        self.assertEqual(status, 403)

    async def test_duplicate_unknown_query_and_non_get_are_rejected(self):
        self.parent = await self.e.message()
        for extra in ('&intent_id=action-intent-another', '&message=hidden', '&event_id='):
            with self.subTest(extra=extra):
                status, _, _ = await self.request(extra=extra)
                self.assertEqual(status, 400)
        status, _, _ = await host_fixture.HomeOperatorHostTests._request(self.e, 'POST',
            '/api/operator/projects/delegation?' + urlencode({'intent_id':self.parent['item']['id']}))
        self.assertEqual(status, 400)

    async def test_revoked_administrator_membership_cannot_read_prior_delegation(self):
        self.parent = await self.e.message()
        self.e.f.identity.store.update(lambda state:state.model_copy(update={'memberships':[
            m.model_copy(update={'roles':[MembershipRole.MEMBER]}) if m.identity_id==self.e.actor.identity_id else m
            for m in state.memberships]}))
        status, _, _ = await self.request()
        self.assertEqual(status, 403)

    async def test_child_project_or_profile_outside_current_envelope_refuses(self):
        self.parent = await self.e.message()
        target = self.e.target
        for changes in ({'project_id':'home'}, {'agent_profile':target.agent_profile.model_copy(update={'profile_revision':999})}):
            with self.subTest(changes=changes):
                self.e.f.workers.store.update(lambda state:state.model_copy(update={'assignments':[
                    target.model_copy(update=changes) if a.id==target.id else a for a in state.assignments]}))
                status, _, _ = await self.request()
                self.assertEqual(status, 403)
                self.e.f.workers.store.update(lambda state:state.model_copy(update={'assignments':[
                    target if a.id==target.id else a for a in state.assignments]}))

    async def test_canonical_usage_arriving_after_immutable_event_is_visible(self):
        active = await self.completed(usage=False)
        event = self.e.received[0]
        self.assertFalse(event.payload['usage_available'])
        usage = self.e.final_usage(active)
        status, _, value = await self.request()
        self.assertEqual(status, 200, value)
        self.assertEqual(value['child']['outcome'], 'succeeded')
        self.assertEqual(value['child']['terminal']['usage']['id'], usage.id)
        self.assertFalse(self.e.bus.store.event(event.event_id).payload['usage_available'])

    async def test_failed_parent_receipt_does_not_claim_admission(self):
        execute = self.e.provider.execute
        async def failed_result(*args, **kwargs):
            result = await execute(*args, **kwargs)
            return result.model_copy(update={'status':'failed'})
        self.e.provider.execute = failed_result
        self.parent = await self.e.message()
        status, _, value = await self.request()
        self.assertEqual(status, 200, value)
        self.assertEqual(value['receipt']['outcome'], 'failed')
        self.assertEqual(value['receipt']['result_status'], 'failed')
        self.assertIsNone(value['admission']['accepted'])
        self.assertIsNone(value['child']['start'])
        self.assertIsNone(value['child']['terminal'])
        self.assertEqual(value['child']['outcome'], 'unknown')

    async def test_child_start_and_terminal_before_parent_receipt_remain_separate(self):
        queued, release = asyncio.Event(), asyncio.Event()
        execute = self.e.provider.execute
        async def pending_result(*args, **kwargs):
            result = await execute(*args, **kwargs)
            queued.set()
            await release.wait()
            return result
        self.e.provider.execute = pending_result
        pending = asyncio.create_task(self.e.message())
        try:
            await asyncio.wait_for(queued.wait(), 5)
            original = self.e.queue_store.get(self.e.thread)[0]
            self.parent = {'item':{'id':original.project_coordination_dispatch.intent_id}}
            await self.e.execution.drain_thread_queue(self.e.thread)
            active = self.e.execution._active_turn(self.e.thread)
            self.assertIsNotNone(active)
            self.e.final_usage(active)
            await self.e.complete_child(active)
            status, _, value = await self.request(event_id=self.e.received[0].event_id)
            self.assertEqual(status, 200, value)
            self.assertEqual(value['intent']['status'], 'executing')
            self.assertIsNone(value['receipt'])
            self.assertIsNone(value['admission']['accepted'])
            self.assertIsNotNone(value['child']['start'])
            self.assertEqual(value['child']['outcome'], 'succeeded')
        finally:
            release.set()
            await pending
        self.assertEqual(len(self.e.starts()), 1)
