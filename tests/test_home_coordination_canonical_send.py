"""Real Home authorizer -> ActionIntent -> TurnService -> guarded native wire.

Only worker filesystem/process/credential and native wire are fake. Canonical
identity, current Definitions, authority, assignments, scopes, preflight and
retained queue use private SQLite. No application import or host process.
"""
import asyncio
import io
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.agent_profiles import AgentProfileCreate, AgentProfileUpdate
from codex_web.agent_runtime import AgentRuntimeSessionRequest, AgentSession
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest
from codex_web.identity import MembershipRole
from codex_web.models import IndexedThread, Project
from codex_web.resources import ResourceCreate, ResourceAlias
from codex_web.runtime.codex import CodexRuntime
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionProviderRegistry, ActionExecutionService
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.home_operator_coordination import HomeOperatorCoordinationService, HomeOperatorCoordinationActionProvider, MESSAGE_ACTION
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_transcript import ThreadTranscriptService
from codex_web.services.turns import TurnService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.turn_queue import TurnQueueRepository
from tests import test_home_operator_host as home_fixture
from tests.test_turn_execution import _Host
from tests.test_codex_runtime import _Host as CodexHost
from tests.test_codex_worker_session import _FakeBackend, _FakeDelegationService


class _NativeSessions:
    """External process manager seam; real assignment-bound sessions are retained."""
    def __init__(self, sessions, completed):
        self.sessions, self.completed = sessions, completed
    def get(self, assignment_id):
        return self.sessions.get(assignment_id)
    async def start(self, assignment_id):
        return self.sessions[assignment_id]
    async def complete(self, assignment_id, **kwargs):
        self.completed.append((assignment_id,kwargs))


class HomeCanonicalSendTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only setup/helpers, never inherit the parent's test methods.
    select = home_fixture.HomeOperatorHostTests.select
    current = home_fixture.HomeOperatorHostTests.current
    mutate_assignment = home_fixture.HomeOperatorHostTests.mutate_assignment
    stop = home_fixture.HomeOperatorHostTests.stop

    def publish(self, payload):
        if not hasattr(self, 'host_resource'):
            self.host_resource = self.f.resources.create(ResourceCreate(resource_type='environment', name='Private Home'), actor=self.actor)
        profile = next(p for p in payload['profiles'] if p['id'] == 'orchestration-only')
        profile['control_plane_operations'] = list(dict.fromkeys([*profile['control_plane_operations'], MESSAGE_ACTION, 'operator.project.threads.list']))
        profile['home_operator'] = {'host_resource_id':self.host_resource.id, 'trusted_worker_ids':[self.f.worker.id],
            'host_network_access':True, 'project_targets':[{'project_id':'data', 'child_profiles':[{'profile_id':'quinn', 'revision':1}]}]}
        record = self.registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',kind='execution-profile-catalog',
            definition_schema_version='1.3',scope_type='project',scope_id='home',payload=payload,actor=self.actor.identity_id))
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor=self.actor.identity_id))
        return record

    async def asyncSetUp(self):
        await home_fixture.HomeOperatorHostTests.asyncSetUp(self)
        f = self.f
        self.data = Project(id='data',organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            name='Data',path=str(self.root/'data-repository'))
        Path(self.data.path).mkdir()
        self.projects.repository.save([*self.projects.list(),self.data])
        f.workers.store.update(lambda state:state.model_copy(update={'workers':[w.model_copy(update={'max_concurrency':3}) for w in state.workers]}))
        self.worker_actor = f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,
            name='Canonical fixture worker',scope=self.actor.tenant,service_scopes=('execution-worker:run','action-intent:worker'))
        self.repository = self.repository_resource('Data',Path(self.data.path))
        self.core = self.repository_resource('Core',self.root/'core-read-only')
        f._publish_secret(scope_id='data')
        profile = self.profiles.create(AgentProfileCreate(profile_id='quinn',name='Data Quinn',execution_profile_id='repository-write',
            sandbox_requirement='workspace-write',authority_role_id='local-admin'),actor=self.actor)
        bound = f.service.prepare_bootstrap(bootstrap_id='data-existing',execution_id='data-existing-execution',project_id='data',
            sandbox='workspace-write',approval_policy='on-request',execution_profile_id='repository-write',
            explicit_repository_id=self.repository.id,writable_repository_ids=(self.repository.id,),read_only_repository_ids=(self.core.id,),
            agent_profile=self.profiles.binding_for(profile))
        claimed = f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=bound.assignment_id)
        self.target = f.workers.start(f.worker.id,bound.assignment_id,AssignmentStartRequest(fence=claimed.fence,
            lease_token=claimed.lease.lease_token),actor=self.worker_actor)
        self.thread = 'preexisting-data-thread'
        self.bindings.bind(bootstrap_id='data-existing',thread_id=self.thread,execution_id=bound.execution_id,
            assignment_id=bound.assignment_id,execution_workspace_id=self.target.execution_workspace_id,actor=self.actor)
        self.index.upsert(IndexedThread(id=self.thread,name='Existing Quinn',project_id='data'))
        self.sessions.store.upsert(AgentSession(organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            project_id='data',provider_id='openai',runtime_id='codex',runtime_type='app-server',provider_native_session_id=self.thread,
            execution_id=bound.execution_id,assignment_id=bound.assignment_id,execution_workspace_id=self.target.execution_workspace_id,status='ready'))
        self.bindings.mark_initial_turn_started(self.thread,self.actor)
        self.host = _Host()
        self.runtime_state = RuntimeStateRepositories(f.sqlite,thread_settings_file=self.root/'settings.json',
            active_turns_file=self.root/'active.json',work_item_states_file=self.root/'work.json')
        self.queue_store = TurnQueueRepository(f.sqlite,self.root/'queues.json')
        self.host._thread_queue_record = self.host._thread_queue = self.queue_store.get
        self.host._update_thread_queue_record = self.queue_store.update
        self.host._put_thread_queue_record = self.queue_store.put
        self.host._delete_thread_queue_record = self.queue_store.delete
        self.host._thread_queue_depth = lambda thread:len(self.queue_store.get(thread))
        self.host._thread_run_settings = lambda thread:self.settings.get(thread)
        self.host._project = lambda project_id:self.projects.get(project_id,self.actor.tenant)
        bindings = BotBindingSelectionService(lambda:[],binding_report_name=lambda b:'',binding_prefix=lambda b:'')
        repo = self.runtime_state.thread_settings
        self.settings = ThreadExecutionSettingsService(load_settings=repo.load,save_settings=repo.save,get_setting=repo.get,put_setting=repo.put,
            bindings=bindings,load_bindings=lambda:[],save_bindings=lambda _:None,gitlab_routing_enabled_for_project=lambda _:False,
            binding_report_name=lambda b:'',binding_prefix=lambda b:'')
        self.runtime = ProjectRuntimeService(self.projects)
        self.recovery = ThreadRecoveryService(self.host,projects=self.runtime,settings=self.settings,naming=None,
            thread_index=self.index,runtime_request=None,event_sink=self.host.events.append,truncate_text=lambda s,n:str(s)[:n])
        self.completed = []
        self.native_sessions = {}
        self.manager = _NativeSessions(self.native_sessions,self.completed)
        self.execution = TurnExecutionService(self.host,binding_service=f.service,session_manager=self.manager,
            bootstrap_bindings=self.bindings,control_actor=self.actor,
            actor_resolver=lambda identity,project:f.identity.actor_for_identity(identity,scope=self.actor.tenant),
            transcript=ThreadTranscriptService(f.sqlite))
        self.execution.schedule_queue_drain = Mock()  # Explicit test-owned drain, no autonomous work.
        self.execution.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        self.execution.automated_admission_allowed = lambda:True
        self.turns = TurnService(projects=self.runtime,settings=self.settings,recovery=self.recovery,resume_runtime=None,
            bindings=bindings,queue_policy=NS(depth=self.host._thread_queue_depth,queue=self.host._thread_queue),execution=self.execution,
            event_sink=self.host.events.append,truncate_text=lambda s,n:str(s)[:n],binding_public=lambda v:v,agent_profiles=self.profiles)
        def origin(snapshot,operation):
            context = self.service.home_operator_host.authorize_operation(snapshot,operation_id=operation,
                worker_actor=self.worker_actor,mutating=operation==MESSAGE_ACTION)
            return context.assignment,context.requester,context.binding,context.envelope
        self.coordination = HomeOperatorCoordinationService(broker=self.service,origin_guard=origin,thread_scope=self.scope,
            bootstrap_bindings=self.bindings,project_coordination=None,turns=self.turns)
        self.service.home_operator_coordination = self.coordination
        self.execution.home_operator_coordination_admission = self.coordination.validate_queued_delegation
        self.action_registry = ActionProviderRegistry(ActionProviderStateStore(f.sqlite))
        self.provider = HomeOperatorCoordinationActionProvider(self.coordination)
        self.action_registry.register(self.provider)
        self.action_binding = self.action_registry.bind(ActionProviderBindingCreate(provider_type='home-operator-coordination',
            provider_instance='canonical',project_id='home',resource_ids=(self.host_resource.id,)),actor=self.actor,resources=f.resources)
        self.intents = ActionIntentService(ActionIntentStore(f.sqlite),ActionExecutionService(self.action_registry,f.resources),
            authority=self.authority,identity=f.identity,execution_workers=f.workers,execution_profiles=f.execution_profiles)
        self.service.action_intents = self.intents
        await self.attach_native()

    def repository_resource(self,name,path):
        path.mkdir(exist_ok=True)
        resource = self.f.resources.create(ResourceCreate(resource_type='repository',name=name,
            aliases=[ResourceAlias(namespace='filesystem',value=str(path))]),actor=self.actor)
        self.f.resources.bind_project(project=self.data,resource_id=resource.id,actor=self.actor)
        return resource

    async def attach_native(self):
        credentials = _FakeDelegationService()
        worker = LocalExecutionWorkerRuntime(self.f.workers,self.f.workspaces,_FakeBackend(),worker=self.f.worker,
            worker_actor=self.worker_actor,control_actor=self.actor,codex_auth_delegation=credentials)
        session = AssignmentBoundAgentProcessSession(worker,self.host,self.target.id,runtime_factory=CodexRuntime,
            credential_provider=credentials,runtime_binding=self.target.runtime_binding)
        self.addCleanup(session._supervision_executor.shutdown,wait=True,cancel_futures=True)
        session.fence = self.target.fence
        session.delegation = credentials._delegation(self.target,self.f.worker.id,self.target.fence)
        session.workspace_path = Path(self.f.workspaces.get(self.target.execution_workspace_id,self.actor).path)
        session.started_monotonic = time.monotonic()
        native = CodexRuntime(CodexHost(),restart_on_timeout=False)
        native.native_runtime_version = '0.160.0'  # Synthetic qualified wire boundary.
        native.ensure_started = AsyncMock()
        native.ready.set()
        self.wire = io.StringIO()
        native.proc = NS(poll=lambda:None,stdin=self.wire,pid=99999999)
        session.runtime = native
        self.native_sessions[self.target.id] = session
        self.native = native
        self.writes_entered = asyncio.Event()
        self.hold_writer = False
        send = native._send
        async def reply(message,**options):
            if message['method']=='turn/start' and self.hold_writer:
                await native.write_lock.acquire()
                self.writes_entered.set()
            await send(message,**options)
            result = {'turn':{'id':'canonical-home-delegated-turn','threadId':self.thread}} if message['method']=='turn/start' else {'thread':{'id':self.thread}}
            await native._dispatch_message({'id':message['id'],'result':result})
        native._send = reply
        await CodexAgentRuntimeAdapter(session).create_session(AgentRuntimeSessionRequest(project_id='data'))

    async def message(self,key='one-original'):
        return await self.coordination.message(assignment_id=self.assignment.id,fence=self.assignment.fence,
            payload={'project_id':'data','thread_id':self.thread,'message':'One bounded current Data review','idempotency_key':key},
            worker_actor=self.worker_actor)

    def starts(self):
        return [json.loads(line) for line in self.wire.getvalue().splitlines() if json.loads(line)['method']=='turn/start']

    async def test_actual_home_intent_queue_and_native_start_preserve_full_scope(self):
        result = await self.message()
        self.assertEqual(result['item']['status'],'succeeded',result['item']['last_error'])
        history = self.intents.history(result['item']['id'],self.actor)
        self.assertEqual(len(history['receipts']),1)
        original = self.queue_store.get(self.thread)[0].model_copy(deep=True)
        self.assertEqual(original.project_coordination_dispatch.schema_version,'1.2')
        self.assertEqual(original.writable_repository_resource_ids,(self.repository.id,))
        self.assertEqual(original.read_only_repository_resource_ids,(self.core.id,))
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1,(self.host.events,self.execution.queued_preflight.store.get('preflight-queued-'+original.id).model_dump()))
        saved = self.execution.queued_preflight.store.get('preflight-queued-'+original.id)
        self.assertEqual(saved.status,'started')
        self.assertEqual(saved.queued_start_receipt.provider_native_turn_id,'canonical-home-delegated-turn')
        self.assertEqual(self.queue_store.get(self.thread),[])
        self.assertEqual(self.completed,[])
        self.assertEqual(self.f.workers.store.assignment(self.target.id).fence,self.target.fence)
        self.assertEqual(len(self.intents.store.load().intents),1)

    async def test_accept_then_lost_queue_response_keeps_one_uncertain_original(self):
        real_start = self.turns.start
        async def lost(*args,**kwargs):
            await real_start(*args,**kwargs)
            raise TimeoutError('Synthetic caller loss after actual canonical queue acceptance')
        self.turns.start = lost
        result = await self.message()
        self.assertEqual(result['item']['status'],'uncertain')
        original = self.queue_store.get(self.thread)[0].model_copy(deep=True)
        self.assertEqual(result['item']['attempt'],1)
        self.assertFalse(result['item']['provider_idempotency_supported'])
        self.assertIsNone(result['item']['request']['idempotency_key'])
        again = await self.message()
        self.assertEqual(again['item']['id'],result['item']['id'])
        self.assertEqual(again['item']['status'],'uncertain')
        self.assertEqual(len(self.intents.store.load().intents),1)
        self.assertEqual([row.id for row in self.queue_store.get(self.thread)],[original.id])
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(),[])
        self.assertEqual(self.queue_store.get(self.thread)[0].model_dump(),original.model_dump())
        self.assertEqual(self.completed,[])

    async def revoke_at_writer(self, kind):
        result = await self.message()
        self.assertEqual(result['item']['status'],'succeeded')
        original = self.queue_store.get(self.thread)[0].model_copy(deep=True)
        self.hold_writer = True
        task = asyncio.create_task(self.execution.drain_thread_queue(self.thread))
        try:
            await asyncio.wait_for(self.writes_entered.wait(),5.)
            if kind=='home':
                self.select(None,1)
            elif kind=='home-definition':
                self.publish(self.payload)
            elif kind=='target-fence':
                self.f.workers.store.update(lambda state:state.model_copy(update={'assignments':[
                    row.model_copy(update={'fence':row.fence+1}) if row.id==self.target.id else row for row in state.assignments]}))
            elif kind=='profile':
                other = self.f.identity.bootstrap_service_actor(identity_id='other-profile-owner',name='Other current owner',
                    scope=self.actor.tenant,service_scopes=())
                self.profiles.update('quinn',AgentProfileUpdate(owner_identity_id=other.identity_id,
                    access={'mode':'owner'},reason='Revoke current invoker at writer boundary'),actor=self.actor)
            elif kind=='role':
                record = self.authority.catalog_record(actor=self.actor,project_id='home')
                self.registry.quarantine(record.record_id,actor=self.actor.identity_id,reason='Final writer authority revocation')
            elif kind=='admin':
                self.f.identity.store.update(lambda state:state.model_copy(update={'memberships':[
                    row.model_copy(update={'roles':(MembershipRole.MEMBER,)}) if row.identity_id==self.actor.identity_id else row for row in state.memberships]}))
            else:
                raise AssertionError(kind)
        finally:
            if self.native.write_lock.locked():
                self.native.write_lock.release()
            await task
        self.assertEqual(self.starts(),[])
        self.assertEqual(self.completed,[])
        self.assertEqual([row.id for row in self.queue_store.get(self.thread)],[original.id])
        saved = self.execution.queued_preflight.store.get('preflight-queued-'+original.id)
        self.assertIsNone(saved.queued_start_receipt)
        self.assertEqual(saved.queued_turn_snapshot,original.model_dump(mode='json'))
        self.assertEqual(self.execution.transcript.read(self.thread)['turns'],[])
        self.assertEqual(len(self.intents.store.load().receipts),1)

    async def test_home_selection_revocation_at_actual_writer_holds_original(self):
        await self.revoke_at_writer('home')

    async def test_home_definition_revocation_at_actual_writer_holds_original(self):
        await self.revoke_at_writer('home-definition')

    async def test_target_fence_revocation_at_actual_writer_holds_original(self):
        await self.revoke_at_writer('target-fence')

    async def test_target_profile_revocation_at_actual_writer_holds_original(self):
        await self.revoke_at_writer('profile')

    async def test_role_revocation_at_actual_writer_holds_original(self):
        await self.revoke_at_writer('role')

    async def test_current_admin_revocation_at_actual_writer_holds_original(self):
        await self.revoke_at_writer('admin')

    async def test_home_source_without_guarded_transport_refuses_before_native_input(self):
        await self.message()
        original = self.queue_store.get(self.thread)[0].model_copy(deep=True)
        session = self.native_sessions[self.target.id]
        session.request_with_start_capture = None
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(),[])
        self.assertEqual(self.queue_store.get(self.thread)[0].model_dump(),original.model_dump())
        self.assertEqual(self.completed,[])


def load_tests(loader,tests,pattern):
    return loader.loadTestsFromTestCase(HomeCanonicalSendTests)
