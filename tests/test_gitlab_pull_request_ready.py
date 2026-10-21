"""Composed fenced broker/intent/SecretBroker/GitHub HTTP qualification, offline."""
import copy
import asyncio
import json
import threading
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

import httpx

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.definitions import DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.resources import ResourceAlias, ResourceUpdate
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.procedure_discovery import ProcedureDiscoveryService, ProcedureExecutionCapabilityService
from codex_web.services.secrets import SecretBroker
from codex_web.services.skills import SkillService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_administrative_broker_runtime as fixture

HEAD = 'a' * 40
BASE = 'b' * 40
ACTION = 'code-host.pull-request.ready'
OPERATION = 'repository.pull-request.ready'


from tests import test_pull_request_ready as ready_fixture

class GitLabPullRequestReadyTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(fixture.AdministrativeBrokerRuntimeTests._assignment)
    _publish_worker_authority = ready_fixture.PullRequestReadyTests._publish_worker_authority
    _publish_profile = ready_fixture.PullRequestReadyTests._publish_profile
    _request = ready_fixture.PullRequestReadyTests._request
    asyncTearDown = ready_fixture.PullRequestReadyTests.asyncTearDown
    change_assignment = ready_fixture.PullRequestReadyTests.change_assignment
    submit = ready_fixture.PullRequestReadyTests.submit

    async def asyncSetUp(self):
        await fixture.AdministrativeBrokerRuntimeTests.asyncSetUp(self,
            operations=(OPERATION, 'action_intent.reconcile', 'repository.read.delivery-evidence'),
            prepare_only=True)
        # Keep the private SQLite WAL alive between service transactions. This
        # idle handle never executes a transaction or replaces their connections.
        # PostgreSQL retains its actual per-test pool and independent writers.
        self.sqlite_lifetime_connection = (
            self.sqlite._connect() if isinstance(self.sqlite, SQLiteStateStore) else None)
        if self.sqlite_lifetime_connection is not None:
            self.addCleanup(self.sqlite_lifetime_connection.close)
        self.resources.update(self.target.id, ResourceUpdate(aliases=[
            ResourceAlias(namespace='repository', provider='gitlab', value='acme/app')]), actor=self.admin)
        self.secret_broker = SecretBroker(SecretStateStore(self.sqlite),
            {'local':LocalFileSecretBackend(Path(self.temp.name) / 'ready-secrets')})
        self.secret = self.secret_broker.create(SecretCreate(name='Offline fixture', value='ready-fixture-token',
            provider='gitlab', allowed_identity_ids=[self.worker_actor.identity_id]), actor=self.admin)
        self.calls = []
        self.pr_reads = 0
        self.mode = 'normal'
        self.rules_version = 1
        self.pr = {'id':10, 'iid':7, 'project_id':42, 'source_project_id':42,
            'target_project_id':42, 'state':'opened', 'draft':True,
            'sha':HEAD, 'source_branch':'feature', 'target_branch':'main',
            'title':'Draft: Preserve real title', 'description':'Keep description'}
        self.target_sha = BASE
        self.provider = GitLabActionProvider(self.resources,
            client=GitLabClient(transport=httpx.MockTransport(self.respond)),
            provider_instance='gitlab.test', api_base='https://gitlab.test/api/v4', web_base='https://gitlab.test')
        registry = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        registry.register(self.provider)
        self.binding = registry.bind(ActionProviderBindingCreate(provider_type='gitlab',
            provider_instance='gitlab.test', project_id='project-a', resource_ids=(self.target.id,),
            credential_ref=self.secret.id), actor=self.admin, resources=self.resources)
        discovery = ProcedureDiscoveryService(SkillService(self.registry), self.resources,
            execution_capabilities=ProcedureExecutionCapabilityService(self.workers, None, None))
        self.intents = ActionIntentService(ActionIntentStore(self.sqlite),
            ActionExecutionService(registry, self.resources, secret_broker=self.secret_broker),
            authority=self.authority, identity=self.identity, execution_workers=self.workers,
            execution_profiles=self.profiles, procedure_discovery=discovery)
        self.service.action_intents = self.intents
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id=self.worker.id, worker_actor=self.worker_actor, fence=self.assignment.fence,
            validator=lambda:self.workers.assignment_for_execution(self.assignment.execution_id, actor=self.admin))
        await self.broker.start()
        self.broker_client = httpx.AsyncClient(base_url="http://control-plane.invalid",
            transport=httpx.AsyncHTTPTransport(uds=str(self.broker.socket_path)), timeout=30)
        self.addAsyncCleanup(self.broker_client.aclose)


    def respond(self, request):
        self.calls.append((request.method, request.url.path))
        self.assertEqual(request.headers['private-token'], 'ready-fixture-token')
        if request.method == 'PUT':
            self.assertTrue(request.url.path.endswith('/merge_requests/7'))
            payload = json.loads(request.content)
            self.assertEqual(payload, {'title':'Preserve real title'})
            self.pr.update(title=payload['title'], draft=False)
            if self.mode == 'lost':
                raise httpx.ReadTimeout('Lost mutation response')
            if self.mode == 'changed-head-after':
                self.pr['sha'] = 'c'*40
            if self.mode == 'fence-after':
                self.change_assignment(fence=self.assignment.fence+1)
            if self.mode == 'metadata-after':
                self.pr['description'] = 'Concurrent update'
            return httpx.Response(200, json=copy.deepcopy(self.pr))
        if request.url.path.endswith('/merge_requests/7'):
            self.pr_reads += 1
            if self.mode == 'metadata-before' and self.pr_reads == 2:
                self.pr['description'] = 'Concurrent metadata'
            if self.mode == 'fence-before':
                self.change_assignment(fence=self.assignment.fence+1)
            if self.mode == 'credential-before':
                self.secret_broker.revoke(self.secret.id, actor=self.admin)
            return httpx.Response(200, json=copy.deepcopy(self.pr))
        if request.url.path.endswith('/repository/branches/main'):
            return httpx.Response(200, json={'name':'main', 'commit':{'id':self.target_sha}})
        if request.url.path.endswith('/projects/acme/app'):
            return httpx.Response(200, json={'id':42,'path_with_namespace':'acme/app'})
        raise AssertionError(f'Unexpected HTTP {request.method} {request.url.path}')

    async def test_broker_transition_and_durable_duplicate(self):
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'succeeded', body)
        history = self.intents.history(body['item']['id'], self.admin)
        self.assertTrue(history['verifications'][-1]['verified'])
        self.assertNotIn('ready-fixture-token', json.dumps(history))
        _, _, again = await self.submit()
        self.assertEqual(body['item']['id'], again['item']['id'])
        self.assertEqual(sum(method=='PUT' for method,_ in self.calls), 1)

    async def test_ready_at_last_history_slot_revalidates_without_duplicate_exhaustion(self):
        from codex_web.procedure_discovery import ProcedureDiscovery
        original = []
        for index in range(63):
            discovery = ProcedureDiscovery(actor_identity_id=self.admin.identity_id,
                organization_id=self.admin.organization_id, workspace_id=self.admin.workspace_id,
                project_id=self.assignment.project_id, resource_ids=(self.target.id,),
                operation='repository.read', context_checksum=f'preserved-{index}', evaluated_at=1,
                outcome='no_match', policy_source='canonical:fixture', policy_allows=True)
            self.workers.record_procedure_discovery(self.assignment.id, discovery,
                expected_fence=self.assignment.fence, actor=self.admin)
            original.append(discovery)
        with patch.object(self.intents.procedure_discovery, 'discover',
                wraps=self.intents.procedure_discovery.discover) as discover:
            status, _, body = await self.submit(key='ready-last-history-slot')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'succeeded', body)
        self.assertGreater(discover.call_count, 5)
        current = self.workers.store.assignment(self.assignment.id)
        self.assertEqual(len(current.procedure_discoveries), 64)
        self.assertEqual(current.procedure_discoveries[:63], tuple(original))
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)
        # The real adapter reads/rechecks repeatedly before and after mutation;
        # all those checkpoints still run, without consuming redundant history.
        self.assertGreaterEqual(self.pr_reads, 5)
        self.assertTrue(self.intents.history(body['item']['id'], self.admin)['verifications'][-1]['verified'])

    async def test_already_ready_reads_only(self):
        self.pr.update(draft=False, title='Preserve real title')
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'], 'succeeded', body)
        self.assertTrue(all(method=='GET' for method,_ in self.calls))

    async def test_pre_send_denials_never_mutate(self):
        cases = [('stale-head', {'sha':'c'*40}), ('wrong-project', {'target_project_id':99}),
                 ('closed', {'state':'closed'}), ('wrong-target', {'target_branch':'other'}),
                 ('malformed-draft', {'draft':None})]
        original = copy.deepcopy(self.pr)
        for mode, values in cases:
            with self.subTest(mode=mode):
                self.pr = {**original, **values}
                _, _, body = await self.submit(key=mode)
                self.assertEqual(body['item']['status'], 'failed', body)
        self.assertFalse(any(method=='PUT' for method,_ in self.calls))

    async def test_stale_target_tip_never_mutates(self):
        self.target_sha = 'c'*40
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'], 'failed', body)
        self.assertFalse(any(method=='PUT' for method,_ in self.calls))

    async def test_revoked_assignment_never_mutates(self):
        self.mode = 'fence-before'
        _, _, body = await self.submit()
        self.assertNotEqual(body['item']['status'], 'succeeded', body)
        self.assertFalse(any(method=='PUT' for method,_ in self.calls))

    async def test_revoked_credential_never_mutates(self):
        self.mode = 'credential-before'
        _, _, body = await self.submit()
        self.assertNotEqual(body['item']['status'], 'succeeded', body)
        self.assertFalse(any(method=='PUT' for method,_ in self.calls))

    async def test_lost_response_read_only_reconciliation(self):
        self.mode = 'lost'
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'], 'uncertain', body)
        _, _, again = await self.submit()
        self.assertEqual(again['item']['id'], body['item']['id'])
        count = len(self.calls)
        status, _, settled = await self._request('POST',
            '/api/action-intents/'+body['item']['id']+'/reconcile?repository_resource_id='+self.target.id,
            payload={'retry_if_idempotent':False})
        self.assertEqual(status,200,settled)
        self.assertEqual(settled['item']['status'],'succeeded',settled)
        self.assertTrue(all(method=='GET' for method,_ in self.calls[count:]))
        self.assertEqual(sum(method=='PUT' for method,_ in self.calls),1)

    async def test_post_send_head_race_stays_uncertain(self):
        self.mode = 'changed-head-after'
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'],'uncertain',body)

    async def test_post_send_fence_race_stays_uncertain(self):
        self.mode = 'fence-after'
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'],'uncertain',body)

    async def test_post_send_metadata_race_stays_uncertain(self):
        self.mode = 'metadata-after'
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'],'uncertain',body)

    async def test_concurrent_metadata_before_send_never_mutates(self):
        self.mode = 'metadata-before'
        _, _, body = await self.submit()
        self.assertEqual(body['item']['status'], 'failed', body)
        self.assertFalse(any(method=='PUT' for method,_ in self.calls))

    async def test_ambiguous_provider_binding_denied_before_intent(self):
        self.intents.execution.registry.bind(ActionProviderBindingCreate(provider_type='gitlab',
            provider_instance='gitlab.test', project_id='project-a', resource_ids=(self.target.id,),
            credential_ref=self.secret.id), actor=self.admin, resources=self.resources)
        status, _, body = await self.submit()
        self.assertEqual(status, 403, body)
        self.assertIn('exactly one eligible', str(body))
        self.assertFalse(self.calls)
