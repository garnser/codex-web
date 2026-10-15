"""Independent review counterexamples composed through canonical services."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from codex_web.api.control_plane_broker import build_control_plane_broker_router
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.identity import HumanUserCreate, AuthenticationAssurance
from codex_web.organizational_memory import KnowledgeCreate
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionProviderRegistry, ActionExecutionService
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.project_coordination import ProjectCoordinationService, ProjectCoordinationActionProvider, OPERATION_MAP
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest
from tests.test_project_coordination import ProjectCoordinationBrokerTests


async def wait_for_pending_phase(event, producer, *, timeout=2):
    """Observe startup and its request together, within the broker fixture budget.

    The producer prepares a workspace and checks authority before entering the
    parked manager.start seam. A task being scheduled proves neither admission
    nor success; surface an early response/exception instead of masking it with
    an event-only timeout. Do not cancel the producer: its owner releases the
    seam and observes completion in finally.
    """
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            (waiter, producer), timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if producer in done:
            result = await producer
            raise AssertionError(f"Producer completed before pending phase: {result!r}")
        if waiter not in done:
            raise AssertionError("Pending phase not reached within broker fixture budget")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


class PendingPhaseSynchronizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_phase_leaves_producer_parked(self):
        event = asyncio.Event()
        release = asyncio.Event()
        async def produce():
            event.set()
            await release.wait()
        producer = asyncio.create_task(produce())
        try:
            await wait_for_pending_phase(event, producer)
            self.assertFalse(producer.done())
        finally:
            release.set()
            await producer

    async def test_early_exception_is_reported(self):
        producer = asyncio.get_running_loop().create_future()
        producer.set_exception(RuntimeError("preparation failed"))
        with self.assertRaisesRegex(RuntimeError, "preparation failed"):
            await wait_for_pending_phase(asyncio.Event(), producer)

    async def test_early_response_is_reported(self):
        producer = asyncio.get_running_loop().create_future()
        producer.set_result((403, {}, {"reason": "admission denied"}))
        with self.assertRaisesRegex(AssertionError, "403.*admission denied"):
            await wait_for_pending_phase(asyncio.Event(), producer)

    async def test_timeout_preserves_producer_for_owned_cleanup(self):
        producer = asyncio.get_running_loop().create_future()
        try:
            with self.assertRaisesRegex(AssertionError, "Pending phase not reached"):
                await wait_for_pending_phase(asyncio.Event(), producer, timeout=0)
            self.assertFalse(producer.done())
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


class ReviewCorrectionsTests(ProjectCoordinationBrokerTests):
    async def test_F4_coordination_and_codehost_bindings_compose(self):
        from codex_web.action_providers import ActionProviderBindingCreate
        from codex_web.services.github_action_provider import GitHubActionProvider
        from codex_web.services.github_code_host import GitHubCodeHostProvider
        from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
        actions=self.intents.execution.registry
        actions.register(GitHubActionProvider(self.f.resources))
        binding=actions.bind(ActionProviderBindingCreate(provider_type='github',provider_instance='github.com',
            project_id='home',resource_ids=(self.f.repository.id,)),actor=self.actor,resources=self.f.resources)
        hosts=CodeHostRegistry();hosts.register_provider(GitHubCodeHostProvider())
        self.service.code_hosts=CodeHostService(hosts,self.f.resources)
        selected,_=self.service._code_host_binding(assignment=self.assignment,actor=self.actor)
        self.assertEqual(selected,'control-plane-read:'+binding.id)
        status,_,value=await self.call('GET','/threads')
        self.assertEqual(status,200,value)

    async def composed_repository(self):
        from pathlib import Path
        from codex_web.action_providers import ActionProviderBindingCreate
        from codex_web.resources import ResourceUpdate, ResourceAlias
        from codex_web.secret_backends import LocalFileSecretBackend
        from codex_web.secrets import SecretCreate
        from codex_web.services.secrets import SecretBroker
        from codex_web.storage.secret_state import SecretStateStore
        from codex_web.services.github_action_provider import GitHubActionProvider
        from codex_web.services.github_code_host import GitHubCodeHostProvider
        from codex_web.integrations.github_client import GitHubClient
        from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
        from tests.test_github_action_provider import _GitHubClient
        f=self.f
        f.repository=f.resources.update(f.repository.id,ResourceUpdate(aliases=[
            ResourceAlias(namespace='repository',provider='github',value='acme/widgets')]),actor=self.actor)
        self.payload['profiles'][-1]['control_plane_operations'] += [
            'repository.read.metadata','repository.read.refs','repository.read.delivery-evidence',
            'repository.branch.publish','repository.pull-request.upsert','repository.pull-request.merge',
            'repository.issue.create','action_intent.reconcile']
        self.publish(self.payload)
        _,reference=f.execution_profiles.resolve('project-governor',organization_id='local',workspace_id='default',project_id='home')
        self.assignment=self.assignment.model_copy(update={'execution_profile_definition':reference})
        f.workers.store.update(lambda s:s.model_copy(update={'assignments':[self.assignment]}))
        actions=self.intents.execution.registry
        secrets=SecretBroker(SecretStateStore(f.sqlite),{'local':LocalFileSecretBackend(Path(f.temp.name)/'f4-secrets')})
        secret=secrets.create(SecretCreate(name='Fixture reference',value='F4_TEST_CREDENTIAL',allowed_identity_ids=[self.actor.identity_id,self.worker_actor.identity_id]),actor=self.actor)
        self.worker_actor=self.worker_actor.model_copy(update={'service_scopes':(*self.worker_actor.service_scopes,'secret:use')})
        self.broker.worker_actor=self.worker_actor
        self.intents.execution.secret_broker=secrets
        client=_GitHubClient()
        async def publish(path,repository,branch,revision,credential,expected):
            self.assertEqual(credential,'F4_TEST_CREDENTIAL')
            self.assertEqual(repository,'acme/widgets')
            self.assertEqual(expected,'')
            client.branches[branch]=revision
            if getattr(self,"lose_publish_receipt",False):
                self.lose_publish_receipt=False
                raise TimeoutError("Fixture response lost after remote mutation")
        actions.register(GitHubActionProvider(f.resources,client,f.workspaces,branch_publisher=publish))
        binding=actions.bind(ActionProviderBindingCreate(provider_type='github',provider_instance='github.com',
            project_id='home',resource_ids=(f.repository.id,),credential_ref=secret.id),actor=self.actor,resources=f.resources)
        calls=[]
        def transport(request):
            calls.append(request.url.path)
            self.assertEqual(request.headers['authorization'],'Bearer F4_TEST_CREDENTIAL')
            return httpx.Response(200,json={'id':42,'full_name':'acme/widgets','default_branch':'main'})
        hosts=CodeHostRegistry();hosts.register_provider(GitHubCodeHostProvider(GitHubClient(transport=httpx.MockTransport(transport))))
        self.service.code_hosts=CodeHostService(hosts,f.resources,secrets=secrets)
        return actions,binding,client,calls

    def worker_repository_authority(self):
        from codex_web.authority_seed import authority_role_catalog_seed_payload
        payload=authority_role_catalog_seed_payload()
        payload['roles'].append({'id':'fact-reader','name':'Scoped facts','description':'Scoped fixture worker read authority','grants':[
            {'id':'facts','capability':'repository.read','level':'read','project_ids':['home'],'resource_ids':[self.f.repository.id]}]})
        payload['bindings'].append(dict(payload['bindings'][0],id='worker-facts',role_id='fact-reader',subject_id=self.worker_actor.identity_id))
        active=self.authority.catalog_record(actor=self.actor,project_id='home')
        record=self.registry.create_draft(DefinitionDraftCreate(definition_id=active.definition_id,kind=active.kind,
            definition_schema_version=active.definition_schema_version,scope_type=active.scope_type,scope_id=active.scope_id,
            payload=payload,actor='fixture'))
        self.registry.approve_publication(record.record_id,actor='independent-reviewer',reference='fixture',reason='Scoped worker facts')
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))

    async def repository_workflow(self, profile, vp=False, lose_receipt=False):
        import subprocess
        from pathlib import Path
        actions,binding,client,calls=await self.composed_repository()
        # Requester privileges do not supply the separate worker fact-read grant.
        status,_,value=await self._request('GET','/api/repository-facts')
        self.assertEqual(status,403,value);self.assertEqual(calls,[])
        self.worker_repository_authority()
        if vp:
            from codex_web.agent_profiles import AgentProfileCreate
            record=self.profiles.create(AgentProfileCreate(profile_id='fixture-vp-engineering',name='VP fixture',
                execution_profile_id='repository-write',sandbox_requirement='workspace-write'),actor=self.actor)
            self.assignment=self.assignment.model_copy(update={'agent_profile':self.profiles.binding_for(record)})
        for profile in (profile,):
            _,reference=self.f.execution_profiles.resolve(profile,organization_id='local',workspace_id='default',project_id='home')
            self.assignment=self.assignment.model_copy(update={'execution_profile_id':profile,'execution_profile_definition':reference})
            self.f.workers.store.update(lambda s:s.model_copy(update={'assignments':[self.assignment]}))
            status,_,value=await self._request('GET','/api/repository-facts')
            self.assertEqual(status,200,value);self.assertEqual(value['item']['full_name'],'acme/widgets')
            status,_,value=await self._request('POST','/api/repository-actions/issue/create',payload={
                'parameters':{'title':'Scoped delivery'},'idempotency_key':'issue-'+profile})
            self.assertEqual(status,200,value);self.assertEqual(value['item']['status'],'succeeded',value)
        if profile == 'project-governor':
            status,_,value=await self.call('GET','/threads');self.assertEqual(status,200,value)
        else:
            status,_,value=await self.call('GET','/threads');self.assertEqual(status,403,value)
        workspace=self.f.workspaces.store.workspace(self.assignment.execution_workspace_id)
        path=Path(workspace.path)
        def git(*args):
            return subprocess.run(['git',*args],cwd=path,check=True,capture_output=True,text=True).stdout.strip()
        git('init','-b',workspace.branch_name);git('config','user.email','fixture@example.test');git('config','user.name','Fixture')
        (path/'delivery.txt').write_text('Scoped fixture\n');git('add','delivery.txt');git('commit','-m','Fixture delivery')
        head=git('rev-parse','HEAD')
        self.f.workspaces.store.update(lambda s:s.model_copy(update={'workspaces':[
            w.model_copy(update={'base_revision':head,'head_revision':head,'repository_members':tuple(
                m.model_copy(update={'base_revision':head,'head_revision':head}) for m in w.repository_members)}) if w.id==workspace.id else w for w in s.workspaces]}))
        self.lose_publish_receipt=lose_receipt
        operations=[('branch/publish',{'branch':workspace.branch_name,'head_revision':head,'expected_remote_absent':True}),
            ('pull-request/upsert',{'title':'Scoped delivery','body':'Fixture','head':workspace.branch_name,'base':'main','draft':False})]
        for suffix,parameters in operations:
            status,_,value=await self._request('POST','/api/repository-actions/'+suffix,payload={
                'parameters':parameters,'idempotency_key':suffix})
            self.assertEqual(status,200,value)
            intent=value['item']['id']
            if suffix == 'branch/publish' and lose_receipt:
                self.assertEqual(value['item']['status'],'uncertain',value)
                status,_,value=await self._request('POST',f'/api/action-intents/{intent}/reconcile',payload={'retry_if_idempotent':False})
                self.assertEqual(status,200,value)
                self.assertEqual(value['item']['status'],'requires_reconciliation',value)
                self.assertEqual(value['item']['attempt'],1)
                self.assertEqual(client.branches[workspace.branch_name],head)
                return  # No forward replay or downstream delivery from an unknown outcome.
            self.assertEqual(value['item']['status'],'succeeded',value)
            status,_,evidence=await self._request('GET',f'/api/action-intents/{intent}/delivery-evidence')
            self.assertEqual(status,200,evidence);self.assertTrue(evidence['receipts']);self.assertTrue(evidence['verifications'])
            status,_,reconciled=await self._request('POST',f'/api/action-intents/{intent}/reconcile',payload={'retry_if_idempotent':False})
            if suffix == 'branch/publish':
                self.assertEqual(status,200,reconciled);self.assertEqual(reconciled['item']['status'],'succeeded',reconciled)
            else:
                self.assertEqual(status,403,reconciled)  # Existing reconciliation action allowlist remains narrow.
        client.pull_requests[0].update(mergeable=True,mergeable_state='clean',merged=False,head={'sha':head})
        status,_,value=await self._request('POST','/api/repository-actions/pull-request/merge',payload={
            'parameters':{'pull_request_number':889,'expected_head_sha':head,'merge_method':'squash'},'idempotency_key':'merge'})
        self.assertEqual(status,200,value);self.assertEqual(value['item']['status'],'succeeded',value)
        self.assertEqual(client.pull_request_merges,1)
        self.assertNotIn('F4_TEST_CREDENTIAL',json.dumps(value))
        self.assertEqual(len(actions.list_bindings(self.actor)),2)

    async def test_F4_governor_full_repository_workflow(self):
        await self.repository_workflow('project-governor')

    async def test_F4_governor_retains_uncertain_publication_without_replay(self):
        await self.repository_workflow('project-governor', lose_receipt=True)

    async def test_F4_repository_write_full_workflow(self):
        await self.repository_workflow('repository-write')

    async def test_F4_vp_repository_write_full_workflow(self):
        await self.repository_workflow('repository-write', vp=True)

    async def test_F4_eligible_ambiguity_foreign_disabled_and_unsupported(self):
        from codex_web.action_providers import ActionProviderBindingCreate
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        from codex_web.services.github_action_provider import GitHubActionProvider
        from codex_web.code_hosts import CodeHostCapability
        actions,binding,client,calls=await self.composed_repository()
        def select(action=None):
            if action:
                return self.service._repository_action_binding(action_registry=actions,actor=self.actor,
                    project_id='home',repository_id=self.f.repository.id,action_id=action)
            return self.service._code_host_binding(assignment=self.assignment,actor=self.actor)[0]
        self.assertEqual(select(),'control-plane-read:'+binding.id)
        self.assertEqual(select('code-host.branch.publish').id,binding.id)
        # A foreign project binding remains ineligible, with a real supported adapter.
        foreign=actions.bind(ActionProviderBindingCreate(provider_type='github',provider_instance='github.com',
            project_id='foreign',resource_ids=(self.f.repository.id,)),actor=self.actor,resources=self.f.resources)
        self.assertEqual(select(),'control-plane-read:'+binding.id)
        # Unsupported actions and absent code-host adapters never acquire eligibility.
        with self.assertRaises(ControlPlaneBrokerDeniedError):select('code-host.not-supported')
        hosts=self.service.code_hosts.registry
        unavailable=type(hosts)();self.service.code_hosts.registry=unavailable
        with self.assertRaises(ControlPlaneBrokerDeniedError):select()
        self.service.code_hosts.registry=hosts
        from codex_web.services.gitlab_action_provider import GitLabActionProvider
        from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
        actions.register(GitLabActionProvider(self.f.resources))
        hosts.register_provider(GitLabCodeHostProvider())
        adapter=hosts.provider('github')
        with patch.object(adapter,'capabilities',return_value=(CodeHostCapability.REFS_READ,)):
            with self.assertRaises(ControlPlaneBrokerDeniedError):select()
            selected,_=self.service._code_host_binding(assignment=self.assignment,actor=self.actor,operation_id='repository.read.refs')
            resolved,_=hosts.resolve(selected,actor=self.actor)
            self.assertEqual(resolved.capabilities,(CodeHostCapability.REFS_READ,))
        duplicate=actions.bind(ActionProviderBindingCreate(provider_type='gitlab',provider_instance='gitlab.com',
            project_id='home',resource_ids=(self.f.repository.id,)),actor=self.actor,resources=self.f.resources)
        for action in (None,'code-host.branch.publish'):
            with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'exactly one eligible'):select(action)
        actions.set_enabled(duplicate.id,False,actor=self.actor)
        self.assertEqual(select(),'control-plane-read:'+binding.id)
        actions.set_enabled(binding.id,False,actor=self.actor)
        with self.assertRaises(ControlPlaneBrokerDeniedError):select()
        with self.assertRaises(ControlPlaneBrokerDeniedError):select('code-host.branch.publish')
        self.assertEqual(calls,[]);self.assertEqual(client.credentials,[])

    async def second_instance(self):
        f=self.f
        f.workers.store.update(lambda s:s.model_copy(update={'workers':[
            w.model_copy(update={'max_concurrency':10}) for w in s.workers]}))
        binding=f.service.prepare_bootstrap(bootstrap_id='second-governor',execution_id='second-governor',
            project_id='home',sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=f.repository.id,execution_profile_id='project-governor')
        claimed=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=binding.assignment_id)
        origin=f.workers.start(f.worker.id,binding.assignment_id,
            AssignmentStartRequest(fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=self.worker_actor)
        service=copy.copy(self.service)
        workers=copy.copy(f.workers)
        workers.store=ExecutionWorkerStore(SQLiteStateStore(f.sqlite.path))  # Different locks, same canonical StateStore.
        threads=copy.copy(self.thread_service)
        threads.binding_service=copy.copy(f.service);threads.binding_service.workers=workers
        coordinator=ProjectCoordinationService(service,threads=threads,turns=self.turn_service,scope=self.scope,
            bindings=self.bindings,workers=workers,memory=self.memory,approvals=self.approvals,attention=self.attention)
        service.project_coordination=coordinator
        registry=ActionProviderRegistry(ActionProviderStateStore(f.sqlite))
        registry.register(ProjectCoordinationActionProvider(coordinator))
        service.action_intents=ActionIntentService(ActionIntentStore(f.sqlite),ActionExecutionService(registry,f.resources),
            authority=self.authority,identity=f.identity,execution_workers=workers,execution_profiles=f.execution_profiles)
        broker=AssignmentBoundControlPlaneBroker(service,assignment=origin,worker_id=f.worker.id,
            worker_actor=self.worker_actor,fence=origin.fence,validator=lambda:workers.store.assignment(origin.id))
        await broker.start();self.addAsyncCleanup(broker.stop)
        return SimpleNamespace(broker=broker,coordinator=coordinator,origin=origin,threads=threads)

    async def racing_calls(self, profiles=('engineer','engineer')):
        second=await self.second_instance()
        original_route=self.thread_service.routing_service.route
        barrier=asyncio.Barrier(2)
        async def route(request,*,actor):
            result=await original_route(request,actor=actor)
            await asyncio.wait_for(barrier.wait(),1)
            return result
        self.thread_service.routing_service=SimpleNamespace(route=route)
        second.threads.routing_service=SimpleNamespace(route=route)
        path='/api/project-coordination/threads?project_id=home&repository_resource_id='+self.f.repository.id
        async def call(holder,profile,key):
            return await self._request.__func__(holder,'POST',path,payload={
                'profile_id':profile,'profile_revision':1,'idempotency_key':key})
        results=await asyncio.gather(call(self,profiles[0],'instance-one'),call(second,profiles[1],'instance-two'))
        self.assertTrue(all(v[0]==200 for v in results),results)
        return results,second

    async def test_F1_distinct_instances_reserve_child_budget_atomically(self):
        results,_=await self.racing_calls()
        winners=[v for v in results if v[2]['intent']['status']=='succeeded']
        self.assertEqual(len(winners),1,results)
        self.assertEqual(self.manager.starts,1)

    async def test_F2_stopped_process_does_not_resurrect_retained_marker(self):
        thread,_=await self.create()
        binding=self.bindings.get_by_thread(thread,self.actor)
        target=self.f.workers.store.assignment(binding.assignment_id)
        self.thread_service.active_turn_getter=lambda _:SimpleNamespace(assignment_id=target.id,fence=target.fence,
            turn_id='retained-native-turn',execution_id='retained-execution')
        self.manager.sessions[target.id].status=lambda:SimpleNamespace(worker_id=target.assigned_worker_id,
            fence=target.fence,running=False,diagnostic_state='unavailable',pending_executor_work=1)
        status,_,value=await self.call('GET','/threads/'+thread)
        self.assertEqual(status,200,value)
        self.assertEqual(value['item']['runner']['state'],'stopped')
        self.assertEqual(value['item']['activity'],'unknown',value)

    def authority_for_operator(self, operator_id, restricted=False):
        from codex_web.authority_seed import authority_role_catalog_seed_payload
        payload=authority_role_catalog_seed_payload()
        payload['roles'].append(dict(payload['roles'][0],id='restricted-reviewers',name='Restricted reviewers',grants=[dict(payload['roles'][0]['grants'][0],id='restricted.read',capability='memory.read')]))
        payload['roles'].append({'id':'operator-inspector','name':'Operator inspector','description':'Read-only scoped inspection fixture','grants':[
            dict(payload['roles'][0]['grants'][0],id='operator.memory',capability='memory.read',level='read',project_ids=['home']),
            dict(payload['roles'][0]['grants'][0],id='operator.coordinate',capability='project.coordinate',level='read',project_ids=['home'])]})
        payload['bindings'].append(dict(payload['bindings'][0],id='operator-binding',subject_id=operator_id,role_id='operator-inspector'))
        payload['bindings'].append(dict(payload['bindings'][0],id='requester-reviewers',role_id='restricted-reviewers'))
        if restricted:
            payload['bindings'].append(dict(payload['bindings'][0],id='operator-reviewers',subject_id=operator_id,role_id='restricted-reviewers'))
        active=self.authority.catalog_record(actor=self.actor,project_id='home')
        record=self.registry.create_draft(DefinitionDraftCreate(definition_id=active.definition_id,kind=active.kind,
            definition_schema_version=active.definition_schema_version,scope_type=active.scope_type,scope_id=active.scope_id,
            payload=payload,actor='fixture'))
        self.registry.approve_publication(record.record_id,actor='independent-reviewer',reference='test-review',reason='Fixture authority scope')
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))

    async def operator_fixture(self):
        human,_=self.f.identity.create_human_user(HumanUserCreate(id='review-operator',display_name='Review operator',roles=['admin']),actor=self.actor)
        self.operator=self.f.identity.actor_for_identity(human.id,scope=self.actor.tenant).model_copy(update={'assurance':AuthenticationAssurance.MFA})
        self.authority_for_operator(self.operator.identity_id)
        app=FastAPI()
        @app.middleware('http')
        async def identity(request,call_next):
            request.state.identity_actor=self.operator
            return await call_next(request)
        app.include_router(build_control_plane_broker_router(self.service))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')

    async def inspect(self,client,**query):
        return await client.get('/api/control-plane-broker/coordination',params={
            'assignment_id':self.assignment.id,'project_id':'home','repository_resource_id':self.f.repository.id,
            'view':'memory',**query})

    async def test_F3_actual_operator_cannot_read_requesters_restricted_memory(self):
        async with await self.operator_fixture() as client:
            record=self.memory.create(KnowledgeCreate(logical_key='restricted',object_type='project',project_id='home',
                title='Restricted',summary='PRIVATE_ROLE_SUMMARY',content='PRIVATE_CONTENT',classification='internal',
                required_role_ids=('restricted-reviewers',),provenance={'source_kind':'other','source_ref':'fixture'}),actor=self.actor)
            self.assertNotIn(record.id,{v.id for v in self.memory.list(actor=self.operator,project_id='home')})
            self.assertIn(record.id,{v.id for v in self.memory.list(actor=self.actor,project_id='home')})
            result=await self.inspect(client)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['count'],0,result.text)
            self.assertNotIn('PRIVATE_ROLE_SUMMARY',result.text)


    async def test_F1_two_instances_enforce_delivery_ceiling_with_existing_reservation(self):
        thread,_=await self.create()
        binding=self.bindings.get_by_thread(thread,self.actor)
        child=self.f.workers.store.assignment(binding.assignment_id)
        self.replace_assignment(child.model_copy(update={'agent_profile':None}))
        results,_=await self.racing_calls(('engineer','scanner'))
        self.assertEqual(sum(v[2]['intent']['status']=='succeeded' for v in results),1,results)
        current=[v for v in self.coordination.assignments(self.assignment,self.coordination.guard(self.assignment,
            OPERATION_MAP['project.threads.list'])[2])
            if v.execution_profile_id!='project-governor' and v.status in {'pending','claimed','running'}]
        self.assertEqual(len(current),2)

    async def test_F1_pending_reservation_survives_owner_loss_and_fenced_cleanup(self):
        from codex_web.execution_workers import AssignmentCancelRequest
        from codex_web.services.execution_workers import WorkerConflictError
        original_start=self.manager.start
        pending_seen=asyncio.Event();proceed=asyncio.Event();pending=[]
        async def hold_start(assignment_id):
            pending.append(self.f.workers.store.assignment(assignment_id))
            pending_seen.set();await proceed.wait()
            return await original_start(assignment_id)
        self.manager.start=hold_start
        payload=dict(profile_id='engineer',profile_revision=1,idempotency_key='pending-reservation')
        creating=asyncio.create_task(self.call('POST','/threads',payload))
        try:
            await wait_for_pending_phase(pending_seen, creating)
            self.assertEqual(pending[0].status,'pending')
            status,_,second=await self.call('POST','/threads',dict(payload,idempotency_key='competing-reservation'))
            self.assertEqual(status,200,second)
            self.assertNotEqual(second['intent']['status'],'succeeded')
            # A claimed successor changes the child fence. Old pending-owner
            # cleanup may neither cancel it nor free its capacity reservation.
            claimed=self.f.workers.claim(self.f.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=pending[0].id)
            self.assertIsNotNone(claimed)
            with self.assertRaises(WorkerConflictError):
                self.f.workers.cancel_bootstrap(claimed.id,AssignmentCancelRequest(expected_fence=0,reason='stale cleanup'),actor=self.actor)
            self.assertEqual(self.f.workers.store.assignment(claimed.id).status,'claimed')
            self.f.workers.cancel_bootstrap(claimed.id,AssignmentCancelRequest(expected_fence=claimed.fence,reason='explicit owned failure cleanup'),actor=self.actor)
            self.assertTrue(__import__('pathlib').Path(self.f.workspaces.store.workspace(claimed.execution_workspace_id).path).exists())
        finally:
            proceed.set()
            await creating
        self.manager.start=original_start
        await self.create('engineer','after-cleanup')
        self.assertEqual(self.manager.starts,1)

    async def test_F1_origin_fence_takeover_before_transaction_inserts_no_child(self):
        original=self.f.workers.create_assignment
        def create_assignment(payload, **kwargs):
            if payload.execution_profile_id == 'repository-write':
                self.replace_assignment(self.assignment.model_copy(update={'fence':self.assignment.fence+1}))
            return original(payload, **kwargs)
        self.f.workers.create_assignment=create_assignment
        before={v.id for v in self.f.workers.store.load().assignments}
        status,_,value=await self.call('POST','/threads',dict(profile_id='engineer',profile_revision=1,idempotency_key='takeover'))
        self.assertEqual(status,403,value)
        self.assertEqual({v.id for v in self.f.workers.store.load().assignments},before)
        self.assertEqual(self.manager.starts,0)

    async def test_F1_lost_origin_does_not_free_pending_child_for_another_instance(self):
        from codex_web.execution_workers import AssignmentCancelRequest
        second=await self.second_instance()
        original_start=self.manager.start
        seen=asyncio.Event();proceed=asyncio.Event();pending=[]
        async def parked(assignment_id):
            pending.append(self.f.workers.store.assignment(assignment_id))
            seen.set();await proceed.wait()
            return await original_start(assignment_id)
        self.manager.start=parked
        payload=dict(profile_id='engineer',profile_revision=1,idempotency_key='lost-origin')
        creating=asyncio.create_task(self.call('POST','/threads',payload))
        path='/api/project-coordination/threads?project_id=home&repository_resource_id='+self.f.repository.id
        try:
            await wait_for_pending_phase(seen, creating)
            self.replace_assignment(self.assignment.model_copy(update={'lease':self.assignment.lease.model_copy(update={'expires_at':1})}))
            status,_,value=await self._request.__func__(second,'POST',path,payload=dict(payload,idempotency_key='successor-before-cleanup'))
            self.assertEqual(status,200,value)
            self.assertNotEqual(value['intent']['status'],'succeeded')
            self.assertEqual(self.manager.starts,0)
            self.f.workers.cancel_bootstrap(pending[0].id,AssignmentCancelRequest(expected_fence=0,reason='explicit fenced abandoned reservation cleanup'),actor=self.actor)
        finally:
            proceed.set()
            await creating
            self.manager.start=original_start
        status,_,value=await self._request.__func__(second,'POST',path,payload=dict(payload,idempotency_key='successor-after-cleanup'))
        self.assertEqual(status,200,value)
        self.assertEqual(value['intent']['status'],'succeeded',value)
        self.assertEqual(self.manager.starts,1)

    async def test_F2_activity_matrix_preserves_queue_and_retained_evidence(self):
        thread,_=await self.create()
        binding=self.bindings.get_by_thread(thread,self.actor)
        original=self.f.workers.store.assignment(binding.assignment_id)
        marker=SimpleNamespace(assignment_id=original.id,fence=original.fence,turn_id='matching-turn',execution_id='matching-execution')
        session=self.manager.sessions[original.id]; original_status=session.status
        self.host.queues[thread]=[SimpleNamespace(id='retained-queued')]
        cases=[('matching',{},marker,None,'active'),('live-idle',{},None,None,'idle'),
            ('stale-assignment',{},SimpleNamespace(**{**vars(marker),'assignment_id':'old'}),None,'unknown'),
            ('stale-fence',{},SimpleNamespace(**{**vars(marker),'fence':99}),None,'unknown'),
            ('missing-native-turn',{},SimpleNamespace(**{**vars(marker),'turn_id':None}),None,'unknown'),
            ('stopped',{},marker,False,'unknown'),('lost',{'status':'lost','lease':None},marker,None,'unknown'),
            ('expired',{'lease':original.lease.model_copy(update={'expires_at':1})},marker,None,'unknown')]
        fixtures={}
        for name,updates,current_marker,running,expected in cases:
            self.replace_assignment(original.model_copy(update=updates))
            self.thread_service.active_turn_getter=lambda _,m=current_marker:m
            session.status=original_status if running is None else lambda:SimpleNamespace(worker_id=original.assigned_worker_id,
                fence=original.fence,running=False,diagnostic_state='unavailable',pending_executor_work=1)
            status,_,detail=await self.call('GET','/threads/'+thread)
            self.assertEqual(status,200,(name,detail))
            row=detail['item'];self.assertEqual(row['activity'],expected,(name,row))
            self.assertEqual(row['queue_depth'],1)
            self.assertEqual(row['retained_turn_id'],getattr(current_marker,'turn_id',None))
            status,_,rows=await self.call('GET','/threads')
            self.assertEqual(rows['items'][0]['activity'],expected,(name,rows))
            fixtures[name]=row
        self.replace_assignment(original);session.status=original_status
        with patch.object(self.thread_service,'_runner_projection',return_value=None):
            status,_,detail=await self.call('GET','/threads/'+thread)
            self.assertEqual(detail['item']['activity'],'unknown')
            fixtures['unavailable']=detail['item']
        import os,pathlib
        if os.environ.get('CODEX_WEB_REVIEW_FIXTURE_DIR'):
            pathlib.Path(os.environ['CODEX_WEB_REVIEW_FIXTURE_DIR'],'runner-fixtures.json').write_text(json.dumps(fixtures,indent=2))

    async def test_F3_role_grant_revoke_and_source_scope_use_operator_principal(self):
        from codex_web.organizational_memory import KnowledgeRelationshipCreate
        from codex_web.services.organizational_memory import KnowledgeAuthorizationError
        async with await self.operator_fixture() as client:
            private=self.memory.create(KnowledgeCreate(logical_key='private',object_type='project',project_id='home',title='Private',
                summary='PRIVATE_ROLE_SUMMARY',content='private',required_role_ids=('restricted-reviewers',),
                provenance={'source_kind':'other','source_ref':'role-source'}),actor=self.actor)
            with self.assertRaises(KnowledgeAuthorizationError):self.memory.get(private.id,actor=self.operator)
            denied=await self.inspect(client,limit=1)
            self.assertEqual(denied.json()['count'],0)
            self.assertIsNone(denied.json()['next_cursor'])
            self.authority_for_operator(self.operator.identity_id,restricted=True)
            allowed=await self.inspect(client)
            self.assertEqual(allowed.json()['count'],1)
            self.assertEqual(allowed.json()['items'][0]['version'],private.version)
            self.assertEqual(allowed.json()['items'][0]['provenance']['source_ref'],'role-source')
            self.authority_for_operator(self.operator.identity_id,restricted=False)
            revoked=await self.inspect(client)
            self.assertEqual(revoked.json()['count'],0)
            foreign=self.memory.create(KnowledgeCreate(logical_key='foreign-source',object_type='project',project_id='foreign',
                title='Foreign source',summary='FOREIGN_SOURCE',content='foreign',provenance={'source_kind':'other','source_ref':'foreign'}),actor=self.actor)
            derived=self.memory.create(KnowledgeCreate(logical_key='derived',object_type='project',project_id='home',
                title='Derived',summary='PRIVATE_DERIVED_SUMMARY',content='derived',provenance={'source_kind':'other','source_ref':'derived'},
                relationships=(KnowledgeRelationshipCreate(target_knowledge_id=foreign.id,relationship_type='derived_from'),)),actor=self.actor)
            with self.assertRaises(KnowledgeAuthorizationError):self.memory.get(derived.id,actor=self.operator)
            hidden=await self.inspect(client)
            self.assertEqual(hidden.json()['count'],0,hidden.text)
            self.assertNotIn('PRIVATE_DERIVED_SUMMARY',hidden.text)
            for query in ({'project_id':'foreign'},{'repository_resource_id':'resource-foreign'}):
                result=await self.inspect(client,**query)
                self.assertIn(result.status_code,(403,404),result.text)
            original_operator=self.operator
            self.operator=self.operator.model_copy(update={'organization_id':'foreign-tenant'})
            tenant_denied=await self.inspect(client)
            self.assertEqual(tenant_denied.status_code,404)
            self.operator=original_operator
            import os,pathlib
            if os.environ.get('CODEX_WEB_REVIEW_FIXTURE_DIR'):
                pathlib.Path(os.environ['CODEX_WEB_REVIEW_FIXTURE_DIR'],'memory-fixtures.json').write_text(json.dumps({
                    'denied':denied.json(),'allowed':allowed.json(),'revoked':revoked.json(),'derived-denied':hidden.json()},indent=2))


def load_tests(loader,tests,pattern):
    # Exercise correction witnesses here; the original module owns its own suite.
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(PendingPhaseSynchronizationTests),
        *(ReviewCorrectionsTests(name) for name in ReviewCorrectionsTests.__dict__ if name.startswith('test_')),
    ])
