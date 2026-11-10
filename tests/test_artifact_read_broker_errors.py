"""Real Unix broker -> CodeHostService -> GitLab adapter; external HTTP is private."""
import base64
import json
import unittest
from types import SimpleNamespace

import httpx

from tests import test_control_plane_broker as broker_fixtures
from tests import test_code_hosts as code_host_fixtures
from codex_web.resources import ResourceCreate, ResourceProvenance, ResourceType, RepositoryExecutionScope, RepositoryTargetSource
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.control_plane_broker import ControlPlaneBrokerService, AssignmentBoundControlPlaneBroker


class ArtifactReadBrokerErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = broker_fixtures.ControlPlaneBrokerTests()
        await self.f.asyncSetUp(start_broker=False)
        self.f._publish_worker_authority(repository_evidence_read=True)
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.f.sqlite))
        self.resource = self.resources.create(ResourceCreate(name='acme/widgets', resource_type=ResourceType.REPOSITORY,
            provenance=ResourceProvenance(provider='gitlab', provider_instance='gitlab.example', external_id='42')),
            actor=self.f.identity.local_trusted_actor())
        self.f.authority.resources = self.resources
        self.calls = []; self.mode = 'missing'; self.failure = None
        self.provider_status = None; self.download_status = None
        registry = CodeHostRegistry()
        self.provider = GitLabCodeHostProvider(GitLabClient(transport=httpx.MockTransport(self.transport)))
        registry.register_provider(self.provider)
        self.secrets = code_host_fixtures._SecretBroker()
        self.code_hosts = CodeHostService(registry, self.resources, secrets=self.secrets)
        actions = broker_fixtures._ActionRegistry('gitlab')
        bindings = actions.list_bindings
        def current_bindings(actor):
            rows = bindings(actor); rows[0].resource_ids = (self.resource.id,); return rows
        actions.list_bindings = current_bindings
        self.service = ControlPlaneBrokerService(identity=self.f.identity, authority=self.f.authority,
            work_items=self.f.work_items, operator=self.f.operator, audit=self.f.service.audit,
            action_intents=SimpleNamespace(execution=SimpleNamespace(registry=actions)), code_hosts=self.code_hosts)
        self.assignment = self.f.assignment.model_copy(update={'execution_profile_id':'repository-write',
            'resource_ids':(self.resource.id,), 'repository_scope':RepositoryExecutionScope(
                organization_id='local',workspace_id='default',project_id='project-a',
                writable_repository_ids=(self.resource.id,), source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref=self.resource.id)})
        self.current = self.assignment
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id='worker-1', worker_actor=self.f.worker_actor, fence=1, validator=lambda:self.current)
        await self.broker.start()
        self.f.broker = self.broker

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    def transport(self, request):
        self.calls.append(request)
        self.assertEqual(request.headers['PRIVATE-TOKEN'], 'provider-secret')
        if self.failure is not None: raise self.failure
        if self.provider_status is not None:
            return httpx.Response(self.provider_status, text='private provider body/token/path must not escape')
        path = request.url.path
        if path == '/api/v4/projects/42':
            return httpx.Response(200, json={'id':42,'name':'widgets'})
        if path == '/api/v4/projects/42/jobs/19025':
            job = {'id':19025,'pipeline':{'id':3394,'project_id':42},
                   'artifacts':[{'file_type':'dotenv','filename':'build.env','size':20}]}
            if self.mode != 'missing': job['artifacts_file'] = {'filename':'package.zip','size':400000}
            if self.mode == 'expired': job['artifacts_expire_at'] = '2000-01-01T00:00:00Z'
            if self.mode == 'erased': job['erased_at'] = '2000-01-01T00:00:00Z'
            if self.mode == 'foreign': job['pipeline']['project_id'] = 99
            if self.mode == 'invalid_size': job['artifacts_file']['size'] = -1
            return httpx.Response(200, json=job)
        if path in {'/api/v4/projects/42/jobs/19025/artifacts',
                    '/api/v4/projects/42/jobs/19025/artifacts/build.env'}:
            if self.download_status is not None:
                return httpx.Response(self.download_status, text='private provider body/provider-secret')
            return httpx.Response(200, content=b'PRIVATE_TEST_FLAG=1\n',headers={'content-type':'text/plain'})
        return httpx.Response(404)

    async def read(self, suffix='?max_bytes=4096&member_path=build.env'):
        return await self.f._request('GET','/api/repository-facts/artifacts/19025/download'+suffix)

    def assert_audited_refusal(self, status, payload, code):
        self.assertEqual(status,409,payload)
        self.assertEqual(payload['error']['code'],code)
        self.assertIn('no retry is authorized',payload['error']['message'])
        text = json.dumps(payload)
        self.assertNotIn('provider-secret',text)
        self.assertNotIn('private provider body',text)
        self.assertNotIn('PRIVATE_TEST_FLAG',text)
        events = self.service.audit.load().events
        matched = [e for e in events if e.correlation_id==payload['error']['correlation_id']]
        self.assertEqual(len(matched),1)
        self.assertEqual(matched[0].response_status,status)
        self.assertEqual(matched[0].operation_id,'repository.read.artifact-download')
        self.assertEqual(matched[0].denial_reason,payload['error']['message'])
        self.assertNotIn('provider-secret',matched[0].denial_reason)

    async def test_actual_gitlab_missing_metadata_is_safe_domain_refusal(self):
        status,_,payload = await self.read()
        self.assertEqual(status,409,'actual GitLab metadata refusal escaped as generic broker500: '+json.dumps(payload))
        self.assert_audited_refusal(status,payload,'repository_artifact_unavailable')
        self.assertEqual([x.url.path for x in self.calls],['/api/v4/projects/42','/api/v4/projects/42/jobs/19025'])

    async def test_actual_expired_and_erased_metadata_are_distinct_from_missing(self):
        for mode in ['expired','erased']:
            with self.subTest(mode=mode):
                self.mode=mode
                status,_,payload=await self.read()
                self.assert_audited_refusal(status,payload,'repository_artifact_expired')
        self.assertFalse(any('/artifacts' in x.url.path for x in self.calls))

    async def test_actual_provider_auth_hidden_and_transient_http_keep_typed_status(self):
        for value in [401,403,404,429,503]:
            with self.subTest(http_status=value):
                self.provider_status=value
                status,_,payload=await self.read()
                self.assert_audited_refusal(status,payload,'repository_read_provider_error')
                self.assertIn('HTTP '+str(value),payload['error']['message'])
                self.assertIn('Remote absence and permission are unproven',payload['error']['message'])
        self.assertEqual(len(self.calls),5)

    async def test_actual_member_http_refusal_after_valid_metadata_is_not_absence(self):
        self.mode='available'
        for value in [403,404]:
            with self.subTest(http_status=value):
                self.download_status=value
                status,_,payload=await self.read()
                self.assert_audited_refusal(status,payload,'repository_read_provider_error')
                self.assertIn('HTTP '+str(value),payload['error']['message'])
        self.assertEqual(len(self.calls),6)

    async def test_actual_transport_failure_is_safe_and_not_retried(self):
        self.failure=httpx.ConnectError('private transport detail/provider-secret')
        status,_,payload=await self.read()
        self.assert_audited_refusal(status,payload,'repository_read_transport_error')
        self.assertEqual(len(self.calls),1)

    async def test_foreign_project_and_invalid_size_stay_unverified_not_missing(self):
        for mode in ['foreign','invalid_size']:
            with self.subTest(mode=mode):
                self.mode=mode
                status,_,payload=await self.read()
                self.assert_audited_refusal(status,payload,'repository_read_unverified')
                self.assertNotIn('does not establish a downloadable archive',payload['error']['message'])
        self.assertFalse(any('/artifacts' in x.url.path for x in self.calls))

    async def test_successful_member_and_archive_still_return_bounded_bytes(self):
        self.mode='available'
        for suffix in ['?max_bytes=4096&member_path=build.env','?max_bytes=8']:
            status,_,payload=await self.read(suffix)
            self.assertEqual(status,200,payload)
            raw=base64.b64decode(payload['item']['content_base64'])
            self.assertEqual(raw,b'PRIVATE_TEST_FLAG=1\n' if '4096' in suffix else b'PRIVATE_')
            self.assertEqual(payload['item']['truncated'],'4096' not in suffix)
        self.assertEqual(len(self.calls),6)

    async def test_invalid_path_is_request_error_before_credentials_or_provider(self):
        status,_,payload=await self.read('?max_bytes=4096&member_path=..%2Fprivate')
        self.assertEqual(status,400,payload)
        self.assertEqual(payload['error']['code'],'repository_read_request_invalid')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.secrets.calls,[])

    async def test_unsupported_member_is_refused_before_credential_resolution(self):
        self.provider.artifact_member=None
        status,_,payload=await self.read()
        self.assert_audited_refusal(status,payload,'repository_read_unsupported')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.secrets.calls,[])

    async def test_unknown_domain_and_untyped_transient_do_not_parse_provider_text(self):
        from codex_web.code_hosts import CodeHostError,CodeHostTransientError
        for error,code in [(CodeHostError('HTTP 404 provider-secret'),'repository_read_unverified'),
                           (CodeHostTransientError('HTTP 503 provider-secret'),'repository_read_transient_error')]:
            async def refused(*args,**kwargs): raise error
            self.provider.artifact_member=refused
            status,_,payload=await self.read()
            self.assert_audited_refusal(status,payload,code)
            self.assertNotIn('HTTP',payload['error']['message'])
        self.assertEqual(self.calls,[])

    async def test_unexpected_non_domain_exception_still_returns_internal500(self):
        async def unexpected(*args,**kwargs): raise ValueError('private provider-secret details')
        self.provider.artifact_member=unexpected
        status,_,payload=await self.read()
        self.assertEqual(status,500,payload)
        self.assertEqual(payload['error']['code'],'control_plane_broker_denied')
        self.assertNotIn('provider-secret',json.dumps(payload))
        self.assertEqual(self.calls,[])

    async def test_foreign_scope_and_stale_fence_fail_before_provider(self):
        for changes in [{'workspace_id':'foreign'},{'fence':2}]:
            self.current=self.assignment.model_copy(update=changes)
            status,_,payload=await self.read()
            self.assertEqual(status,403,payload)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.secrets.calls,[])
