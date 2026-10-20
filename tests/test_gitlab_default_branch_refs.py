"""Bounded GitLab default-tip reads with synthetic provider transport only."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from codex_web.code_hosts import CodeHostError
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceProvenance
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from tests import test_code_host_adapters as adapters
from tests import test_code_hosts as fixtures


HEAD = 'a' * 40
ROOT = '/api/v4/projects/acme%2Fwidgets'


class GitLabDefaultBranchRefTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, *, branches=None, tags=None, default='main', exact=None, exact_status=200):
        calls = []
        branches = branches if branches is not None else [
            {'name': f'aaa-{i:03}', 'default': False, 'commit': {'id': 'b' * 40}}
            for i in range(100)]
        tags = tags if tags is not None else []
        exact = exact if exact is not None else {'name': default, 'default': True, 'commit': {'id': HEAD}}
        def handler(request):
            path = request.url.raw_path.decode().split('?', 1)[0]
            calls.append((path, dict(request.url.params), request.headers.get('PRIVATE-TOKEN')))
            if path == ROOT:
                return httpx.Response(200, json={'id':42, 'name':'widgets',
                    'path_with_namespace':'acme/widgets', 'default_branch':default,
                    'web_url':'https://gitlab.example/acme/widgets'})
            if path == ROOT + '/repository/branches':
                return httpx.Response(200, json=branches)
            if path == ROOT + '/repository/tags':
                return httpx.Response(200, json=tags)
            if path == ROOT + '/repository/branches/' + str(default).replace('/', '%2F'):
                value = exact() if callable(exact) else exact
                return httpx.Response(exact_status, json=value)
            if path == ROOT + '/repository/commits/' + HEAD:
                return httpx.Response(200, json={'id':HEAD, 'message':'provider-attested commit'})
            return httpx.Response(404, json={'message':'not found'})
        return GitLabCodeHostProvider(GitLabClient(transport=httpx.MockTransport(handler))), calls

    async def refs(self, provider):
        return await provider.refs(adapters.CodeHostAdapterTests._binding('gitlab'),
            adapters.CodeHostAdapterTests._resource('gitlab'), credential='fixture-token')

    async def test_missing_default_uses_one_exact_encoded_lookup_without_pagination(self):
        provider, calls = self.provider(default='release/main', tags=[
            {'name':f'v{i}', 'commit':{'id':'c'*40}} for i in range(100)])
        refs = await self.refs(provider)
        self.assertEqual(len(refs), 201)
        self.assertEqual([(r.name,r.revision) for r in refs if r.name=='release/main'], [('release/main',HEAD)])
        self.assertEqual(len(calls),4)
        self.assertEqual(calls[-1][0],ROOT+'/repository/branches/release%2Fmain')
        self.assertEqual([q for p,q,_ in calls if p.endswith(('/branches','/tags'))],
            [{'per_page':'100'},{'per_page':'100'}])
        self.assertTrue(all(token=='fixture-token' for _,_,token in calls))

    async def test_valid_first_page_default_needs_no_metadata_or_exact_lookup(self):
        provider,calls=self.provider(branches=[{'name':'main','default':True,'commit':{'id':HEAD}}])
        self.assertEqual([(r.name,r.revision) for r in await self.refs(provider)], [('main',HEAD)])
        self.assertEqual(len(calls),2)

    async def test_malformed_exact_default_and_provider_absence_fail_closed(self):
        for exact in ({'name':'foreign','default':True,'commit':{'id':HEAD}},
                {'name':'main','default':True,'commit':{'id':'short'}},
                {'name':'main','default':True,'commit':{'id':123}},
                {'name':'main','default':True,'commit':None},
                {'name':'main','default':False,'commit':{'id':HEAD}}):
            with self.subTest(exact=exact):
                provider,calls=self.provider(exact=exact)
                with self.assertRaises(CodeHostError):await self.refs(provider)
                self.assertEqual(len(calls),4)
        provider,calls=self.provider(exact_status=404)
        with self.assertRaises(CodeHostError):await self.refs(provider)
        self.assertEqual(len(calls),4)

    async def test_invalid_or_ambiguous_first_page_default_is_not_attested(self):
        for branches in ([{'name':'main','default':True,'commit':{'id':'short'}}],
                [{'name':'main','default':True,'commit':{'id':HEAD}},
                 {'name':'other','default':True,'commit':{'id':HEAD}}]):
            with self.subTest(branches=branches):
                provider,calls=self.provider(branches=branches)
                with self.assertRaises(CodeHostError):await self.refs(provider)
                self.assertEqual(len(calls),2)

    async def test_empty_repository_without_default_remains_empty(self):
        provider,calls=self.provider(branches=[],default=None)
        self.assertEqual(await self.refs(provider),())
        self.assertEqual(len(calls),3)

    async def test_real_service_materialization_accepts_current_tip_and_refuses_stale_or_changed_tip(self):
        fixture=fixtures.CodeHostContractTests();fixture.setUp()
        self.addCleanup(fixture.tearDown)
        resource=fixture.resources.create(ResourceCreate(resource_type='repository',name='acme/widgets',
            aliases=[ResourceAlias(namespace='provider',provider='gitlab',value='acme/widgets')],
            provenance=ResourceProvenance(provider='gitlab',provider_instance='gitlab.example',
                external_id='42',external_url='https://gitlab.example/acme/widgets')),actor=fixture.actor)
        binding=adapters.CodeHostAdapterTests._binding('gitlab').model_copy(update={
            'organization_id':fixture.actor.organization_id,'workspace_id':fixture.actor.workspace_id,
            'capabilities':tuple(GitLabCodeHostProvider().capabilities())})
        checkout=Path(fixture.temp.name)/'checkout';checkout.mkdir()
        for outcome in ('current','stale','changed'):
            with self.subTest(outcome=outcome):
                exact_calls=0
                def exact():
                    nonlocal exact_calls
                    exact_calls+=1
                    return {'name':'main','default':True,'commit':{
                        'id':'c'*40 if outcome=='changed' and exact_calls>1 else HEAD}}
                provider,calls=self.provider(exact=exact)
                registry=CodeHostRegistry();registry.register_provider(provider);registry.register_binding(binding)
                service=CodeHostService(registry,fixture.resources,secrets=fixture.secrets)
                with (patch.object(service,'_git_has_complete_history',return_value=True) as local,
                        patch.object(service,'_materialize_git_sync') as fetch):
                    async def materialize():
                        return await service.materialize_default_branch_revision(binding.id,resource.id,
                            'd'*40 if outcome=='stale' else HEAD,checkout=checkout,actor=fixture.actor)
                    if outcome=='current':
                        result=await materialize();self.assertEqual(result.revision,HEAD);self.assertFalse(result.fetched)
                    else:
                        with self.assertRaisesRegex(CodeHostError,'default.branch'):await materialize()
                    fetch.assert_not_called()
                    if outcome=='stale':local.assert_not_called()
                self.assertEqual(exact_calls,1 if outcome=='stale' else 2)
                self.assertTrue(all(c[1].get('page') is None for c in calls))
