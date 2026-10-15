"""Official-HTTP readiness contract through canonical provider and secret services."""
import json
import unittest

import httpx

from codex_web.integrations.github_client import GitHubClient
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_READY_ACTION_ID as READY
from tests import test_github_action_provider as github_fixture
from tests import test_action_intents as intent_fixture


class GitHubReadyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = github_fixture.GitHubActionProviderTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.item = {'number': 17, 'node_id': 'PR_fixture', 'state': 'open', 'merged': False,
            'draft': True, 'head': {'sha': 'a' * 40},
            'base': {'ref': 'main', 'repo': {'full_name': 'garnser/codex-web'}}}
        self.calls = []
        self.lost = False
        self.after_read = None
        self.after_write = None
        self.f.provider.client = GitHubClient(transport=httpx.MockTransport(self.respond))
        self.request = self.f._request(READY, {'pull_request_number': 17, 'expected_head_sha': 'a' * 40})

    def respond(self, request):
        self.calls.append((request.method, str(request.url)))
        self.assertEqual(request.headers['authorization'], 'Bearer secret-github-token')
        if request.method == 'GET':
            response = httpx.Response(200, json=self.item)
            if self.after_read:
                self.after_read()
            return response
        self.assertEqual(request.url.path, '/graphql')
        data = json.loads(request.content)
        self.assertIn('markPullRequestReadyForReview', data['query'])
        payload = data['variables']['input']
        self.assertEqual(payload['pullRequestId'], 'PR_fixture')
        self.assertIn('codex-web-action', payload['clientMutationId'])
        self.item['draft'] = False
        if self.after_write:
            self.after_write()
        if self.lost:
            raise httpx.ReadError('Controlled response lost after readiness write')
        return httpx.Response(200, json={'data': {'markPullRequestReadyForReview': {
            'clientMutationId': payload['clientMutationId'], 'pullRequest': {
                'id': 'PR_fixture', 'number': 17, 'isDraft': False, 'headRefOid': 'a' * 40,
                'baseRefName': 'main', 'repository': {'nameWithOwner': 'garnser/codex-web'}}}}})

    async def execute(self):
        return await self.f.execution.execute(self.f.binding.id, self.request, actor=self.f.actor)

    async def test_ready_and_already_ready_are_verified_without_a_second_mutation(self):
        first = await self.execute()
        self.assertTrue((await self.f.execution.verify(self.f.binding.id, first, actor=self.f.actor)).verified)
        again = await self.execute()
        self.assertTrue(again.output['already_ready'])
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        self.assertNotIn('secret-github-token', first.model_dump_json())

    def test_gitlab_does_not_advertise_unqualified_readiness(self):
        from codex_web.services.gitlab_action_provider import GitLabActionProvider
        provider = GitLabActionProvider(self.f.resources)
        self.assertNotIn(READY, [action.action_id for action in provider.actions()])

    async def test_mismatched_head_repository_number_and_missing_state_refuse_before_mutation(self):
        original = json.loads(json.dumps(self.item))
        for change in ({'head': {'sha': 'b' * 40}}, {'number': 18}, {'merged': None},
                       {'base': {'ref': 'main', 'repo': {'full_name': 'foreign/repo'}}}, {'draft': None}):
            self.item = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ActionRequirementError):
                await self.execute()
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    async def test_missing_expected_head_or_caller_node_and_query_are_refused(self):
        valid = self.request.parameters
        for parameters in ({'pull_request_number': 17}, {**valid, 'node_id': 'foreign'},
                           {**valid, 'query': 'arbitrary'}, {**valid, 'expected_head_sha': None}):
            self.request = self.request.model_copy(update={'parameters': parameters})
            with self.assertRaises(ActionRequirementError):
                await self.execute()
        self.assertEqual(self.calls, [])

    async def test_canonical_admission_is_rechecked_after_awaited_read(self):
        revoked = False
        async def admission(*_args):
            if revoked:
                raise ActionRequirementError('Fixture assignment or authority revoked')
        def revoke():
            nonlocal revoked
            revoked = True
        self.f.execution.procedure_admission = admission
        self.after_read = revoke
        with self.assertRaisesRegex(ActionRequirementError, 'admission'):
            await self.execute()
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    async def test_changed_head_after_possible_write_is_unknown_and_not_replayed(self):
        self.after_write = lambda: self.item.update(head={'sha': 'b' * 40})
        with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
            await self.execute()
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)

    async def test_lost_response_is_not_replayed_by_provider(self):
        self.lost = True
        with self.assertRaises(httpx.ReadError):
            await self.execute()
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)

    async def test_enterprise_graphql_endpoint_and_bounded_response(self):
        seen = []
        def respond(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b'x' * 65537)
        client = GitHubClient(transport=httpx.MockTransport(respond))
        with self.assertRaisesRegex(RuntimeError, 'bounded'):
            await client.mark_pull_request_ready('https://github.example/api/v3', 'PR_fixture',
                token='fixture-token', marker='fixture-marker')
        self.assertEqual(seen, ['https://github.example/api/graphql'])

    async def test_real_intent_lost_response_reconciles_by_read_without_replay(self):
        from codex_web.action_intents import ActionIntentCreate, ActionIntentReconcileRequest
        fixture = intent_fixture.ActionIntentTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture.service.execution = self.f.execution
        fixture.authority.resources = self.f.resources
        worker = fixture.worker_actor.model_copy(update={'service_scopes':
            ('action-intent:worker', 'action-provider:admin', 'secret:admin')})
        intent = fixture.service.create(ActionIntentCreate(binding_id=self.f.binding.id,
            request=self.request), actor=fixture.actor)
        from codex_web.action_intents import ActionIntentClaimRequest
        self.assertIsNotNone(fixture.service.claim(ActionIntentClaimRequest(worker_id='ready-test'),
            actor=worker, intent_id=intent.id))
        self.lost = True
        uncertain = await fixture.service.execute_claimed(intent.id, 'ready-test', actor=worker)
        self.assertEqual(uncertain.status, 'uncertain')
        before = sum(method == 'POST' for method, _ in self.calls)
        settled = await fixture.service.reconcile(intent.id,
            ActionIntentReconcileRequest(retry_if_idempotent=False), actor=worker)
        self.assertEqual(settled.status, 'succeeded', settled.last_error)
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), before)
        history = fixture.service.history(intent.id, fixture.actor)
        self.assertTrue(history['verifications'])
        self.assertEqual(history['receipts'][-1]['outcome'], 'completed')

    async def test_real_broker_denies_default_and_executes_only_explicit_project_declaration(self):
        from tests import test_repository_action_admission as broker_fixture
        from codex_web.resources import ResourceAlias, ResourceUpdate
        from codex_web.services.github_action_provider import GitHubActionProvider
        from codex_web.action_providers import ActionProviderBindingCreate
        from codex_web.secrets import SecretCreate
        from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
        from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        fixture = broker_fixture.RepositoryActionAdmissionTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.a.asyncTearDown)
        self.addAsyncCleanup(fixture.b.asyncTearDown)
        route = '/api/repository-actions/pull-request/ready'
        payload = {'parameters': self.request.parameters, 'idempotency_key': 'fixture-ready'}
        status, _, result = await fixture.b._request('POST', route, payload=payload)
        self.assertEqual(status, 403, result)
        self.assertIn('declaration', result['error']['message'].lower())
        self.assertEqual(fixture.a.service.store.load().intents, [])
        fixture.a.resources.update(fixture.repo.id, ResourceUpdate(aliases=[ResourceAlias(
            namespace='repository', provider='github', value='garnser/codex-web')]), actor=fixture.a.actor)
        provider = GitHubActionProvider(fixture.a.resources, self.f.provider.client)
        fixture.a.registry.register(provider)
        secret = self.f.secrets.create(SecretCreate(name='Private broker fixture', value='secret-github-token',
            allowed_identity_ids=[fixture.b.worker_actor.identity_id]), actor=self.f.actor)
        fixture.a.registry.bind(ActionProviderBindingCreate(provider_type='github', provider_instance='github.com',
            resource_ids=(fixture.repo.id,), credential_ref=secret.id), actor=fixture.a.actor, resources=fixture.a.resources)
        fixture.a.execution.secret_broker = self.f.secrets
        profiles = install_execution_profile_definitions(fixture.a.definition_registry)
        catalog = execution_profile_catalog_seed_payload()
        profile = next(row for row in catalog['profiles'] if row['id'] == 'repository-write')
        self.assertNotIn('repository.pull-request.ready', profile['control_plane_operations'])
        profile['control_plane_operations'].append('repository.pull-request.ready')
        record = fixture.a.definition_registry.create_draft(DefinitionDraftCreate(
            kind='execution-profile-catalog', definition_id='execution.profiles.default',
            definition_schema_version='1.2', payload=catalog, actor='fixture', scope_type='project', scope_id='home'))
        fixture.a.definition_registry.publish(record.record_id, DefinitionPublishRequest(actor='fixture'))
        _, reference = profiles.resolve('repository-write', project_id='home')
        fixture.intent_assignment = fixture.assignment.model_copy(update={'execution_profile_definition': reference})
        fixture.b.current_assignment = fixture.intent_assignment
        fixture.b.broker.snapshot = fixture.intent_assignment.model_copy(deep=True)
        fixture.b.service.execution_profiles = profiles
        fixture.a.service.execution_profiles = profiles
        status, _, result = await fixture.b._request('POST', route, payload=payload)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['item']['status'], 'succeeded', result)
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        original = fixture.intent_assignment
        for kind in ('lease', 'fence', 'deadline', 'profile'):
            fixture.b.current_assignment = original
            self.item['draft'] = True
            def revoke():
                changes = {'lease': original.lease.model_copy(update={'expires_at': 0})} if kind == 'lease' else (
                    {'fence': original.fence + 1} if kind == 'fence' else (
                    {'deadline_at': 0} if kind == 'deadline' else {'execution_profile_id': 'orchestration-only'}))
                fixture.b.current_assignment = original.model_copy(update=changes)
            self.after_read = revoke
            with self.subTest(kind=kind):
                status, _, result = await fixture.b._request('POST', route,
                    payload={**payload, 'idempotency_key': 'revoked-' + kind})
                self.assertEqual(status, 200, result)
                self.assertEqual(result['item']['status'], 'failed', result)
                self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        fixture.b.current_assignment = original
        self.item['draft'] = True
        reads = 0
        def expire_during_verification():
            nonlocal reads
            reads += 1
            if reads == 3:
                fixture.b.current_assignment = original.model_copy(update={
                    'lease': original.lease.model_copy(update={'expires_at': 0})})
        self.after_read = expire_during_verification
        status, _, result = await fixture.b._request('POST', route,
            payload={**payload, 'idempotency_key': 'expired-during-verification'})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['item']['status'], 'requires_reconciliation', result)
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 2)
