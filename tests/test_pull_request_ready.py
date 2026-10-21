"""Composed fenced broker/intent/SecretBroker/GitHub HTTP qualification, offline."""
import copy
import asyncio
import json
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

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
from codex_web.services.github_action_provider import GitHubActionProvider
from codex_web.integrations.github_client import GitHubClient
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


class PullRequestReadyTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(fixture.AdministrativeBrokerRuntimeTests._assignment)
    def _publish_worker_authority(self, **kwargs):
        fixture.AdministrativeBrokerRuntimeTests._publish_worker_authority(self, repository_evidence_read=True)
    async def _request(self, method, target, *, payload=None, headers=None):
        # Unlike the borrowed metadata fixture's two-second read, this waits
        # for a full governed action with repeated durable admission checks.
        # The provider action retains its independent 120-second engine bound.
        response = await self.broker_client.request(method, target, json=payload, headers=headers)
        return response.status_code, dict(response.headers), response.json()
    _publish_profile = fixture.AdministrativeBrokerRuntimeTests._publish_profile

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
            ResourceAlias(namespace='repository', provider='github', value='acme/app')]), actor=self.admin)
        self.secret_broker = SecretBroker(SecretStateStore(self.sqlite),
            {'local':LocalFileSecretBackend(Path(self.temp.name) / 'ready-secrets')})
        self.secret = self.secret_broker.create(SecretCreate(name='Offline fixture', value='ready-fixture-token',
            provider='github', allowed_identity_ids=[self.worker_actor.identity_id]), actor=self.admin)
        self.calls = []
        self.pr_reads = 0
        self.mode = 'normal'
        self.rules_version = 1
        self.pr = {'id':10, 'node_id':'PR_fixture', 'number':7, 'state':'open', 'draft':True, 'merged':False,
            'head':{'sha':HEAD, 'ref':'feature'},
            'base':{'sha':BASE, 'ref':'main', 'repo':{'full_name':'acme/app'}},
            'html_url':'https://github.test/acme/app/pull/7'}
        self.provider = GitHubActionProvider(self.resources,
            client=GitHubClient(transport=httpx.MockTransport(self.respond)),
            provider_instance='github.test', api_base='https://github.test/api/v3', web_base='https://github.test')
        registry = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        registry.register(self.provider)
        self.binding = registry.bind(ActionProviderBindingCreate(provider_type='github',
            provider_instance='github.test', project_id='project-a', resource_ids=(self.target.id,),
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
            transport=httpx.AsyncHTTPTransport(uds=str(self.broker.socket_path)), timeout=10)
        self.addAsyncCleanup(self.broker_client.aclose)

    async def asyncTearDown(self):
        try:
            await self.broker.stop()
        finally:
            if self.sqlite_lifetime_connection is not None:
                self.sqlite_lifetime_connection.close()
            self.temp.cleanup()

    def change_assignment(self, **values):
        def update(state):
            state.assignments = [x.model_copy(update=values) if x.id==self.assignment.id else x for x in state.assignments]
            return state
        self.workers.store.update(update)

    def respond(self, request):
        self.calls.append((request.method, request.url.path))
        self.assertEqual(request.headers['authorization'], 'Bearer ready-fixture-token')
        if request.method == 'POST':
            self.assertEqual(request.url.path, '/api/graphql')
            payload=json.loads(request.content)
            self.assertIn('markPullRequestReadyForReview',payload['query'])
            self.assertNotIn('mergePullRequest',payload['query'])
            self.assertEqual(payload['variables']['input']['pullRequestId'],'PR_fixture')
            if self.mode.startswith('ambiguous-'):
                error = {'ambiguous-read': httpx.ReadError, 'ambiguous-protocol': httpx.RemoteProtocolError,
                    'ambiguous-timeout': httpx.ReadTimeout}[self.mode]
                raise error('offline ambiguous response; provider remains draft')
            self.pr['draft']=False
            if self.mode == 'lost': raise httpx.ReadTimeout('offline lost response')
            if self.mode == 'changed-head-after': self.pr['head']['sha']='c'*40
            if self.mode == 'fence-after': self.change_assignment(fence=self.assignment.fence+1)
            if self.mode == 'rules-after': self.rules_version+=1
            return httpx.Response(200,json={'data':{'markPullRequestReadyForReview':{
                'clientMutationId':payload['variables']['input']['clientMutationId'],
                'pullRequest':{'id':'PR_fixture','number':7,'isDraft':False,
                    'headRefOid':self.pr['head']['sha'],'baseRefOid':BASE,'baseRefName':'main',
                    'repository':{'nameWithOwner':'acme/app'}}}}})
        if request.url.path.endswith('/pulls/7'):
            self.pr_reads+=1
            if self.mode=='fence-verification' and self.pr_reads==4:
                self.change_assignment(fence=self.assignment.fence+1)
            if self.mode == 'fence-before': self.change_assignment(fence=self.assignment.fence+1)
            if self.mode == 'credential-before': self.secret_broker.revoke(self.secret.id, actor=self.admin)
            if self.mode == 'lease-before':
                self.change_assignment(lease=self.assignment.lease.model_copy(update={'expires_at':0}))
            if self.mode == 'definition-before':
                self._publish_profile(self.profile_payload,'1.1',project='project-a')
            if self.mode == 'provider-before':
                self.provider.api_base='https://other-provider.test/api/v3'
            if self.mode == 'binding-before':
                self.intents.execution.registry.set_enabled(self.binding.id, False, actor=self.admin)
            return httpx.Response(200,json=copy.deepcopy(self.pr))
        if '/rules/branches/' in request.url.path: return httpx.Response(200,json=[])
        if request.url.path.endswith('/branches/main'): return httpx.Response(200,json={'protected':True})
        if request.url.path.endswith('/protection'): return httpx.Response(200,json={'version':self.rules_version})
        raise AssertionError(f'Unexpected HTTP {request.method} {request.url.path}')

    async def submit(self, key='ready-fixture', **parameters):
        return await self._request('POST', '/api/repository-actions/pull-request/ready?repository_resource_id='+self.target.id,
            payload={'parameters':{'pull_request_number':7,'expected_head_sha':HEAD,'expected_base_sha':BASE,'expected_base_ref':'main',**parameters},
                     'idempotency_key':key})

    async def test_official_enterprise_transition_and_durable_duplicate(self):
        status,_,body=await self.submit()
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'succeeded',body)
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)
        history=self.intents.history(body['item']['id'],self.admin)
        self.assertTrue(history['verifications'][-1]['verified'])
        self.assertNotIn('ready-fixture-token',json.dumps(history))
        self.assertNotIn(self.assignment.lease.lease_token,json.dumps(history))
        # Retain a cold SQLite reopen as well as ordinary independent service
        # transactions: the durable duplicate must survive the WAL's last close.
        if self.sqlite_lifetime_connection is not None:
            self.assertFalse(self.sqlite_lifetime_connection.in_transaction)
            self.sqlite_lifetime_connection.close()
        _,_,again=await self.submit()
        self.assertEqual(again['item']['id'],body['item']['id'])
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def test_known_ready_is_verified_without_mutation(self):
        self.pr['draft']=False
        status,_,body=await self.submit()
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'succeeded',body)
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_lost_response_is_not_replayed_and_read_only_reconciles(self):
        self.mode='lost'
        status,_,body=await self.submit()
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'uncertain',body)
        _,_,same=await self.submit()
        self.assertEqual(same['item']['status'],'uncertain')
        count=len(self.calls)
        status,_,settled=await self._request('POST',
            '/api/action-intents/'+body['item']['id']+'/reconcile?repository_resource_id='+self.target.id,
            payload={'retry_if_idempotent':False})
        self.assertEqual(status,200,settled)
        self.assertEqual(settled['item']['status'],'succeeded',settled)
        self.assertTrue(all(method=='GET' for method,_ in self.calls[count:]))
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def test_exhausted_readonly_reconciliation_crosses_broker_and_provider_without_replay(self):
        import time
        from codex_web.services.procedure_discovery import BrokerProcedureAdmissionService
        from codex_web.procedure_discovery import ProcedureDiscovery
        self.mode = 'lost'
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'uncertain', body)
        intent_id = body['item']['id']
        self.assertEqual([item.id for item in self.intents.store.unsettled_readiness_origin(self.assignment.id)], [intent_id])
        current = self.workers.store.assignment(self.assignment.id)
        for index in range(64 - len(current.procedure_discoveries)):
            self.workers.record_procedure_discovery(self.assignment.id, ProcedureDiscovery(
                actor_identity_id=self.admin.identity_id, organization_id=self.admin.organization_id,
                workspace_id=self.admin.workspace_id, project_id=self.assignment.project_id,
                resource_ids=(self.target.id,), operation='repository.read',
                context_checksum='prior-' + str(index), evaluated_at=time.time(),
                outcome='no_match', policy_source='canonical:test', policy_allows=True),
                expected_fence=self.assignment.fence, actor=self.admin)
        original = self.workers.store.assignment(self.assignment.id).procedure_discoveries
        from codex_web.action_intents import ActionIntentReconcileRequest
        from codex_web.services.action_providers import ActionRequirementError
        intent = self.intents.get(intent_id, self.admin)
        request = ActionIntentReconcileRequest(retry_if_idempotent=False)
        self.assertIsNone(self.intents.read_only_reconciliation_scope(intent,
            ActionIntentReconcileRequest(retry_if_idempotent=True), self.worker_actor))
        with self.assertRaises(ActionRequirementError):
            self.intents.read_only_reconciliation_scope(intent, request, self.worker_actor,
                assignment=self.assignment.model_copy(update={'fence':self.assignment.fence+1}))
        for change in ({'fence':self.assignment.fence+1},
                {'lease':self.assignment.lease.model_copy(update={'expires_at':0})}):
            with self.subTest(change=change):
                self.change_assignment(**change)
                with self.assertRaises(ActionRequirementError):
                    self.intents.read_only_reconciliation_scope(intent, request, self.worker_actor)
                self.change_assignment(fence=self.assignment.fence, lease=self.assignment.lease)
        self.service.procedure_admission = BrokerProcedureAdmissionService(
            self.intents.procedure_discovery, self.workers, self.admin)
        count = len(self.calls)
        status, _, settled = await self._request('POST',
            '/api/action-intents/' + intent_id + '/reconcile?repository_resource_id=' + self.target.id,
            payload={'retry_if_idempotent': False})
        self.assertEqual(status, 200, settled)
        self.assertEqual(settled['item']['status'], 'succeeded', settled)
        self.assertTrue(all(method == 'GET' for method, _ in self.calls[count:]))
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        retained = self.workers.store.assignment(self.assignment.id).procedure_discoveries
        self.assertEqual(retained[:64], original)
        self.assertGreater(len(retained), 64)
        self.assertIn('action_intent.reconcile', {entry.operation for entry in retained[64:]})
        self.assertIn('code-host.pull-request.ready', {entry.operation for entry in retained[64:]})
        self.assertEqual(self.intents.store.unsettled_readiness_origin(self.assignment.id), ())

    async def test_recovery_context_cannot_execute_rollback_or_verify_another_intent(self):
        from codex_web.procedure_reconciliation import ReadOnlyReconciliationScope, read_only_reconciliation
        from codex_web.procedure_discovery import procedure_action_intent
        from codex_web.services.action_providers import ActionRequirementError
        import time
        scope = ReadOnlyReconciliationScope(id='a'*32, intent_id='retained-ready',
            assignment_id=self.assignment.id, execution_id=self.assignment.execution_id,
            worker_id=self.worker.id, fence=self.assignment.fence,
            requester_identity_id=self.admin.identity_id, organization_id=self.admin.organization_id,
            workspace_id=self.admin.workspace_id, project_id=self.assignment.project_id,
            resource_ids=(self.target.id,), agent_profile=self.assignment.agent_profile,
            execution_profile_definition=self.assignment.execution_profile_definition, expires_at=time.time()+300)
        with read_only_reconciliation(scope), procedure_action_intent(scope.intent_id):
            for phase in ('execute', 'rollback'):
                with self.subTest(phase=phase), self.assertRaises(ActionRequirementError):
                    await self.intents.procedure_provider_admission(None, None, None, None, self.worker_actor, phase)
        with read_only_reconciliation(scope), procedure_action_intent('another-ready'):
            with self.assertRaisesRegex(ActionRequirementError, 'intent changed'):
                await self.intents.procedure_provider_admission(None, None, None, None, self.worker_actor, 'verify')
        self.assertEqual(self.calls, [])

    async def test_foreign_target_and_stale_head_are_known_precondition_failures(self):
        self.pr['base']['repo']['full_name']='other/repo'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body)
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_changed_head_after_send_stays_uncertain(self):
        self.mode='changed-head-after'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'uncertain',body)
        status,_,settled=await self._request('POST',
            '/api/action-intents/'+body['item']['id']+'/reconcile?repository_resource_id='+self.target.id,
            payload={'retry_if_idempotent':False})
        self.assertEqual(status,200,settled)
        self.assertEqual(settled['item']['status'],'requires_reconciliation',settled)
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def test_fence_revoked_during_preflight_prevents_mutation(self):
        self.mode='fence-before'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body)
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_no_node_or_graphql_injection(self):
        status,_,body=await self.submit(node_id='foreign')
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'failed',body)
        self.assertEqual(self.calls,[])

    async def test_no_automatic_profile_widening_and_explicit_declaration_denial(self):
        self.assertFalse(any(OPERATION in item.get('control_plane_operations',[])
            for item in execution_profile_catalog_seed_payload()['profiles']))
        self.profile_payload['profiles'][-1]['control_plane_operations'].remove(OPERATION)
        self._publish_profile(self.profile_payload,'1.1',project='project-a')
        _,pin=self.profiles.resolve('orchestration-admin',project_id='project-a')
        self.change_assignment(execution_profile_definition=pin)
        status,_,body=await self.submit()
        self.assertEqual(status,403,body)
        self.assertEqual(self.calls,[])

    async def test_stale_head_and_base_prevent_provider_mutation(self):
        for field in ('head','base'):
            self.pr[field]['sha']='d'*40
            _,_,body=await self.submit(key='stale-'+field)
            self.assertEqual(body['item']['status'],'failed')
            self.pr[field]['sha']=HEAD if field=='head' else BASE
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_credential_revocation_between_reads_prevents_mutation(self):
        self.mode='credential-before'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body['item']['last_error'])
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_rules_race_after_send_is_uncertain_not_known_failure(self):
        self.mode='rules-after'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'uncertain',body['item']['last_error'])
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def test_fence_race_after_send_is_uncertain(self):
        self.mode='fence-after'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'uncertain',body['item']['last_error'])
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def test_retry_request_never_replays_unknown_transition(self):
        self.mode='lost'
        _,_,body=await self.submit()
        status,_,denied=await self._request('POST',
            '/api/action-intents/'+body['item']['id']+'/reconcile?repository_resource_id='+self.target.id,
            payload={'retry_if_idempotent':True})
        self.assertEqual(status,403,denied)
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def test_receipt_inspection_is_narrow_and_current_assignment_owned(self):
        _,_,body=await self.submit()
        path='/api/action-intents/'+body['item']['id']+'/delivery-evidence?repository_resource_id='+self.target.id
        status,_,evidence=await self._request('GET',path)
        self.assertEqual(status,200,evidence)
        self.assertEqual(evidence['intent']['action_id'],ACTION)
        def foreign(state):
            state.intents=[item.model_copy(update={'execution_id':'other-execution'}) if item.id==body['item']['id'] else item for item in state.intents]
            return state
        self.intents.store.update(foreign,intent_ids=(body['item']['id'],))
        status,_,denied=await self._request('GET',path)
        self.assertEqual(status,403,denied)

    async def test_real_asgi_create_cannot_omit_execution_declaration(self):
        from fastapi import FastAPI
        from codex_web.api.action_intents import build_action_intents_router
        app=FastAPI()
        @app.middleware('http')
        async def authenticated_fixture(request, call_next):
            request.state.identity_actor=self.admin
            request.state.tenant_scope=self.admin.tenant
            return await call_next(request)
        app.include_router(build_action_intents_router(self.intents))
        payload={'binding_id':self.binding.id,'request':{'action_id':ACTION,
            'organization_id':'local','workspace_id':'default','project_id':'project-a',
            'resource_ids':[self.target.id], 'idempotency_key':'unbound-asgi',
            'parameters':{'pull_request_number':7,'expected_head_sha':HEAD,'expected_base_sha':BASE,'expected_base_ref':'main'}},
            'policy_decision':{'decision_id':'fixture','source':'fixture:readiness',
                'outcome':'allow','reason':'fixture explicit operation'}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            response=await client.post('/api/action-intents',json=payload)
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('explicitly declared live execution',response.text)
        self.assertEqual(self.calls,[])

    async def test_missing_mandatory_procedure_admission_is_fail_closed(self):
        self.intents.execution.procedure_admission=None
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body['item']['last_error'])
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_lease_expired_during_read_prevents_mutation(self):
        self.mode='lease-before'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body['item']['last_error'])
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_binding_changed_during_read_prevents_mutation(self):
        self.mode='binding-before'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body['item']['last_error'])
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_both_providers_explicitly_declare_readiness(self):
        from codex_web.services.gitlab_action_provider import GitLabActionProvider
        provider=GitLabActionProvider(self.resources)
        self.assertIn(ACTION,[definition.action_id for definition in provider.actions()])
        self.assertIn(ACTION,[definition.action_id for definition in self.provider.actions()])

    async def test_definition_superseded_during_read_prevents_mutation(self):
        self.mode='definition-before'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body['item']['last_error'])
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_provider_registration_changed_during_read_prevents_mutation(self):
        self.mode='provider-before'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'failed',body['item']['last_error'])
        self.assertFalse(any(m=='POST' for m,_ in self.calls))

    async def test_required_published_procedure_is_not_bypassed(self):
        from codex_web.resources import ResourceOperationalContext, ResourceType
        from codex_web.services.skill_scanners import SkillScannerRegistry
        from codex_web.services.skill_security import SkillSecurityService
        from codex_web.storage.skill_security import SkillSecurityStore
        from codex_web.skills import SkillCreate, SkillPublish
        from tests.test_skill_security import FakeScanner
        from tests import test_skill_procedures as procedures
        self.resources.update(self.target.id, ResourceUpdate(operational_context=ResourceOperationalContext(
            environment='staging',service_versions={'gitlab':'18.0.0'})),actor=self.admin)
        class Metadata(procedures.MetadataProvider):
            def actions(inner):
                return tuple(d.model_copy(update={'required_resource_types':(ResourceType.REPOSITORY,)}) for d in super().actions())
        registry=self.intents.execution.registry
        registry.register(Metadata(),tenant_scope=self.admin.tenant)
        skills=self.intents.procedure_discovery.skills
        skills.clock=lambda:100
        skills.bind_procedure_contracts(actions=registry,resources=self.resources,scope_actor=lambda _:self.admin)
        scanners=SkillScannerRegistry();scanners.register(FakeScanner())
        skills.bind_security(SkillSecurityService(SkillSecurityStore(self.sqlite),self.registry,scanners,clock=lambda:100))
        content=procedures.procedure()
        content['operation_triggers']=[ACTION]
        content['resource_selectors'][0]['resource_types']=['repository']
        content['required_worker_capabilities']=[]
        for step in content['steps']:
            if 'worker' in step:
                del step['worker'];step['action']=dict(content['steps'][0]['action'])
        draft=skills.create(SkillCreate(skill_id='required-ready-procedure',name='Required fixture procedure',
            instructions='Consult exact published evidence before an external operation.',operational_procedure=content),actor=self.admin)
        published=skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.admin)
        self.intents.procedure_discovery=ProcedureDiscoveryService(skills,self.resources,
            execution_capabilities=ProcedureExecutionCapabilityService(self.workers,None,None))
        status,_,body=await self.submit()
        self.assertIn(status,(200,409),body)
        if status==200: self.assertIn(body['item']['status'],('failed','cancelled'))
        self.assertIn('procedure_',json.dumps(body))
        self.assertEqual(self.calls,[])
        self.assertIn(published['recordId'],json.dumps(body))

    async def test_legacy_migration_never_invents_ready_origin_and_future_version_fails(self):
        _,_,body=await self.submit()
        state=self.intents.store.load(intent_id=body['item']['id']).model_dump(mode='json')
        state['schema_version']='1.5'
        for item in state['intents']: item.pop('readiness_origin',None)
        migrated=self.intents.store._decode(state)
        self.assertEqual(migrated.schema_version,'1.6')
        self.assertIsNone(migrated.intents[0].readiness_origin)
        state['schema_version']='99.0'
        with self.assertRaises(ValueError): self.intents.store._decode(state)

    async def test_verification_await_cannot_report_success_after_lease_fence_change(self):
        self.mode='fence-verification'
        _,_,body=await self.submit()
        self.assertEqual(body['item']['status'],'requires_reconciliation',body['item']['last_error'])
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)
        history=self.intents.history(body['item']['id'],self.admin)
        self.assertFalse(history['verifications'][-1]['verified'])

    async def test_asgi_bound_intent_and_assignment_broker_share_durable_target(self):
        from fastapi import FastAPI
        from codex_web.api.action_intents import build_action_intents_router
        app=FastAPI()
        @app.middleware('http')
        async def authenticated_fixture(request, call_next):
            request.state.identity_actor=self.admin
            request.state.tenant_scope=self.admin.tenant
            return await call_next(request)
        app.include_router(build_action_intents_router(self.intents))
        payload={'binding_id':self.binding.id,'execution_id':self.assignment.execution_id,
            'request':{'action_id':ACTION,'organization_id':'local','workspace_id':'default','project_id':'project-a',
                'resource_ids':[self.target.id], 'idempotency_key':'asgi-owned',
                'parameters':{'pull_request_number':7,'expected_head_sha':HEAD,'expected_base_sha':BASE,'expected_base_ref':'main'}},
            'policy_decision':{'decision_id':'fixture','source':'fixture:readiness',
                'outcome':'allow','reason':'fixture explicit operation'}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            response=await client.post('/api/action-intents',json=payload)
        self.assertEqual(response.status_code,200,response.text)
        created=response.json()['item']
        self.assertEqual(created['readiness_origin']['assignment_id'],self.assignment.id)
        self.assertEqual(created['readiness_origin']['repository'],'acme/app')
        self.assertEqual(self.calls,[])
        _,_,completed=await self.submit(key='asgi-owned')
        self.assertEqual(completed['item']['id'],created['id'])
        self.assertEqual(completed['item']['status'],'succeeded',completed['item']['last_error'])
        self.assertEqual(sum(m=='POST' for m,_ in self.calls),1)

    async def public_ready(self, key, verification=None, **extra):
        from fastapi import FastAPI
        from codex_web.api.action_intents import build_action_intents_router
        app = FastAPI()
        @app.middleware('http')
        async def authenticated_fixture(request, call_next):
            request.state.identity_actor = self.admin
            request.state.tenant_scope = self.admin.tenant
            return await call_next(request)
        app.include_router(build_action_intents_router(self.intents))
        payload = {'binding_id': self.binding.id, 'execution_id': self.assignment.execution_id,
            'request': {'action_id': ACTION, 'organization_id': 'local', 'workspace_id': 'default',
                'project_id': 'project-a', 'resource_ids': [self.target.id], 'idempotency_key': key,
                'parameters': {'pull_request_number': 7, 'expected_head_sha': HEAD,
                    'expected_base_sha': BASE, 'expected_base_ref': 'main'}},
            'policy_decision': {'decision_id': 'fixture', 'source': 'canonical:readiness-fixture',
                'outcome': 'allow', 'reason': 'explicit offline fixture'}}
        if verification is not None:
            payload['verification_required'] = verification
        payload.update(extra)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            return await client.post('/api/action-intents', json=payload)

    async def persisted_optional_ready(self, key):
        response = await self.public_ready(key)
        self.assertEqual(response.status_code, 200, response.text)
        intent_id = response.json()['item']['id']
        # Model a genuinely persisted pre-fix optional-verification record.
        def old_record(state):
            state.intents = [item.model_copy(update={'verification_required': False})
                if item.id == intent_id else item for item in state.intents]
            return state
        self.intents.store.update(old_record, intent_ids=(intent_id,))
        self.intents.store = ActionIntentStore(self.sqlite)
        self.assertFalse(self.intents.get(intent_id, self.admin).verification_required)
        return intent_id

    async def test_public_asgi_refuses_optional_verification_and_broker_remains_mandatory(self):
        response = await self.public_ready('refused-optional', verification=False)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('verification', response.text.lower())
        self.assertEqual(self.calls, [])
        _, _, completed = await self.submit(key='refused-optional')
        self.assertEqual(completed['item']['status'], 'succeeded', completed)
        self.assertTrue(completed['item']['verification_required'])
        verification = self.intents.history(completed['item']['id'], self.admin)['verifications'][-1]
        self.assertTrue(verification['provider_verification']['verified'])

    async def test_persisted_optional_ambiguous_draft_is_never_synthetic_success(self):
        for index, mode in enumerate(('ambiguous-read', 'ambiguous-protocol', 'ambiguous-timeout'), 1):
            with self.subTest(mode=mode):
                key = 'persisted-' + mode
                intent_id = await self.persisted_optional_ready(key)
                self.mode = mode
                _, _, uncertain = await self.submit(key=key)
                self.assertEqual(uncertain['item']['status'], 'uncertain', uncertain)
                self.assertTrue(self.pr['draft'])
                before = len(self.calls)
                status, _, observed = await self._request('POST', '/api/action-intents/' + intent_id
                    + '/reconcile?repository_resource_id=' + self.target.id,
                    payload={'retry_if_idempotent': False})
                self.assertEqual(status, 200, observed)
                self.assertEqual(observed['item']['status'], 'requires_reconciliation', observed)
                self.assertGreater(len(self.calls), before)
                self.assertTrue(all(method == 'GET' for method, _ in self.calls[before:]))
                self.assertEqual(sum(method == 'POST' for method, _ in self.calls), index)
                history = self.intents.history(intent_id, self.admin)
                self.assertFalse(history['verifications'][-1]['verified'])
                self.assertFalse(history['verifications'][-1]['provider_verification']['verified'])
                self.assertFalse(any(receipt['outcome'] == 'completed' for receipt in history['receipts']))

    async def test_persisted_optional_lost_response_nondraft_has_real_verification_no_replay(self):
        key = 'persisted-positive'
        intent_id = await self.persisted_optional_ready(key)
        self.mode = 'lost'
        _, _, uncertain = await self.submit(key=key)
        self.assertEqual(uncertain['item']['status'], 'uncertain')
        status, _, observed = await self._request('POST', '/api/action-intents/' + intent_id
            + '/reconcile?repository_resource_id=' + self.target.id, payload={'retry_if_idempotent': False})
        self.assertEqual(status, 200, observed)
        self.assertEqual(observed['item']['status'], 'succeeded', observed)
        verification = self.intents.history(intent_id, self.admin)['verifications'][-1]
        self.assertTrue(verification['verified'])
        self.assertTrue(verification['provider_verification']['verified'])
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)

    async def optional_settlement_denied(self, change):
        from codex_web.action_intents import ActionIntentReconcileRequest
        key = 'persisted-stale-' + change
        intent_id = await self.persisted_optional_ready(key)
        self.mode = 'ambiguous-timeout'
        _, _, uncertain = await self.submit(key=key)
        self.assertEqual(uncertain['item']['status'], 'uncertain')
        if change == 'fence':
            self.change_assignment(fence=self.assignment.fence + 1)
        elif change == 'lease':
            self.change_assignment(lease=self.assignment.lease.model_copy(update={'expires_at': 0}))
        else:
            self._publish_profile(self.profile_payload, '1.1', project='project-a')
        before = len(self.calls)
        observed = await self.intents.reconcile(intent_id,
            ActionIntentReconcileRequest(retry_if_idempotent=False), actor=self.worker_actor)
        self.assertEqual(observed.status.value, 'requires_reconciliation')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        history = self.intents.history(intent_id, self.admin)
        self.assertFalse(any(item['verified'] for item in history['verifications']))

    async def test_persisted_optional_verification_requires_current_fence(self):
        await self.optional_settlement_denied('fence')

    async def test_persisted_optional_verification_requires_current_lease(self):
        await self.optional_settlement_denied('lease')

    async def test_persisted_optional_verification_requires_current_definition(self):
        await self.optional_settlement_denied('definition')

    async def evidence_settlement(self, change=None, *, lost=False, optional=False, boundary='evidence',
                                  configured_security=False):
        """Park real evidence AFTER real GitHub verification, never fake either result."""
        if configured_security:
            self.configure_security_boundary()
        import time
        from types import SimpleNamespace
        from unittest.mock import patch
        from codex_web.action_intents import ActionIntentClaimRequest, ActionIntentReconcileRequest
        from codex_web.artifact_evidence import EvidenceCreate, EvidenceRequirement, EvidenceType, EvidenceResult
        from codex_web.models import WorkItemState
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.services.work_item_state import install_work_item_state_machine
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from codex_web.storage.runtime_state import ModelMapRepository
        repository = ModelMapRepository(self.sqlite, namespace='work_item_states',
            legacy_path=Path(self.temp.name)/'work-items.json', model=WorkItemState)
        work = WorkItemState(ref='acme/app#1479', organization_id='local', workspace_id='default',
            project_id='project-a', resource_ids=[self.target.id], current_stage='implementation_active',
            last_meaningful_update_at=1.0, updated_at=1.0, created_at=1.0)
        repository.put(work.ref, work)
        host = SimpleNamespace(DATA_DIR=Path(self.temp.name),
            WORK_ITEM_EVENTS_FILE=Path(self.temp.name)/'work-events.jsonl',
            _load_work_item_states=repository.load, _save_work_item_states=repository.save,
            _get_work_item_state_record=repository.get, _put_work_item_state_record=lambda state:repository.put(state.ref,state),
            _update_work_item_state_record=repository.update)
        machine = install_work_item_state_machine(SimpleNamespace(state=SimpleNamespace()), host, store=self.sqlite)
        self.intents.work_item_host = host
        artifacts = ArtifactEvidenceService(ArtifactEvidenceStore(self.sqlite), work_item_host=host)
        self.intents.artifact_evidence = artifacts
        requirement = EvidenceRequirement(id='ci-pass', evidence_type=EvidenceType.CI_CHECK,
            accepted_results=(EvidenceResult.PASS,))
        artifacts.create_evidence(EvidenceCreate(work_item_ref=work.ref, evidence_type=EvidenceType.CI_CHECK,
            provider='offline-ci', result=EvidenceResult.PASS, summary='Actual stored passing CI evidence'), actor=self.admin)
        credential_deadline = time.time() + 300
        if change == 'credential-expiry':
            self.secret_broker.store.update(lambda state: state.model_copy(update={'references': [
                reference.model_copy(update={'expires_at': credential_deadline})
                if reference.id == self.secret.id else reference for reference in state.references]}))
        response = await self.public_ready('post-provider-evidence', work_item_ref=work.ref,
            expected_evidence=[requirement.model_dump(mode='json')],
            work_item_success={'current_stage':'ready_for_validation'})
        self.assertEqual(response.status_code,200,response.text)
        intent_id=response.json()['item']['id']
        if optional:
            def old_record(state):
                state.intents=[item.model_copy(update={'verification_required':False})
                    if item.id==intent_id else item for item in state.intents]
                return state
            self.intents.store.update(old_record,intent_ids=(intent_id,))
            self.intents.store=ActionIntentStore(self.sqlite)
            self.assertFalse(self.intents.get(intent_id,self.admin).verification_required)

        entered=threading.Event()
        release=threading.Event()
        observations=[]
        real_evaluate=artifacts.evaluate
        real_verify=self.intents.execution.verify
        async def observe(*args,**kwargs):
            result=await real_verify(*args,**kwargs)
            observations.append(result)
            return result
        def gated_evaluate(*args,**kwargs):
            if not observations:
                return real_evaluate(*args,**kwargs)
            self.assertTrue(observations and observations[-1].verified)
            self.assertFalse(self.pr['draft'])
            entered.set()
            if not release.wait(10):
                raise AssertionError('Evidence gate was not released')
            evaluation=real_evaluate(*args,**kwargs)
            self.assertTrue(evaluation.satisfied)
            return evaluation
        def mutate():
            if change=='fence': self.change_assignment(fence=self.assignment.fence+1)
            elif change=='lease': self.change_assignment(lease=self.assignment.lease.model_copy(update={'expires_at':0}))
            elif change=='profile': self._publish_profile(self.profile_payload,'1.1',project='project-a')
            elif change=='role': self._publish_worker_authority()
            elif change=='binding': self.intents.execution.registry.set_enabled(self.binding.id,False,actor=self.admin)
            elif change=='credential': self.secret_broker.revoke(self.secret.id,actor=self.admin)
            elif change=='credential-expiry':
                # Advance only the credential's observation clock. Its stored
                # revision is unchanged at the write, so a revision-only guard
                # would incorrectly accept a naturally expired credential.
                clock = patch('codex_web.secrets.time', SimpleNamespace(time=lambda: credential_deadline + 1))
                clock.start()
                self.addCleanup(clock.stop)
            elif change=='configured-security-refresh':
                current = self.intents.get(intent_id, self.admin)
                refreshed, error = self.intents._refresh_security(current, actor=self.admin,
                    authority_decision=current.authority_recheck or current.authority_decision)
                self.assertIsNone(error)
                self.assertIsNotNone(refreshed)
                self.assertNotEqual(refreshed.security_decision.id, current.security_decision.id)
            elif change=='configured-security':
                current = self.intents.get(intent_id, self.admin)
                from codex_web.action_intents import ActionDecisionOutcome
                denied = current.authority_decision.model_copy(update={'outcome': ActionDecisionOutcome.DENY})
                refreshed, error = self.intents._refresh_security(current, actor=self.admin,
                    authority_decision=denied)
                self.assertIsNone(refreshed)
                self.assertIn('denied', error)
                self.assertEqual(self.intents.get(intent_id, self.admin).security_decision.outcome.value, 'deny')
            elif change=='security':
                from codex_web.security import SecurityDecisionOutcome
                self.intents.store.update(lambda state: state.model_copy(update={'intents': [
                    item.model_copy(update={'security_decision': item.security_decision.model_copy(
                        update={'outcome': SecurityDecisionOutcome.DENY})}) if item.id == intent_id else item
                    for item in state.intents]}), intent_ids=(intent_id,))
            elif change=='intent':
                def altered(state):
                    state.intents=[item.model_copy(update={'attempt':item.attempt+1}) if item.id==intent_id else item
                        for item in state.intents]
                    return state
                self.intents.store.update(altered,intent_ids=(intent_id,))
        async def execute():
            claimed=await asyncio.to_thread(self.intents.claim,
                ActionIntentClaimRequest(worker_id=self.worker.id,lease_seconds=180),
                actor=self.worker_actor,intent_id=intent_id)
            self.assertIsNotNone(claimed)
            return await self.intents.execute_claimed(intent_id,self.worker.id,actor=self.worker_actor)
        if lost:
            self.mode='lost'
            initial=await execute()
            self.assertEqual(initial.status.value,'uncertain')
            self.assertTrue(any(r['outcome']=='unknown' for r in self.intents.history(intent_id,self.admin)['receipts']))
        count=len(self.calls)
        async def settle():
            if lost:
                return await self.intents.reconcile(intent_id,
                    ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
            return await execute()
        if boundary == 'security-refresh':
            evaluate_security = self.intents.security_boundary.evaluate_action
            def stale_allow(**kwargs):
                decision = evaluate_security(**kwargs)
                if observations and decision.outcome.value == 'allow' and not entered.is_set():
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('Security refresh gate was not released')
                return decision
            with patch.object(self.intents.execution, 'verify', observe), patch.object(
                    self.intents.security_boundary, 'evaluate_action', stale_allow):
                task = asyncio.create_task(settle())
                try:
                    gate_entered = await asyncio.to_thread(entered.wait, 10)
                    if gate_entered:
                        await asyncio.to_thread(mutate)
                finally:
                    release.set()
                    settled = await task
                self.assertTrue(gate_entered, settled.model_dump(mode='json'))
        elif boundary=='evidence':
            with patch.object(self.intents.execution,'verify',observe), patch.object(artifacts,'evaluate',gated_evaluate):
                task=asyncio.create_task(self.intents.reconcile(intent_id,
                    ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor) if lost else execute())
                try:
                    gate_entered=await asyncio.to_thread(entered.wait,10)
                    if gate_entered: mutate()
                finally:
                    release.set()
                settled=await task
                self.assertTrue(gate_entered,('real evidence was never entered',settled.model_dump(mode='json')))
        else:
            # Change origin at the actual guarded Work Item/terminal write, after
            # all async admission checks, to exercise the transactional guard.
            if boundary=='work-item':
                real_update=machine.dependencies.update_state
                def update(*args,**kwargs):
                    mutate()
                    return real_update(*args,**kwargs)
                object.__setattr__(machine.dependencies,'update_state',update)
                settled=await settle()
            elif boundary=='verification':
                real_append=self.intents._append_verification
                def append(*args,**kwargs):
                    if kwargs['verified']: mutate()
                    return real_append(*args,**kwargs)
                with patch.object(self.intents,'_append_verification',append): settled=await settle()
            elif boundary=='receipt':
                real_receipt=self.intents._append_receipt
                def receipt(*args,**kwargs):
                    mutate()
                    return real_receipt(*args,**kwargs)
                with patch.object(self.intents,'_append_receipt',receipt):
                    settled=await self.intents.reconcile(intent_id,
                        ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
            else:
                real_set=self.intents._set_status
                def status(intent_id,status,**kwargs):
                    if status.value=='succeeded': mutate()
                    return real_set(intent_id,status,**kwargs)
                with patch.object(self.intents,'_set_status',status): settled=await settle()
        history=self.intents.history(intent_id,self.admin)
        if change == 'configured-security':
            self.assertEqual(self.intents.get(intent_id, self.admin).security_decision.outcome.value, 'deny')
            self.assertTrue(any('denied' in reason for reason in
                self.intents.get(intent_id, self.admin).security_decision.reasons))
        self.assertEqual(sum(method=='POST' for method,_ in self.calls),1)
        if lost: self.assertTrue(all(method=='GET' for method,_ in self.calls[count:]))
        verification=history['verifications'][-1]
        self.assertTrue(verification['provider_verification']['verified'],verification)
        self.assertTrue(verification['evidence_satisfied'],verification)
        if change:
            self.assertEqual(settled.status.value,'requires_reconciliation',history)
            if boundary in {'evidence','verification','security-refresh'}:
                self.assertFalse(any(v['verified'] for v in history['verifications']))
            if boundary!='terminal': self.assertEqual(repository.get(work.ref).current_stage,'implementation_active')
        else:
            self.assertEqual(settled.status.value,'succeeded',history)
            self.assertTrue(verification['verified'])
            self.assertEqual(repository.get(work.ref).current_stage,'ready_for_validation')
            self.assertTrue(host.WORK_ITEM_EVENTS_FILE.exists())

    async def test_evidence_await_fence_denies_normal(self):
        await self.evidence_settlement('fence')

    async def test_evidence_await_lease_denies_normal(self):
        await self.evidence_settlement('lease',optional=True)

    async def test_evidence_await_profile_denies_normal(self):
        await self.evidence_settlement('profile')

    async def test_evidence_await_role_denies_normal(self):
        await self.evidence_settlement('role')

    async def test_evidence_await_intent_change_denies_normal(self):
        await self.evidence_settlement('intent')

    async def test_evidence_await_fence_denies_lost_response(self):
        await self.evidence_settlement('fence',lost=True,optional=True)

    async def test_evidence_await_lease_denies_lost_response(self):
        await self.evidence_settlement('lease',lost=True,optional=True)

    async def test_evidence_await_profile_denies_lost_response(self):
        await self.evidence_settlement('profile',lost=True,optional=True)

    async def test_evidence_await_role_denies_lost_response(self):
        await self.evidence_settlement('role',lost=True)

    async def test_evidence_await_current_origin_advances_normal(self):
        await self.evidence_settlement(optional=True)

    async def test_evidence_await_current_origin_advances_lost_response(self):
        await self.evidence_settlement(lost=True,optional=True)

    async def test_work_item_transaction_fence_guard(self):
        await self.evidence_settlement('fence',boundary='work-item')

    async def test_terminal_transaction_fence_guard(self):
        await self.evidence_settlement('fence',boundary='terminal')

    async def test_verification_transaction_definition_guard(self):
        await self.evidence_settlement('profile',boundary='verification')

    async def test_work_item_transaction_role_guard(self):
        await self.evidence_settlement('role',boundary='work-item')

    async def test_lost_response_receipt_await_cannot_advance_stale_origin(self):
        await self.evidence_settlement('fence',lost=True,boundary='receipt')

    async def test_verification_write_binding_disable_is_fenced(self):
        await self.evidence_settlement('binding',boundary='verification')

    async def test_verification_write_credential_revoke_is_fenced(self):
        await self.evidence_settlement('credential',boundary='verification')

    async def test_work_item_write_binding_disable_is_fenced(self):
        await self.evidence_settlement('binding',boundary='work-item')

    async def test_work_item_write_credential_revoke_is_fenced(self):
        await self.evidence_settlement('credential',boundary='work-item')

    async def test_terminal_write_binding_disable_is_fenced(self):
        await self.evidence_settlement('binding',boundary='terminal')

    async def test_terminal_write_credential_revoke_is_fenced(self):
        await self.evidence_settlement('credential',boundary='terminal')

    async def test_lost_verification_write_binding_disable_is_fenced(self):
        await self.evidence_settlement('binding',lost=True,boundary='verification')

    async def test_lost_verification_write_credential_revoke_is_fenced(self):
        await self.evidence_settlement('credential',lost=True,boundary='verification')

    async def test_lost_work_item_write_binding_disable_is_fenced(self):
        await self.evidence_settlement('binding',lost=True,boundary='work-item')

    async def test_lost_work_item_write_credential_revoke_is_fenced(self):
        await self.evidence_settlement('credential',lost=True,boundary='work-item')

    async def test_lost_terminal_write_binding_disable_is_fenced(self):
        await self.evidence_settlement('binding',lost=True,boundary='terminal')

    async def test_lost_terminal_write_credential_revoke_is_fenced(self):
        await self.evidence_settlement('credential',lost=True,boundary='terminal')

    async def test_verification_write_checks_credential_expiry_without_revision_change(self):
        await self.evidence_settlement('credential-expiry',boundary='verification')

    async def test_work_item_write_checks_credential_expiry_without_revision_change(self):
        await self.evidence_settlement('credential-expiry',boundary='work-item')

    async def test_lost_terminal_write_checks_credential_expiry_without_revision_change(self):
        await self.evidence_settlement('credential-expiry',lost=True,boundary='terminal')

    async def test_verification_write_preserves_current_security_denial(self):
        await self.evidence_settlement('security',boundary='verification')

    async def test_lost_terminal_write_preserves_current_security_denial(self):
        await self.evidence_settlement('security',lost=True,boundary='terminal')

    async def test_security_admission_fences_absent_collections_without_creating_them(self):
        from codex_web.services.skill_security import SkillSecurityService
        from codex_web.storage.skill_security import SkillSecurityStore
        security_store = SkillSecurityStore(self.sqlite)
        self.intents.procedure_discovery.skills.security = SkillSecurityService(
            security_store, definitions=self.registry, providers={})
        names = (security_store.SCANS, security_store.OVERRIDES, security_store.BASELINES)
        self.assertFalse(any(self.sqlite.record_collections_exist(names).values()))
        status, _, body = await self.submit('absent-procedure-security-records')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'succeeded', body)
        self.assertFalse(any(self.sqlite.record_collections_exist(names).values()))

    async def test_keyed_writer_barrier_preserves_history_indexes_and_rejects_future_schema(self):
        _, _, completed = await self.submit()
        intent_id = completed['item']['id']
        store = self.intents.store
        before = store.load().model_dump(mode='json')
        self.sqlite.record_apply(store.records_namespace,
            upserts={store._meta_key: {'schema_version': '1.2'}})
        restarted = ActionIntentStore(self.sqlite)
        self.assertEqual(restarted.load().model_dump(mode='json'), before)
        self.assertEqual(self.sqlite.record_get(store.records_namespace, store._meta_key),
            {'schema_version': '1.3'})
        self.assertEqual(restarted.find_idempotent('local', 'default', self.binding.id, ACTION,
            'ready-fixture').id, intent_id)
        self.sqlite.record_apply(store.records_namespace,
            upserts={store._meta_key: {'schema_version': '99.0'}})
        revision = self.sqlite.namespace_revision(store.records_namespace)
        with self.assertRaisesRegex(ValueError, 'unsupported action intent records schema'):
            restarted.get(intent_id)
        self.assertEqual(self.sqlite.namespace_revision(store.records_namespace), revision)

    def configure_security_boundary(self):
        from codex_web.security import ExecutionSecurityPolicy, NetworkEgressPolicy
        from codex_web.services.security_boundary import SecurityBoundaryService
        from codex_web.storage.security_events import SecurityEventStore
        policy = ExecutionSecurityPolicy(network=NetworkEgressPolicy(enabled=True))
        self.intents.execution.registry.store.update(lambda state: state.model_copy(update={
            'bindings': [binding.model_copy(update={'security_policy': policy})
                         if binding.id == self.binding.id else binding for binding in state.bindings]}))
        self.intents.security_boundary = SecurityBoundaryService(SecurityEventStore(self.sqlite))

    async def test_configured_security_stale_allow_cannot_overwrite_denial_normal(self):
        await self.evidence_settlement('configured-security', boundary='security-refresh', configured_security=True)

    async def test_configured_security_stale_allow_cannot_overwrite_denial_lost(self):
        await self.evidence_settlement('configured-security', lost=True,
            boundary='security-refresh', configured_security=True)

    async def test_configured_security_current_admission_advances_normal(self):
        await self.evidence_settlement(configured_security=True)

    async def test_configured_security_current_admission_advances_lost(self):
        await self.evidence_settlement(lost=True, configured_security=True)

    async def test_configured_security_verification_denial_normal(self):
        await self.evidence_settlement('configured-security', boundary='verification', configured_security=True)

    async def test_configured_security_verification_denial_lost(self):
        await self.evidence_settlement('configured-security', lost=True,
            boundary='verification', configured_security=True)

    async def test_configured_security_work_item_denial_normal(self):
        await self.evidence_settlement('configured-security', boundary='work-item', configured_security=True)

    async def test_configured_security_work_item_denial_lost(self):
        await self.evidence_settlement('configured-security', lost=True,
            boundary='work-item', configured_security=True)

    async def test_configured_security_terminal_denial_normal(self):
        await self.evidence_settlement('configured-security', boundary='terminal', configured_security=True)

    async def test_configured_security_terminal_denial_lost(self):
        await self.evidence_settlement('configured-security', lost=True,
            boundary='terminal', configured_security=True)

    async def test_configured_security_verification_requires_exact_allow_snapshot(self):
        await self.evidence_settlement('configured-security-refresh',
            boundary='verification', configured_security=True)

    async def test_configured_security_work_item_requires_exact_allow_snapshot(self):
        await self.evidence_settlement('configured-security-refresh',
            boundary='work-item', configured_security=True)

    async def test_configured_security_lost_terminal_requires_exact_allow_snapshot(self):
        await self.evidence_settlement('configured-security-refresh', lost=True,
            boundary='terminal', configured_security=True)
