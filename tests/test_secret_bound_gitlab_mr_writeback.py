from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path
from copy import deepcopy

import httpx

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import TaskSourceConfiguration, WorkItemHandoff, WorkItemAckCreate
from codex_web.secrets import SecretRotate
from codex_web.services.builtin_task_source_runtime import SecretBoundTaskSource
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.secrets import SecretUseDeniedError
from codex_web.services.task_source_runtime import TaskSourceWritebackService
from codex_web.services.task_sources import (
    InvalidTaskSourceIdentity, TaskSourceCombinedWriteCapable,
    TaskSourceCapabilities, TaskSourceCapability, UnsupportedTaskSourceCapability,
)
from codex_web.services.work_items import WorkItemService
from tests import test_builtin_task_source_runtime as secret_fixture
from tests import test_task_source_writeback as writeback_fixture


class SecretBoundGitLabMrWritebackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = secret_fixture.BuiltInTaskSourceRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.row = {'iid':42, 'references':{'full':'group/project!42'},
            'title':'Actual MR', 'state':'opened', 'updated_at':'r1',
            'labels':['priority::P1','custom','owner::james','status::in progress']}
        self.requests = []
        self.tokens = []
        def respond(request):
            self.tokens.append(request.headers.get('PRIVATE-TOKEN'))
            body = json.loads(request.content) if request.content else None
            self.requests.append((request.method, request.url.raw_path.decode(), body))
            self.assertEqual(request.url.raw_path.decode(), '/api/v4/projects/group%2Fproject/merge_requests/42')
            if request.method == 'PUT':
                self.assertEqual(set(body), {'labels'})
                self.row['labels'] = body['labels'].split(',') if body['labels'] else []
                self.row['updated_at'] = f'r{len(self.requests)}'
            response = deepcopy(self.row)
            if request.method == 'PUT' and getattr(self, 'mismatch_after_put', False):
                response['iid'] = 43
            return httpx.Response(200, json=response)
        self.client = GitLabClient(transport=httpx.MockTransport(respond))
        self.adapter = GitLabTaskSource('https://gitlab.example/api/v4', 'test-token', client=self.client)
        self.identity = self.adapter._identity('group/project!42')

    def source(self):
        secret = self.fixture._secret('gitlab')
        config = TaskSourceConfiguration(source_type='gitlab',
            source_instance=self.adapter.source_instance, scope='group/project', credential_secret_id=secret.id)
        service = object.__new__(WorkItemService)
        service.identity_service = self.fixture.identity
        service.secret_broker = self.fixture.secrets
        service.gitlab = self.client
        source = service._canonical_gitlab_source(config, scope=self.fixture.scope)
        self.assertIsInstance(source, SecretBoundTaskSource)
        self.assertFalse(isinstance(source, TaskSourceCombinedWriteCapable))
        self.assertFalse(hasattr(source, 'token'))
        return source, secret

    async def test_production_secret_bound_sync_updates_exact_mr_both_labels_and_ack(self):
        source,_ = self.source()
        state = writeback_fixture._state(owner='quinn', stage='ready_for_validation').model_copy(update={
            'ref':'group/project!42', 'kind':'merge_request', 'source_identity':self.identity})
        dependencies = writeback_fixture._Dependencies(state)
        service = TaskSourceWritebackService(None, writeback_fixture._registry(source), dependencies=dependencies)
        result = await service.sync(state)
        self.assertEqual([request[0] for request in self.requests], ['GET','PUT'])
        self.assertEqual(self.requests[1][2], {'labels':'custom,priority::P1,owner::quinn,status::in progress'})
        self.assertEqual(result.ref, 'group/project!42')
        self.assertEqual(result.source_identity.external_id, 'group/project!42')
        self.assertEqual(result.current_owner, 'quinn')
        self.assertEqual(result.current_stage, 'ready_for_validation')
        self.assertEqual(dependencies.state.source_identity, result.source_identity)
        self.assertIn('custom', result.labels)
        self.assertEqual(self.row['state'], 'opened')
        self.assertTrue(service._last_applied)
        before = len(self.requests)
        await service.sync(result)
        self.assertEqual(len(self.requests), before)

    async def test_pending_failed_mr_survives_secret_bound_writeback_and_actual_ack(self):
        source, _ = self.source()
        state = writeback_fixture._state(owner='quinn', stage='failed_with_action_owner').model_copy(update={
            'ref': 'group/project!42', 'kind': 'merge_request', 'source_identity': self.identity,
            'next_owner': 'carl', 'implementation_owner': 'carl', 'validation_owner': 'quinn',
            'artifact_state': 'merge_request', 'mr_refs': ['group/project!42'],
            'handoff': WorkItemHandoff(from_agent='quinn', to_agent='carl', status='pending', requested_at=180.0),
        })
        dependencies = writeback_fixture._Dependencies(state)
        service = TaskSourceWritebackService(None, writeback_fixture._registry(source), dependencies=dependencies)
        result = await service.sync(state)
        self.assertEqual(result.current_stage, 'failed_with_action_owner')
        self.assertEqual(result.current_owner, 'quinn')
        self.assertEqual(result.next_owner, 'carl')
        self.assertEqual(result.handoff, state.handoff)
        self.assertIn('status::awaiting confirmation', self.row['labels'])
        self.assertEqual([r[0] for r in self.requests], ['GET', 'PUT'])
        before = len(self.requests)
        await service.sync(result)
        self.assertEqual(len(self.requests), before)
        # A recipient's real acknowledgement changes the delivery fingerprint,
        # including when the business stage and provider revision stay fixed.
        from codex_web.services.work_item_state import WorkItemStateMachine
        from tests.test_task_source_work_item_projection import _Host
        with tempfile.TemporaryDirectory() as directory:
            host = _Host(Path(directory))
            host.states[result.ref] = result.model_copy(deep=True)
            result = WorkItemStateMachine(host)._structured_ack(
                result.ref, WorkItemAckCreate(actor='carl', accepted=True),
            )
        dependencies.state = result.model_copy(deep=True)
        acknowledged = await service.sync(result)
        self.assertEqual(acknowledged.handoff.status, 'accepted')
        self.assertEqual(acknowledged.current_stage, 'failed_with_action_owner')
        self.assertIn('status::blocked', self.row['labels'])
        self.assertEqual(self.row['state'], 'opened')
        self.assertTrue(all(token == 'provider-token' for token in self.tokens))
        self.assertNotIn('provider-token', json.dumps(acknowledged.model_dump(mode='json')))

    async def test_creating_pending_handoff_changes_fingerprint_without_owner_or_stage_change(self):
        source, _ = self.source()
        state = writeback_fixture._state(owner='quinn', stage='failed_with_action_owner').model_copy(update={
            'ref': 'group/project!42', 'kind': 'merge_request', 'source_identity': self.identity,
        })
        dependencies = writeback_fixture._Dependencies(state)
        service = TaskSourceWritebackService(None, writeback_fixture._registry(source), dependencies=dependencies)
        result = await service.sync(state)
        self.assertIn('status::blocked', self.row['labels'])
        result.handoff = WorkItemHandoff(from_agent='quinn', to_agent='carl', requested_at=180.0)
        dependencies.state = result.model_copy(deep=True)
        pending = await service.sync(result)
        self.assertEqual(pending.current_owner, result.current_owner)
        self.assertEqual(pending.current_stage, result.current_stage)
        self.assertIn('status::awaiting confirmation', self.row['labels'])
        self.assertEqual([r[0] for r in self.requests], ['GET', 'PUT', 'GET', 'PUT'])

    async def test_individual_labels_preserve_lifecycle_for_open_closed_merged(self):
        for lifecycle in ('opened','closed','merged'):
            with self.subTest(lifecycle=lifecycle):
                self.row.update(state=lifecycle, labels=['custom','owner::james','status::blocked'])
                result = await self.adapter.write_owner(self.identity, 'quinn')
                self.assertIn('status::blocked', result.labels)
                result = await self.adapter.write_state(self.identity, 'closed')
                self.assertIn('owner::quinn', result.labels)
                self.assertIn('custom', result.labels)
                self.assertFalse(any(label.startswith('status::') for label in result.labels))
                self.assertEqual(result.source_state, lifecycle)
                self.assertEqual(self.row['state'], lifecycle)

    async def test_individual_noop_reads_but_never_puts(self):
        await self.adapter.write_owner(self.identity,'james')
        await self.adapter.write_state(self.identity,'implementation_active')
        self.assertEqual([r[0] for r in self.requests],['GET','GET'])

    async def test_resolved_proxy_uses_rotated_secret_and_revocation_stops_http(self):
        source,secret = self.source()
        await source.write_owner(self.identity,'quinn')
        self.assertTrue(all(token=='provider-token' for token in self.tokens))
        self.fixture.secrets.rotate(secret.id,SecretRotate(value='rotated-token'),actor=self.fixture.admin)
        self.tokens.clear()
        await source.write_state(self.identity,'ready_for_validation')
        self.assertTrue(self.tokens)
        self.assertTrue(all(token=='rotated-token' for token in self.tokens))
        self.fixture.secrets.revoke(secret.id,actor=self.fixture.admin,reason='test')
        before=len(self.requests)
        with self.assertRaises(SecretUseDeniedError):
            await source.write_owner(self.identity,'dana')
        self.assertEqual(len(self.requests),before)

    async def test_individual_malformed_identity_rejected_before_any_http(self):
        for method,value in ((self.adapter.write_owner,'quinn'),(self.adapter.write_state,'closed')):
            for external_id in ('group/project!0','group/project!-1','group/project#42!1','group/project!42!1','group/project!１２'):
                with self.subTest(method=method.__name__,identity=external_id), self.assertRaises(InvalidTaskSourceIdentity):
                    await method(self.adapter._identity(external_id),value)
        self.assertEqual(self.requests,[])

    async def test_individual_mismatched_read_response_never_puts(self):
        self.row['iid']=43
        for method,value in ((self.adapter.write_owner,'quinn'),(self.adapter.write_state,'closed')):
            with self.assertRaises(InvalidTaskSourceIdentity):
                await method(self.identity,value)
        self.assertEqual([r[0] for r in self.requests],['GET','GET'])

    async def test_individual_mismatched_mutation_response_is_not_acknowledged(self):
        self.mismatch_after_put = True
        for method,value in ((self.adapter.write_owner,'quinn'),(self.adapter.write_state,'closed')):
            with self.assertRaises(InvalidTaskSourceIdentity):
                await method(self.identity,value)
        self.assertEqual([r[0] for r in self.requests],['GET','PUT','GET','PUT'])
        self.assertTrue(all(set(r[2])=={'labels'} for r in self.requests if r[0]=='PUT'))

    async def test_capability_denial_keeps_individual_routes_closed_before_http(self):
        self.adapter.capabilities=TaskSourceCapabilities(frozenset({TaskSourceCapability.READ}))
        for method,value in ((self.adapter.write_owner,'quinn'),(self.adapter.write_state,'closed')):
            with self.assertRaises(UnsupportedTaskSourceCapability):
                await method(self.identity,value)
        self.assertEqual(self.requests,[])
