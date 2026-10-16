import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import httpx

from codex_web.code_host_merge_requirements import MergeFactAvailability as Availability, MergeFactSource as Source
from codex_web.code_hosts import CodeHostError
from codex_web.integrations.github_client import GitHubClient
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.github_merge_requirements import GitHubMergeRequirementsReader
from tests import test_code_host_adapters as fixtures


class _Provider:
    head, base, repository_id = 'a' * 40, 'b' * 40, 987654321

    def __init__(self):
        self.resource = fixtures.CodeHostAdapterTests._resource('github')
        self.resource = self.resource.model_copy(update={'provenance': self.resource.provenance.model_copy(
            update={'external_id': str(self.repository_id)})})
        self.binding = fixtures.CodeHostAdapterTests._binding('github')
        self.branch = 'main'
        self.legacy = {'required_status_checks': {'strict': True, 'contexts': ['unit', 'browser'],
            'checks': [{'context': context, 'app_id': 15368} for context in ['unit', 'browser']]},
            'required_pull_request_reviews': {'required_approving_review_count': 0,
                'require_code_owner_reviews': False, 'require_last_push_approval': False, 'dismiss_stale_reviews': False},
            'required_conversation_resolution': {'enabled': True}}
        self.rules, self.reviews, self.threads = [], [], [{'id': 'thread-one', 'isResolved': True}]
        self.runs = [self.check(index, context) for index, context in enumerate(['unit', 'browser'])]
        self.statuses, self.calls, self.counts = [], [], {}
        self.hook = None
        self.clock = 1000
        self.gate_changes = {}
        self.web_url = 'https://github.example/acme/widgets/pull/7'

    def check(self, index, context='extra'):
        return {'id': 116740899271 + index, 'name': context, 'head_sha': self.head,
            'app': {'id': 15368}, 'status': 'completed', 'conclusion': 'success',
            'completed_at': '2026-10-16T00:00:00Z'}

    def review(self, index, *, state='APPROVED', author=None, login=None):
        return {'id': f'review-{index}', 'state': state,
            'author': {'id': author or f'user-{index}', 'login': login or f'reviewer-{index}'},
            'submittedAt': datetime.fromtimestamp(1792108800 + index, timezone.utc).isoformat(),
            'commit': {'oid': self.head}}

    def rule(self, kind='pull_request', **changes):
        parameters = {'required_approving_review_count': 2, 'require_code_owner_review': True,
            'require_last_push_approval': True, 'dismiss_stale_reviews_on_push': True,
            'required_review_thread_resolution': True}
        parameters.update(changes)
        return {'type': kind, 'ruleset_id': 987654321, 'ruleset_source_type': 'Organization',
                'ruleset_source': 'acme', 'parameters': parameters}

    def identity(self):
        return {'id': 'pr-node', 'number': 7, 'headRefOid': self.head, 'baseRefOid': self.base, 'baseRefName': self.branch}

    def legacy_graphql(self):
        if self.legacy is None:
            return None
        reviews, checks = self.legacy.get('required_pull_request_reviews'), self.legacy.get('required_status_checks')
        return {'id': 'legacy-rule', 'requiresApprovingReviews': reviews is not None,
            'isAdminEnforced': True, 'requiresCommitSignatures': False, 'requiresDeployments': False,
            'requiresLinearHistory': False, 'restrictsPushes': False, 'lockBranch': False,
            'requiredApprovingReviewCount': (reviews or {}).get('required_approving_review_count', 0),
            'requiresCodeOwnerReviews': (reviews or {}).get('require_code_owner_reviews', False),
            'requireLastPushApproval': (reviews or {}).get('require_last_push_approval', False),
            'dismissesStaleReviews': (reviews or {}).get('dismiss_stale_reviews', False),
            'requiresConversationResolution': self.legacy.get('required_conversation_resolution', {}).get('enabled', False),
            'requiresStatusChecks': checks is not None, 'requiresStrictStatusChecks': (checks or {}).get('strict', False)}

    def connection(self, rows, cursor):
        offset = int(cursor or 0)
        more = offset + 100 < len(rows)
        return {'nodes': rows[offset:offset + 100], 'totalCount': len(rows),
                'pageInfo': {'hasNextPage': more, 'endCursor': str(offset + 100) if more else None}}

    def response(self, request):
        self.calls.append(request)
        key = request.method + ':' + request.url.path
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.hook:
            override = self.hook(request, self.counts[key])
            if override is not None:
                return override
        path = request.url.path
        if request.method == 'POST':
            body = json.loads(request.content)
            assert body['query'].startswith('query MergeRequirements') and 'mutation' not in body['query']
            assert body['variables']['owner'] == 'acme' and body['variables']['name'] == 'widgets'
            assert body['variables']['number'] == 7
            pr = self.identity()
            if 'query MergeRequirementsGates' in body['query']:
                pr.update(state='OPEN', isDraft=False, mergeable='MERGEABLE', mergeStateStatus='CLEAN',
                    reviewDecision='APPROVED' if self.reviews else None,
                    baseRef={'name': self.branch, 'branchProtectionRule': self.legacy_graphql()})
                pr.update(self.gate_changes)
            elif 'query MergeRequirementsReviews' in body['query']:
                pr['reviews'] = self.connection(self.reviews, body['variables']['cursor'])
            else:
                pr['reviewThreads'] = self.connection(self.threads, body['variables']['cursor'])
            return httpx.Response(200, json={'data': {'repository': {'databaseId': self.repository_id, 'pullRequest': pr}}})
        page = int(request.url.params.get('page', 1))
        offset = (page - 1) * 100
        if path.endswith('/protection'):
            return httpx.Response(404, json={'message': 'Branch not protected'}) if self.legacy is None else httpx.Response(200, json=self.legacy)
        if '/rules/branches/' in path:
            return httpx.Response(200, json=self.rules[offset:offset + 100])
        if path.endswith('/check-runs'):
            return httpx.Response(200, json={'total_count': len(self.runs), 'check_runs': self.runs[offset:offset + 100]})
        if path.endswith('/status'):
            return httpx.Response(200, json={'sha': self.head, 'total_count': len(self.statuses),
                                           'statuses': self.statuses[offset:offset + 100]})
        if path.endswith('/pulls/7'):
            return httpx.Response(200, json={'node_id': 'pr-node', 'number': 7, 'state': 'open', 'draft': False,
                'html_url': self.web_url,
                'user': {'node_id': 'author-node', 'login': 'author'}, 'head': {'sha': self.head},
                'base': {'sha': self.base, 'ref': self.branch, 'repo': {'id': self.repository_id}}})
        if path == '/repos/acme/widgets':
            return httpx.Response(200, json={'id': self.repository_id, 'full_name': 'acme/widgets'})
        raise AssertionError(f'unexpected provider read: {request.method} {path}')

    async def read(self, *, adapter=False):
        client = GitHubClient(transport=httpx.MockTransport(self.response))
        if adapter:
            return await GitHubCodeHostProvider(client).merge_requirements(self.binding, self.resource, '7', credential='fixture-token')
        return await GitHubMergeRequirementsReader(client, self.binding, self.resource,
            'acme/widgets', 'fixture-token', clock=lambda: self.clock).read('7')


