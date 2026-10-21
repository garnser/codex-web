import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from codex_web.code_host_merge_requirements import MergeFactAvailability as Availability, MergeFactSource as Source
from codex_web.code_hosts import CodeHostCapability, CodeHostError
from codex_web.integrations.gitlab_client import GitLabClient, GitLabReadIncompleteError
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
from tests import test_code_host_adapters as fixtures


class GitLabMergeRequirementsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binding = fixtures.CodeHostAdapterTests._binding('gitlab')
        self.resource = fixtures.CodeHostAdapterTests._resource('gitlab')
        self.pr = dict(iid=7, project_id=42, sha='a'*40, target_branch='main', state='opened',
            draft=False, detailed_merge_status='mergeable', author={'id': 1},
            blocking_discussions_resolved=True,
            head_pipeline={'id': 11, 'sha': 'a'*40, 'status': 'success'})
        self.approvals = dict(approved=True, approved_by=[], approvals_required=0, approvals_left=0)
        self.branch = dict(name='main', commit={'id':'b'*40})
        self.calls, self.hook, self.status = [], None, None

    def response(self, request):
        self.calls.append(request)
        assert request.method == 'GET'
        assert request.headers['PRIVATE-TOKEN'] == 'fixture-secret'
        path = request.url.path
        if path.endswith('/approvals'):
            value = self.approvals
        elif path.endswith('/merge_requests/7'):
            value = self.pr
        elif '/repository/branches/' in path:
            value = self.branch
        else:
            value = {'id':42,'path_with_namespace':'acme/widgets'}
        value = copy.deepcopy(value)
        if self.hook:
            changed = self.hook(request, value)
            if changed is not None:
                return changed
        if self.status and path.endswith('/approvals'):
            return httpx.Response(self.status, json={'message':'private reflected provider error'})
        return httpx.Response(200, json=value)

    def reader(self, *, checkpoint=None, clock=lambda:1000):
        return GitLabMergeRequirementsReader(GitLabClient(transport=httpx.MockTransport(self.response)),
            self.binding,self.resource,'acme/widgets','fixture-secret',checkpoint=checkpoint,clock=clock)

    async def test_native_gates_are_useful_without_claiming_full_paid_policy(self):
        fact = await self.reader().read('7')
        self.assertEqual(fact.merge_state,'mergeable')
        self.assertEqual(fact.mergeable,'MERGEABLE')
        self.assertEqual(fact.review_decision,'APPROVED')
        self.assertEqual(fact.base_revision,'b'*40) # exact target tip, not diff merge-base
        self.assertEqual(fact.observed_checks[0].revision,'a'*40)
        self.assertEqual(fact.observed_checks[0].state,'success')
        self.assertEqual(fact.reviews.approving_count,0)
        self.assertFalse(fact.complete)
        self.assertIsNone(fact.reviews.code_owner_required)
        self.assertIsNone(fact.strict_checks_required)
        self.assertEqual(len(self.calls),7)
        self.assertTrue(all('/approval_state' not in str(r.url) for r in self.calls))
        self.assertNotIn('fixture-secret',fact.model_dump_json())
        self.assertIn(CodeHostCapability.MERGE_REQUIREMENTS_READ,GitLabCodeHostProvider().capabilities())

    async def test_community_edition_missing_counts_are_not_zero(self):
        self.approvals = {'approved':True,'approved_by':[{'user':{'id':2}}],
                          'user_can_approve':False,'user_has_approved':False}
        fact = await self.reader().read('7')
        self.assertEqual(fact.review_decision,'APPROVED')
        self.assertIsNone(fact.reviews.approving_count)
        self.assertEqual(fact.independent_approval_count,1)
        self.assertIsNone(fact.current_head_approval_count)
        self.assertFalse(fact.complete)

    async def test_optional_ce_absence_is_not_a_mandatory_review_failure(self):
        self.approvals = {'approved':False,'approved_by':[]}
        fact = await self.reader().read('7')
        self.assertEqual(fact.review_decision,'NOT_APPROVED')
        self.assertEqual(fact.merge_state,'mergeable')
        self.assertNotIn('provider_review_gate_not_approved',fact.blockers)
        self.assertIsNone(fact.reviews.approving_count)

    async def test_author_approval_is_not_independent_or_current_head_proof(self):
        self.approvals['approved_by']=[{'user':{'id':1}}]
        fact=await self.reader().read('7')
        self.assertEqual(fact.independent_approval_count,0)
        self.assertIsNone(fact.current_head_approval_count)

    async def test_native_negative_gates_and_failed_pipeline_are_retained(self):
        self.pr.update(detailed_merge_status='not_approved',blocking_discussions_resolved=False)
        self.pr['head_pipeline']['status']='failed'
        self.approvals.update(approved=False,approvals_required=2,approvals_left=2)
        fact=await self.reader().read('7')
        self.assertEqual(fact.review_decision,'REVIEW_REQUIRED')
        self.assertEqual(fact.reviews.approving_count,2)
        self.assertEqual(fact.observed_checks[0].state,'failure')
        self.assertIn('provider_merge_gate_not_clean',fact.blockers)
        self.assertIn('provider_review_gate_not_approved',fact.blockers)
        self.assertIn('unresolved_conversations',fact.blockers)

    async def test_denied_and_unsupported_approvals_remain_explicit(self):
        for status,expected in [(401,Availability.DENIED),(403,Availability.DENIED),
                                (404,Availability.UNSUPPORTED),(500,Availability.UNKNOWN)]:
            with self.subTest(status=status):
                self.status=status
                fact=await self.reader().read('7')
                source=next(s for s in fact.sources if s.source==Source.REVIEWS)
                self.assertEqual(source.availability,expected)
                self.assertIsNone(fact.review_decision)
                self.assertIsNone(fact.reviews.approving_count)
                self.assertNotIn('private reflected',fact.model_dump_json())

    async def test_wrong_pipeline_revision_is_not_current_head_success(self):
        self.pr['head_pipeline']['sha']='c'*40
        fact=await self.reader().read('7')
        self.assertEqual(fact.observed_checks,())
        self.assertEqual(next(s for s in fact.sources if s.source==Source.CHECKS).reason_code,'pipeline_head_unproven')

    async def test_scope_and_required_boolean_shape_fail_closed(self):
        for field,value in [('project_id',43),('iid',8),('draft','false')]:
            with self.subTest(field=field):
                original=self.pr[field];self.pr[field]=value
                with self.assertRaises(CodeHostError):await self.reader().read('7')
                self.pr[field]=original

    async def test_head_target_gate_and_approval_changes_invalidate_observation(self):
        for kind in ['head','target','gate','approval']:
            with self.subTest(kind=kind):
                counts={}
                def hook(request,value):
                    path=request.url.path;counts[path]=counts.get(path,0)+1
                    if counts[path]!=2:return
                    if kind=='head' and path.endswith('/merge_requests/7'):value['sha']='c'*40
                    if kind=='target' and '/repository/branches/' in path:value['commit']['id']='c'*40
                    if kind=='gate' and path.endswith('/merge_requests/7'):value['detailed_merge_status']='checking'
                    if kind=='approval' and path.endswith('/approvals'):value['approved']=False
                self.hook=hook
                fact=await self.reader().read('7')
                self.assertTrue(fact.changed)
                self.assertFalse(fact.complete)
                self.assertIn('observation_changed',fact.blockers)
        self.hook=None

    async def test_revoked_canonical_checkpoint_escapes_including_provider_error(self):
        class Revoked(Exception):pass
        calls=0
        async def guard():
            nonlocal calls
            calls+=1
            if calls==9:raise Revoked()
        self.status=403
        with self.assertRaises(Revoked):await self.reader(checkpoint=guard).read('7')
        self.assertEqual(len(self.calls),4)

    async def test_duplicate_or_overbudget_approvals_do_not_become_empty(self):
        for rows in [[{'user':{'id':2}}]*2,[{'user':{'id':i+2}} for i in range(501)]]:
            self.approvals['approved_by']=rows
            fact=await self.reader().read('7')
            self.assertIsNone(fact.review_decision)
            self.assertIsNone(fact.independent_approval_count)
            self.assertFalse(fact.complete)

    async def test_projection_keeps_native_unknown_status_not_guessing_clean(self):
        self.pr['detailed_merge_status']='future_provider_gate'
        fact=await self.reader().read('7')
        self.assertEqual(fact.merge_state,'future_provider_gate')
        self.assertEqual(fact.mergeable,'UNKNOWN')
        self.assertIn('provider_merge_gate_not_clean',fact.blockers)

    async def test_read_is_single_use_and_invalid_iid_never_calls_provider(self):
        reader=self.reader();await reader.read('7')
        with self.assertRaises(CodeHostError):await reader.read('7')
        before=len(self.calls)
        for value in ['../7','0','2147483648']:
            with self.assertRaises(CodeHostError):await self.reader().read(value)
        self.assertEqual(len(self.calls),before)

    async def test_whole_read_deadline_expires_without_mutation(self):
        timeout = asyncio.timeout
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200,json={})
        reader=self.reader()
        reader.client=GitLabClient(transport=httpx.MockTransport(slow))
        with patch('codex_web.services.gitlab_merge_requirements.asyncio.timeout', side_effect=lambda _: timeout(0.001)):
            with self.assertRaises(CodeHostError):await reader.read('7')
        self.assertEqual(reader.calls,1)

    async def test_provider_entrypoint_uses_projection(self):
        provider=GitLabCodeHostProvider(GitLabClient(transport=httpx.MockTransport(self.response)))
        fact=await provider.merge_requirements(self.binding,self.resource,'7',credential='fixture-secret')
        self.assertEqual(fact.external_id,'7')


class GitLabBoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_and_json_limits_are_fail_closed(self):
        responses=[httpx.Response(302,headers={'Location':'https://unrelated.invalid'}),
            httpx.Response(200,content='{"x":1,"x":2}',headers={'content-type':'application/json'}),
            httpx.Response(200,content='{"x":NaN}',headers={'content-type':'application/json'}),
            httpx.Response(200,content='"scalar"',headers={'content-type':'application/json'}),
            httpx.Response(200,content='{}',headers={'content-type':'text/html'}),
            httpx.Response(200,json={'x':'a'*100})]
        for response in responses:
            client=GitLabClient(transport=httpx.MockTransport(lambda request:response))
            with self.subTest(response=response):
                with self.assertRaises(GitLabReadIncompleteError):
                    await client.read_json_bounded('https://gitlab.example/api/v4','fixed',token='secret',max_bytes=64)

    async def test_cancellation_is_not_swallowed(self):
        async def response(request):raise asyncio.CancelledError()
        client=GitLabClient(transport=httpx.MockTransport(response))
        with self.assertRaises(asyncio.CancelledError):
            await client.read_json_bounded('https://gitlab.example/api/v4','fixed',token='secret')
