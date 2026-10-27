from __future__ import annotations

from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.api.agent_runtime_usage import build_agent_runtime_usage_router
from tests import test_agent_runtime_telemetry as fixtures


class CurrentNativeTurnUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.AgentRuntimeTelemetryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.sessions = self.fixture.sessions
        self.session = self.fixture.session
        self.store = self.fixture.usage
        self.db = self.store.store
        self.actor = self.fixture.actor
        self.pair = (self.session.provider_native_session_id, 'turn-current')
        self.record = AgentRuntimeUsage(
            id=self.service._record_id(self.session, self.pair[1]),
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
            project_id=self.session.project_id, agent_session_id=self.session.id,
            assignment_id=self.session.assignment_id, execution_id=self.session.execution_id,
            execution_workspace_id=self.session.execution_workspace_id,
            provider_id=self.session.provider_id, runtime_id=self.session.runtime_id,
            runtime_type=self.session.runtime_type, provider_native_session_id=self.pair[0],
            provider_native_turn_id=self.pair[1], input_tokens=111, output_tokens=22,
            terminal_outcome='succeeded', telemetry_completeness='exact',
            event_fingerprints=('private-fingerprint',), observed_at=100,
        )
        self.store.upsert(self.record)
        self.request = Request({'type':'http','state':{'identity_actor':self.actor}})
        self.endpoint = build_agent_runtime_usage_router(self.service).routes[0].endpoint

    def read(self, pairs=None, **changes):
        args = dict(project_id='project-a', provider_id='anthropic', pairs=pairs or (self.pair,))
        args.update(changes)
        return self.service.current_native_turns(self.actor, **args)

    def test_real_store_batch_ignores_large_unrelated_history_and_never_scans_or_writes(self):
        second = self.record.model_copy(update={
            'id':self.service._record_id(self.session, 'turn-other'),
            'provider_native_turn_id':'turn-other', 'terminal_outcome':'unknown'})
        self.store.upsert(second)
        # Malformed unrelated records prove this is not a catalog decode with
        # filtering afterwards. The point path must never visit them.
        self.db.record_apply(self.store.namespace, upserts={
            'unrelated-'+str(i):{'not-a-usage-record':'x'*1024} for i in range(1200)})
        self.db.record_apply(self.sessions.records_namespace, upserts={
            'sessions:unrelated-'+str(i):{'not-a-session':True} for i in range(1200)})
        with ExitStack() as stack:
            for obj, names in ((self.store, ('load','list','_ensure_records')),
                               (self.sessions, ('load','list','_ensure_records')),
                               (self.db, ('get','document_get','record_items','record_apply','record_replace','record_update','put'))):
                for name in names:
                    stack.enter_context(patch.object(obj, name, side_effect=AssertionError('history_or_write_forbidden')))
            batch = stack.enter_context(patch.object(self.db, 'record_get_many', wraps=self.db.record_get_many))
            pages = stack.enter_context(patch.object(self.db, 'record_page', wraps=self.db.record_page))
            value = self.read((self.pair, (self.pair[0], 'turn-other')))
        self.assertTrue(value['complete'])
        self.assertEqual(batch.call_count, 1)
        self.assertEqual(batch.call_args.args[0], self.store.namespace)
        self.assertEqual(len(batch.call_args.args[1]), 2)
        self.assertEqual(pages.call_count, 1, 'same native session alias is read once per batch')
        self.assertEqual(pages.call_args.kwargs['limit'], 2)
        self.assertEqual(value['items'][0]['usage']['input_tokens'], 111)
        self.assertLess(len(json.dumps(value)), 12000)
        self.assertNotIn('fingerprint', json.dumps(value))
        self.assertFalse(value['semantic_task_completion_inferred'])

    def test_wrong_tenant_workspace_project_provider_runtime_remain_unknown(self):
        for changes in ({'organization_id':'foreign'}, {'workspace_id':'foreign'}):
            with self.subTest(changes=changes):
                actor = self.actor.model_copy(update=changes)
                value = self.service.current_native_turns(actor, project_id='project-a', provider_id='anthropic', pairs=(self.pair,))
                self.assertEqual(value['items'][0]['state'], 'unknown')
                self.assertIsNone(value['items'][0]['usage'])
        for changes in ({'project_id':'foreign'}, {'provider_id':'openai'}, {'runtime_id':'other'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.read(**changes)['complete'])

    def test_alias_ambiguity_missing_or_corrupt_target_is_unknown(self):
        self.assertEqual(self.read((('absent-native','turn'),))['items'][0]['state'], 'unknown')
        alias = self.sessions._native_key(self.session)
        for alias_value in ({'session_id':'absent'}, {'session_id':self.session.id,'extra':True}):
            with self.subTest(alias=alias_value):
                self.db.record_apply(self.sessions.records_namespace, upserts={alias:alias_value})
                self.assertFalse(self.read()['complete'])
        self.db.record_apply(self.sessions.records_namespace, upserts={alias:{'session_id':self.session.id}})
        foreign = self.session.model_copy(update={'id':'foreign-session','organization_id':'foreign'})
        self.sessions.upsert(foreign)
        self.assertFalse(self.read()['complete'])

    def test_changed_custody_is_unknown_but_terminal_needs_no_live_lease(self):
        terminal = self.session.model_copy(update={'status':'closed'})
        self.sessions.upsert(terminal)
        self.assertTrue(self.read()['complete'], 'historical terminal metadata needs no live lease')
        self.sessions.upsert(terminal.model_copy(update={'assignment_id':'new-assignment'}))
        entry = self.read()['items'][0]
        self.assertEqual(entry['reason'], 'stale_attribution')
        self.assertIsNone(entry['usage'])

    def test_missing_or_mismatched_usage_is_unknown_and_fresh_each_call(self):
        self.assertEqual(self.read(((self.pair[0],'missing-turn'),))['items'][0]['reason'], 'usage_unavailable')
        for changes in ({'organization_id':'foreign'}, {'project_id':'foreign'}, {'agent_session_id':'other'},
                        {'execution_id':'other'}, {'execution_workspace_id':'other'}, {'provider_native_turn_id':'other'}):
            self.db.record_apply(self.store.namespace, upserts={self.record.id:self.record.model_copy(update=changes).model_dump(mode='json')})
            self.assertEqual(self.read()['items'][0]['reason'], 'stale_attribution')
        self.store.upsert(self.record)
        self.assertTrue(self.read()['complete'])

    def test_legacy_or_uninitialized_state_is_not_migrated(self):
        self.db.record_replace(self.store.namespace, {'schema_version':'1.1','records':[self.record.model_dump(mode='json')]})
        with patch.object(self.store, '_ensure_records', side_effect=AssertionError('no_migration')), \
                patch.object(self.db, 'record_get_many', side_effect=AssertionError('no_legacy_batch')):
            self.assertFalse(self.read()['complete'])
        self.db.record_replace(self.sessions.records_namespace, {})
        self.assertEqual(self.read()['items'][0]['reason'], 'attribution_unavailable')

    def test_duplicate_empty_or_over_bound_pairs_refuse_before_storage(self):
        with patch.object(self.sessions, 'inspect_native', side_effect=AssertionError('no_read')):
            for pairs in ((), (self.pair,self.pair), tuple(('native-'+str(i),'turn')for i in range(41)), (('','turn'),)):
                with self.subTest(pairs=len(pairs)), self.assertRaises(ValueError):
                    self.service.current_native_turns(self.actor, project_id='project-a', provider_id='anthropic', pairs=pairs)

    def test_oversized_closed_references_and_invalid_point_record_are_unknown(self):
        self.db.record_apply(self.store.namespace, upserts={self.record.id:self.record.model_copy(update={'evidence_ids':('x'*5000,)}).model_dump(mode='json')})
        self.assertEqual(self.read()['items'][0]['reason'], 'projection_too_large')
        self.db.record_apply(self.store.namespace, upserts={self.record.id:{'invalid':True}})
        self.assertEqual(self.read()['items'][0]['reason'], 'usage_unavailable')

    async def test_existing_route_serves_closed_batch_and_retains_default_history(self):
        result = await self.endpoint(self.request, project_id='project-a', provider_id='anthropic',
                                     native_session_id=[self.pair[0]], native_turn_id=[self.pair[1]])
        value = json.loads(result.body)
        self.assertTrue(value['complete'])
        self.assertNotIn('event_fingerprints', value['items'][0]['usage'])
        # The historical endpoint contract remains unchanged for existing UIs.
        historical = json.loads((await self.endpoint(self.request, project_id='project-a')).body)
        self.assertEqual(historical['items'], [self.record.model_dump(mode='json')])

    async def test_route_rejects_malformed_pairs_and_identity_before_read(self):
        with patch.object(self.service, 'current_native_turns', side_effect=AssertionError('no_read')):
            for changes in ({'native_turn_id':None}, {'native_turn_id':['a','b']}, {'include_items':False},
                            {'execution_id':'other'}, {'native_session_id':[self.pair[0]]*41,'native_turn_id':['t']*41}):
                args = dict(project_id='project-a', provider_id='anthropic', native_session_id=[self.pair[0]], native_turn_id=[self.pair[1]])
                args.update(changes)
                with self.subTest(changes=changes), self.assertRaises(HTTPException):
                    await self.endpoint(self.request, **args)
            with self.assertRaises(HTTPException) as denied:
                await self.endpoint(Request({'type':'http','state':{}}), native_session_id=['a'], native_turn_id=['b'])
            self.assertEqual(denied.exception.status_code,401)