class GitHubMergeRequirementsTests(unittest.IsolatedAsyncioTestCase):
    def sources(self, fact):
        return {source.source: source for source in fact.sources}

    async def test_adapter_observes_explicit_zero_reviews_and_exact_required_apps(self):
        provider = _Provider()
        fact = await provider.read(adapter=True)
        self.assertTrue(fact.complete)
        self.assertEqual(fact.web_url, provider.web_url)
        self.assertFalse(fact.blockers)
        self.assertEqual(fact.reviews.approving_count, 0)
        self.assertEqual(fact.independent_approval_count, 0)
        self.assertTrue(fact.conversation_resolution_required)
        self.assertEqual(fact.unresolved_conversation_count, 0)
        self.assertEqual({item.provider_app_id for item in fact.required_checks}, {'15368'})
        self.assertEqual(fact.head_revision, provider.head)
        self.assertEqual(fact.base_revision, provider.base)
        self.assertTrue(fact.legacy_admin_enforced)
        self.assertEqual(self.sources(fact)[Source.LEGACY_PROTECTION].rule_ids, ('legacy-rule',))
        self.assertEqual(fact.observed_checks[0].provider_conclusion, 'success')
        self.assertTrue(all(request.method in {'GET', 'POST'} for request in provider.calls))

    async def test_provider_links_cannot_change_authority_repository_or_pull_request(self):
        for url in ('javascript:alert(1)', 'https://elsewhere.example/acme/widgets/pull/7',
                    'https://user@github.example/acme/widgets/pull/7',
                    'https://github.example/other/repo/pull/7',
                    'https://github.example/acme/widgets/pull/8'):
            with self.subTest(url=url):
                provider = _Provider()
                provider.web_url = url
                fact = await provider.read()
                self.assertTrue(fact.complete)
                self.assertIsNone(fact.web_url)

    async def test_effective_org_rules_combine_with_legacy_and_independent_reviews(self):
        provider = _Provider()
        provider.rules = [provider.rule()]
        provider.reviews = [provider.review(1), provider.review(2), provider.review(3, author='author-node', login='renamed-author')]
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertFalse(fact.blockers)
        self.assertEqual(fact.reviews.approving_count, 2)
        self.assertEqual(fact.independent_approval_count, 2)
        self.assertTrue(fact.reviews.code_owner_required)
        self.assertTrue(fact.reviews.last_push_approval_required)
        self.assertTrue(fact.reviews.dismiss_stale_approvals)
        self.assertEqual(self.sources(fact)[Source.EFFECTIVE_RULES].rule_ids,
                         ('ruleset:Organization:acme:987654321',))

    async def test_legacy_absence_needs_graphql_confirmation_and_does_not_erase_rules(self):
        provider = _Provider()
        provider.legacy = None
        provider.rules = [provider.rule()]
        fact = await provider.read()
        self.assertEqual(self.sources(fact)[Source.LEGACY_PROTECTION].availability, Availability.ABSENT)
        self.assertEqual(fact.reviews.approving_count, 2)
        self.assertTrue(fact.complete)
        self.assertIn('independent_approvals_missing', fact.blockers)
        self.assertIn('provider_review_gate_not_approved', fact.blockers)

    async def test_denial_unsupported_and_malformed_policy_never_become_zero(self):
        for code in (403, 404, 500):
            provider = _Provider()
            provider.hook = lambda request, n: httpx.Response(code) if '/rules/branches/' in request.url.path else None
            fact = await provider.read()
            with self.subTest(code=code):
                self.assertFalse(fact.complete)
                self.assertIsNone(fact.reviews.approving_count)
                self.assertIsNone(fact.requirements_fingerprint)
                self.assertTrue(fact.blockers)
        provider = _Provider()
        provider.legacy['required_pull_request_reviews']['required_approving_review_count'] = False
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)

    async def test_initial_denial_is_not_erased_by_a_later_empty_response(self):
        provider = _Provider()
        provider.hook = lambda request, n: httpx.Response(403) if '/rules/branches/' in request.url.path and n == 1 else None
        fact = await provider.read()
        self.assertEqual(self.sources(fact)[Source.EFFECTIVE_RULES].availability, Availability.DENIED)
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)

    async def test_rule_addition_removal_and_same_head_policy_changes_invalidate_read(self):
        for change in ('add', 'remove', 'update'):
            provider = _Provider()
            if change == 'remove':
                provider.legacy = None
            def hook(request, n):
                if request.url.path.endswith('/protection') and n == 2:
                    if change == 'add':
                        provider.legacy = None
                    elif change == 'remove':
                        provider.legacy = _Provider().legacy
                    else:
                        provider.legacy['required_pull_request_reviews']['required_approving_review_count'] = 2
            provider.hook = hook
            fact = await provider.read()
            with self.subTest(change=change):
                self.assertTrue(fact.changed)
                self.assertFalse(fact.complete)

    async def test_head_or_base_change_during_observation_is_not_current_evidence(self):
        for field in ('head', 'base'):
            provider = _Provider()
            def hook(request, n):
                if request.url.path.endswith('/status'):
                    setattr(provider, field, 'c' * 40)
            provider.hook = hook
            fact = await provider.read()
            with self.subTest(field=field):
                self.assertTrue(fact.changed)
                self.assertFalse(fact.complete)

    async def test_paginated_checks_reviews_and_threads_are_complete_only_at_real_end(self):
        provider = _Provider()
        provider.runs += [provider.check(index + 1000, f'job-{index}') for index in range(100)]
        provider.reviews = [provider.review(index) for index in range(101)]
        provider.threads = [{'id': f'thread-{index}', 'isResolved': True} for index in range(101)]
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertEqual(len(fact.observed_checks), 102)
        self.assertEqual(fact.independent_approval_count, 101)
        self.assertEqual(fact.conversation_count, 101)
        provider = _Provider()
        provider.rules = [{**provider.rule(kind='non_fast_forward'), 'ruleset_id': index + 1} for index in range(500)]
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertEqual(self.sources(fact)[Source.EFFECTIVE_RULES].availability, Availability.TRUNCATED)

    async def test_wrong_app_or_ambiguous_duplicate_cannot_satisfy_required_context(self):
        for mutate in (lambda p: p.runs[0].update(app={'id': 99}),
                       lambda p: p.runs.append({**p.runs[0], 'id': 99999999999})):
            provider = _Provider()
            mutate(provider)
            fact = await provider.read()
            self.assertIn('required_checks_unmet_or_ambiguous', fact.blockers)

    async def test_stale_observation_unsupported_rules_and_malformed_threads_fail_closed(self):
        provider = _Provider()
        provider.hook = lambda request, n: setattr(provider, 'clock', 1031) if request.url.path.endswith('/status') else None
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIn('observation_stale', fact.blockers)
        provider = _Provider()
        provider.rules = [provider.rule(kind='future_rule')]
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIn('future_rule', fact.unsupported_rules)
        provider = _Provider()
        provider.threads[0]['isResolved'] = 'true'
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertEqual(self.sources(fact)[Source.CONVERSATIONS].availability, Availability.UNKNOWN)
        self.assertIsNone(fact.unresolved_conversation_count)

    async def test_scope_mismatch_and_invalid_locator_stop_before_facts_escape(self):
        provider = _Provider()
        provider.resource = provider.resource.model_copy(update={'provenance': provider.resource.provenance.model_copy(
            update={'external_id': '1'})})
        with self.assertRaises(CodeHostError):
            await provider.read()
        self.assertEqual(len(provider.calls), 1)

    async def test_legacy_404_without_graphql_confirmation_is_unknown_not_absence(self):
        provider = _Provider()
        def hook(request, n):
            if request.url.path.endswith('/protection'):
                return httpx.Response(404)
        provider.hook = hook
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)
        self.assertEqual(self.sources(fact)[Source.LEGACY_PROTECTION].availability, Availability.UNKNOWN)

    async def test_legacy_initial_denial_is_not_overwritten_by_later_confirmed_absence(self):
        provider = _Provider()
        provider.legacy = None
        provider.hook = lambda request, n: httpx.Response(403) if request.url.path.endswith('/protection') and n == 1 else None
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)
        self.assertEqual(self.sources(fact)[Source.LEGACY_PROTECTION].availability, Availability.DENIED)

    async def test_graphql_and_rest_policy_disagreement_does_not_choose_weaker_requirements(self):
        provider = _Provider()
        rule = provider.legacy_graphql()
        rule['requiredApprovingReviewCount'] = 2
        provider.gate_changes['baseRef'] = {'name': 'main', 'branchProtectionRule': rule}
        fact = await provider.read()
        self.assertTrue(fact.changed)
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)

    async def test_constraints_not_returned_by_legacy_rest_are_explicitly_unsupported(self):
        for feature in ('requiresCommitSignatures', 'requiresDeployments', 'requiresLinearHistory', 'restrictsPushes', 'lockBranch'):
            provider = _Provider()
            rule = provider.legacy_graphql()
            rule[feature] = True
            provider.gate_changes['baseRef'] = {'name': 'main', 'branchProtectionRule': rule}
            fact = await provider.read()
            with self.subTest(feature=feature):
                self.assertFalse(fact.complete)
                self.assertTrue(fact.blockers)
                self.assertIn(f'legacy.{feature}', fact.unsupported_rules)
                self.assertEqual(self.sources(fact)[Source.LEGACY_PROTECTION].rule_ids, ('legacy-rule',))

    async def test_review_identity_survives_login_changes_and_comments_do_not_erase_approval(self):
        provider = _Provider()
        provider.reviews = [provider.review(1, author='one', login='old-login'),
            provider.review(2, author='one', login='new-login'),
            provider.review(3, author='one', state='COMMENTED'),
            provider.review(4, author='author-node', login='renamed-pr-author')]
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertEqual(fact.independent_approval_count, 1)
        self.assertEqual(fact.current_head_approval_count, 1)
        provider.reviews.append(provider.review(5, author='one', state='CHANGES_REQUESTED'))
        provider.gate_changes['reviewDecision'] = 'CHANGES_REQUESTED'
        fact = await provider.read()
        self.assertEqual(fact.independent_approval_count, 0)
        self.assertIn('provider_review_gate_not_approved', fact.blockers)

    async def test_unknown_review_states_and_equal_time_opinions_are_not_zero_approvals(self):
        for ambiguous in (False, True):
            provider = _Provider()
            provider.reviews = [provider.review(1, author='one')]
            if ambiguous:
                provider.reviews.append({**provider.reviews[0], 'id': 'another-review', 'state': 'CHANGES_REQUESTED'})
            else:
                provider.reviews[0]['state'] = 'FUTURE_REVIEW_STATE'
            fact = await provider.read()
            self.assertFalse(fact.complete)
            self.assertIsNone(fact.independent_approval_count)
            self.assertEqual(self.sources(fact)[Source.REVIEWS].availability, Availability.UNKNOWN)
        provider = _Provider()
        provider.reviews = [{'id': 'pending', 'state': 'PENDING', 'author': None, 'submittedAt': None, 'commit': None}]
        provider.reviews.append({'id': 'commented', 'state': 'COMMENTED', 'author': None, 'submittedAt': None, 'commit': None})
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertEqual(fact.independent_approval_count, 0)

    async def test_missing_or_duplicate_connection_pages_do_not_claim_complete_conversations(self):
        provider = _Provider()
        provider.threads = [{'id': f'thread-{index}', 'isResolved': True} for index in range(101)]
        def hook(request, n):
            if request.method == 'POST' and 'query MergeRequirementsThreads' in json.loads(request.content)['query']:
                pr = provider.identity()
                connection = provider.connection(provider.threads, None)
                connection['pageInfo'] = {'hasNextPage': False, 'endCursor': None}
                pr['reviewThreads'] = connection
                return httpx.Response(200, json={'data': {'repository': {'databaseId': provider.repository_id, 'pullRequest': pr}}})
        provider.hook = hook
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.unresolved_conversation_count)
        self.assertTrue(fact.blockers)

    async def test_legacy_status_cannot_borrow_an_app_identity_from_a_same_named_check(self):
        provider = _Provider()
        provider.statuses = [{'id': 116740899271, 'context': 'unit', 'state': 'success', 'updated_at': '2026-10-16T00:00:00Z'}]
        fact = await provider.read()
        self.assertIn('required_checks_unmet_or_ambiguous', fact.blockers)
        self.assertFalse(next(check for check in fact.observed_checks if check.kind == 'status').publisher_known)
        provider.legacy['required_status_checks']['checks'][0]['app_id'] = None
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertNotIn('required_checks_unmet_or_ambiguous', fact.blockers)

    async def test_rule_order_does_not_change_fingerprint_or_invalidate_observation(self):
        provider = _Provider()
        provider.rules = [{**provider.rule(kind='non_fast_forward'), 'ruleset_id': value} for value in (1, 2)]
        def hook(request, n):
            if '/rules/branches/' in request.url.path and n == 2:
                provider.rules.reverse()
        provider.hook = hook
        first = await provider.read()
        second = await provider.read()
        self.assertTrue(first.complete)
        self.assertFalse(first.changed)
        self.assertEqual(first.requirements_fingerprint, second.requirements_fingerprint)

    async def test_ghe_routing_and_escaped_target_branch_remain_engine_owned(self):
        provider = _Provider()
        provider.branch = 'release/stable'
        provider.binding = provider.binding.model_copy(update={'base_url': 'https://github.example/api/v3'})
        original = provider.response
        actual = []
        def ghe(request):
            actual.append(request)
            if request.url.path.startswith('/api/v3/'):
                mapped = httpx.Request(request.method, request.url.copy_with(path=request.url.path.removeprefix('/api/v3')),
                                       headers=request.headers, content=request.content)
                return original(mapped)
            return original(request)
        provider.response = ghe
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertEqual(fact.target_branch, provider.branch)
        self.assertTrue(any(b'/branches/release%2fstable/protection' in request.url.raw_path.lower() for request in actual))
        self.assertTrue(all(request.url.path == '/api/graphql' for request in actual if request.method == 'POST'))

    async def test_primary_provider_denial_preserves_http_status_and_cancellation_does_not_retry(self):
        provider = _Provider()
        provider.hook = lambda request, n: httpx.Response(403)
        with self.assertRaises(CodeHostError) as raised:
            await provider.read()
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(provider.calls), 1)
        provider = _Provider()
        entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def transport(request):
            if request.method == 'POST':
                entered.set()
                try:
                    await release.wait()
                finally:
                    exited.set()
            return provider.response(request)
        reader = GitHubMergeRequirementsReader(GitHubClient(transport=httpx.MockTransport(transport)),
            provider.binding, provider.resource, 'acme/widgets', 'fixture-token')
        task = asyncio.create_task(reader.read('7'))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())
        self.assertEqual(len(provider.calls), 2)

    async def test_projection_byte_limit_retains_scope_and_marks_omitted_requirements_unknown(self):
        provider = _Provider()
        provider.runs += [provider.check(index + 1000, f'job-{index}-' + 'x' * 900) for index in range(300)]
        fact = await provider.read()
        self.assertLessEqual(len(fact.model_dump_json().encode()), 128 * 1024)
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)
        self.assertFalse(fact.required_checks)
        self.assertEqual(fact.blockers, ('projection_byte_limit',))
        self.assertTrue(all(source.availability == Availability.TRUNCATED for source in fact.sources))
        self.assertEqual(fact.head_revision, provider.head)

    async def test_a_reader_cannot_reuse_previous_scope_or_facts_for_another_request(self):
        provider = _Provider()
        reader = GitHubMergeRequirementsReader(GitHubClient(transport=httpx.MockTransport(provider.response)),
            provider.binding, provider.resource, 'acme/widgets', 'fixture-token')
        await reader.read('7')
        count_before = len(provider.calls)
        with self.assertRaises(CodeHostError):
            await reader.read('8')
        self.assertEqual(len(provider.calls), count_before)

    async def test_stronger_legacy_minimum_and_distinct_required_apps_are_both_retained(self):
        provider = _Provider()
        provider.legacy['required_pull_request_reviews']['required_approving_review_count'] = 3
        checks = provider.rule(kind='required_status_checks')
        checks['parameters'] = {'strict_required_status_checks_policy': False,
            'required_status_checks': [{'context': 'unit', 'integration_id': 99}]}
        provider.rules = [provider.rule(), checks]
        provider.reviews = [provider.review(index) for index in range(3)]
        provider.runs.append({**provider.check(500, 'unit'), 'app': {'id': 99}})
        fact = await provider.read()
        self.assertTrue(fact.complete)
        self.assertFalse(fact.blockers)
        self.assertEqual(fact.reviews.approving_count, 3)
        self.assertTrue(fact.strict_checks_required)
        self.assertEqual({check.provider_app_id for check in fact.required_checks if check.context == 'unit'}, {'15368', '99'})
        checks['parameters']['strict_required_status_checks_policy'] = 'false'
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)

    async def test_unknown_native_check_state_does_not_become_success_or_pending(self):
        provider = _Provider()
        provider.runs[0]['status'] = 'future_status'
        fact = await provider.read()
        self.assertEqual(fact.observed_checks[0].state, 'unknown')
        self.assertIn('required_checks_unmet_or_ambiguous', fact.blockers)

    async def test_generic_404_with_null_graphql_rule_is_still_unknown(self):
        provider = _Provider()
        provider.legacy = None
        provider.hook = lambda request, n: httpx.Response(404, json={'message': 'Not Found'}) if request.url.path.endswith('/protection') else None
        fact = await provider.read()
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)
        self.assertEqual(self.sources(fact)[Source.LEGACY_PROTECTION].availability, Availability.UNKNOWN)

    async def test_canonical_guard_is_checked_before_and_after_reads_and_is_not_swallowed(self):
        for fail_at in (1, 2, 9):
            provider = _Provider()
            calls = []
            rejection = ValueError('current canonical admission rejected')
            async def guard():
                calls.append(True)
                if len(calls) == fail_at:
                    raise rejection
            reader = GitHubMergeRequirementsReader(GitHubClient(transport=httpx.MockTransport(provider.response)),
                provider.binding, provider.resource, 'acme/widgets', 'fixture-token', checkpoint=guard)
            with self.subTest(fail_at=fail_at), self.assertRaises(ValueError) as raised:
                await reader.read('7')
            self.assertIs(raised.exception, rejection)
            self.assertEqual(len(provider.calls), fail_at // 2)

    async def test_whole_read_deadline_closes_transport_without_returning_zero_requirements(self):
        provider = _Provider()
        exited = asyncio.Event()
        async def transport(request):
            if request.method == 'POST':
                try:
                    await asyncio.Event().wait()
                finally:
                    exited.set()
            return provider.response(request)
        reader = GitHubMergeRequirementsReader(GitHubClient(transport=httpx.MockTransport(transport)),
            provider.binding, provider.resource, 'acme/widgets', 'fixture-token')
        deadline = asyncio.timeout
        with patch('codex_web.services.github_merge_requirements.asyncio.timeout', side_effect=lambda seconds: deadline(0)):
            fact = await reader.read('7')
        self.assertTrue(exited.is_set())
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.approving_count)
        self.assertIsNone(fact.independent_approval_count)
        self.assertTrue(fact.blockers)

    async def test_canonical_revocation_after_a_provider_error_is_not_a_partial_fact(self):
        provider = _Provider()
        revoked = False
        def hook(request, n):
            nonlocal revoked
            if '/rules/branches/' in request.url.path:
                revoked = True
                return httpx.Response(403)
        async def guard():
            if revoked:
                raise PermissionError('read authority revoked')
        provider.hook = hook
        reader = GitHubMergeRequirementsReader(GitHubClient(transport=httpx.MockTransport(provider.response)),
            provider.binding, provider.resource, 'acme/widgets', 'fixture-token', checkpoint=guard)
        with self.assertRaises(PermissionError):
            await reader.read('7')
