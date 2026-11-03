"""Real GitLab fact representation must reach the existing closure owner."""
import unittest
from types import SimpleNamespace

from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
from tests import test_procedure_mr_closure as fixtures
from tests import test_github_merge_requirements as github_fixtures


class MRClosureProviderStateTests(unittest.IsolatedAsyncioTestCase):
    async def closure_fixture(self):
        fixture = fixtures.MRClosureOwnerTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.f.asyncTearDown)
        return fixture

    async def test_actual_gitlab_opened_fact_satisfies_positive_closure_prerequisites(self):
        # Borrow the canonical private SQLite/ActionIntent/CI original and
        # independent current-policy acceptance fixture without collecting its
        # TestCase class a second time. Only provider GET transport is fixed.
        f = await self.closure_fixture()
        calls = []

        async def read(base, path, *, token):
            calls.append(path)
            if path.endswith('/approvals'):
                return {'approved': True, 'approved_by': [],
                    'approvals_required': 0, 'approvals_left': 0}
            if path.endswith('/merge_requests/57'):
                return {'iid': 57, 'project_id': 9, 'sha': fixtures.HEAD,
                    'target_branch': 'main', 'state': 'opened', 'draft': False,
                    'detailed_merge_status': 'mergeable', 'author': {'id': 1},
                    'blocking_discussions_resolved': True,
                    'head_pipeline': {'id': 7, 'sha': fixtures.HEAD, 'status': 'success'}}
            if '/repository/branches/' in path:
                return {'name': 'main', 'commit': {'id': fixtures.BASE}}
            return {'id': 9, 'path_with_namespace': 'group/platform/codex-web'}

        async def requirements(context, scope):
            fact = await GitLabMergeRequirementsReader(
                SimpleNamespace(read_json_bounded=read),
                SimpleNamespace(id=scope.provider_binding_id,
                    provider_instance=f.f.provider.provider_instance, base_url=f.f.provider.api_base),
                f.f.repository, 'group/platform/codex-web', 'private-fixture-only').read('57')
            self.assertIsInstance(fact, CodeHostMergeRequirementsFact)
            self.assertEqual(fact.pull_request_state, 'OPENED')
            self.assertEqual((fact.head_revision, fact.base_revision), (fixtures.HEAD, fixtures.BASE))
            self.assertFalse(fact.changed)
            # Intentionally partial GitLab policy facts are not manufactured
            # complete, nor substituted for independent canonical acceptance.
            self.assertFalse(fact.complete)
            return fact

        f.owner.read_requirements = requirements
        before = f.intents.store.load(intent_id=f.original.id).model_dump(mode='json')
        result = await f.owner.execute(f.context, f.input)
        self.assertEqual(len(calls), 7)
        self.assertEqual(result.disposition, 'succeeded', result.code)
        self.assertEqual(result.outputs,
            {'expected_head_sha': fixtures.HEAD, 'expected_base_sha': fixtures.BASE})
        self.assertEqual(f.intents.store.load(intent_id=f.original.id).model_dump(mode='json'), before)
        self.assertEqual(f.f.client.merge_request_merges, 0)

    async def test_actual_github_reader_open_state_uses_its_own_protocol_vocabulary(self):
        from codex_web.services.procedure_mr_closure import provider_pull_request_is_open
        provider = github_fixtures._Provider()
        fact = await provider.read()
        self.assertEqual((fact.provider_type, fact.pull_request_state), ('github', 'OPEN'))
        before = fact.model_dump(mode='json')
        self.assertTrue(provider_pull_request_is_open(fact))
        self.assertEqual(fact.model_dump(mode='json'), before)
        # A state predicate does not extend the GitLab CI-original closure
        # protocol to GitHub or authorize a provider mutation.
        self.assertTrue(all(request.method == 'GET' or
            b'query MergeRequirements' in request.content for request in provider.calls))

    async def test_exact_provider_state_pairs_refuse_closed_merged_unknown_and_foreign(self):
        from codex_web.services.procedure_mr_closure import provider_pull_request_is_open
        f = await self.closure_fixture()
        scope = f.owner._original(f.context, f.input)[0]
        fact = await f.requirements(f.context, scope)
        for provider, states in (
                ('gitlab', ('OPENED', 'opened')),
                ('github', ('OPEN', 'open'))):
            for state in states:
                with self.subTest(provider=provider, state=state):
                    self.assertTrue(provider_pull_request_is_open(fact.model_copy(update={
                        'provider_type': provider, 'pull_request_state': state})))
        refused = [('gitlab', state) for state in
            ('OPEN', 'open', 'Opened', 'CLOSED', 'closed', 'MERGED', 'merged', 'UNKNOWN', 'unknown', '')]
        refused += [('github', state) for state in
            ('OPENED', 'opened', 'Open', 'CLOSED', 'closed', 'MERGED', 'merged', 'UNKNOWN', 'unknown', '')]
        refused += [(provider, state) for provider in ('foreign', 'GitLab', 'GitHub')
            for state in ('OPENED', 'opened', 'OPEN', 'open')]
        for provider, state in refused:
            with self.subTest(provider=provider, state=state):
                self.assertFalse(provider_pull_request_is_open(fact.model_copy(update={
                    'provider_type': provider, 'pull_request_state': state})))

    async def test_actual_closure_retains_all_non_open_and_scope_guards(self):
        f = await self.closure_fixture()
        before = f.intents.store.load(intent_id=f.original.id).model_dump(mode='json')
        for change in ({'pull_request_state': 'CLOSED'}, {'pull_request_state': 'MERGED'},
                {'pull_request_state': 'unknown'}, {'provider_type': 'foreign'},
                {'head_revision': 'c' * 40}, {'base_revision': 'c' * 40},
                {'binding_id': 'foreign'}, {'provider_instance': 'foreign'},
                {'resource_id': 'foreign'}, {'external_id': '58'},
                {'draft': True}, {'changed': True}):
            with self.subTest(change=change):
                f.meta_changes = {'pull_request_state': 'OPENED', **change}
                result = await f.owner.execute(f.context, f.input)
                self.assertEqual((result.disposition, result.code),
                    ('waiting', 'mr_closure_current_mr_changed'))
                self.assertFalse(result.outputs)
        self.assertEqual(f.intents.store.load(intent_id=f.original.id).model_dump(mode='json'), before)
        self.assertEqual(f.f.client.merge_request_merges, 0)
