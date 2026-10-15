"""Assignment-bound project coordination must be explicitly composed."""
import unittest

from codex_web.services.control_plane_broker import ControlPlaneBrokerService


class ProjectCoordinationCompositionTests(unittest.TestCase):
    def test_broker_has_typed_project_coordination_operations(self):
        operations = {item.id for item in ControlPlaneBrokerService.operations()}
        self.assertTrue({
            'project.threads.list', 'project.thread.read', 'project.thread.create',
            'project.thread.message', 'project.memory.list', 'project.memory.read',
            'project.memory.checkpoint', 'project.elevation.read',
        }.issubset(operations))

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.agent_profiles import AgentProfileCreate
from codex_web.authority import AuthorityEvaluationRequest
from codex_web.control_plane_broker import ControlPlaneBrokerLimits
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionProviderRegistry, ActionExecutionService
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_runtime import AgentSessionService, AgentRuntimeRegistry
from codex_web.services.control_plane_broker import ControlPlaneBrokerService, AssignmentBoundControlPlaneBroker
from codex_web.services.project_coordination import ProjectCoordinationService, ProjectCoordinationActionProvider, PROJECT_COORDINATION_OPERATIONS
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.projects import ProjectService
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.threads import ThreadService
from codex_web.services.turns import TurnService
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.attention import AttentionService
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.attention import AttentionStore
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from tests import test_turn_execution_binding as binding_tests, test_control_plane_broker as broker_tests
from tests.test_control_plane_broker import _WorkItems, _Operator
from tests.test_turn_execution import _Host
from tests.test_turn_service import _Settings, _Recovery, _Resume, _Bindings
from codex_web.models import IndexedThread


class _IsolatedTestManager:
    """Transport seam only; assignments, claims, quotas and fences remain real."""
    def __init__(self, fixture, actor):
        self.f, self.actor = fixture, actor
        self.sessions = {}
        self.starts = 0
        self.uncertain = False
        self.before_reply = None

    async def start(self, assignment_id):
        f = self.f
        claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(), actor=self.actor, assignment_id=assignment_id)
        if claimed is None:
            raise RuntimeError('worker capacity unavailable')
        started = f.workers.start(f.worker.id, assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.actor)
        workspace = f.workspaces.get(started.execution_workspace_id, f.actor)
        async def request(method, params):
            self.starts += 1
            if self.before_reply:
                self.before_reply()
            if self.uncertain:
                raise TimeoutError('native thread creation response unavailable')
            return {'thread': {'id': 'native-' + str(self.starts)}}
        session = SimpleNamespace(workspace_path=Path(workspace.path), request=request,
            status=lambda: SimpleNamespace(worker_id=f.worker.id, fence=started.fence, running=True),
            git_metadata_path=None, git_worktree_metadata_path=None)
        self.sessions[assignment_id] = session
        return session

    def get(self, assignment_id):
        return self.sessions.get(assignment_id)

    async def complete(self, assignment_id, **kwargs):
        # Failure cleanup is tested by existing canonical bootstrap suites.
        return None


class ProjectCoordinationBrokerTests(unittest.IsolatedAsyncioTestCase):
    _request = broker_tests.ControlPlaneBrokerTests._request

    async def asyncSetUp(self):
        self.f = binding_tests.TurnExecutionBindingTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        f = self.f
        f._publish_secret()
        f.service._clock = time.time
        f.workers.store.update(lambda state: state.model_copy(update={'workers': [
            w.model_copy(update={'max_concurrency': 3}) for w in state.workers]}))
        self.actor = f.actor
        self.worker_actor = f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,
            name='Test execution worker', scope=self.actor.tenant,
            service_scopes=('execution-worker:run', 'action-intent:worker'))
        self.registry = f.execution_profiles.registry
        self.authority = install_authority_roles(self.registry, f.resources)
        self.profiles = AgentProfileService(AgentProfileStore(f.sqlite), definitions=self.registry,
            authority=self.authority, execution_profiles=f.execution_profiles)
        f.execution_profiles.agent_profiles = self.profiles
        for profile_id in ('engineer', 'scanner'):
            self.profiles.create(AgentProfileCreate(profile_id=profile_id, name=profile_id,
                execution_profile_id='repository-write', sandbox_requirement='workspace-write'), actor=self.actor)
        self.payload = execution_profile_catalog_seed_payload()
        self.payload['profiles'].append(dict(id='project-governor', name='Project governor',
            description='Explicit bounded coordination', workspace_mode='repository', repository_access='mutable',
            required_worker_capabilities=['git','command_execution'],
            control_plane_operations=['control_plane.operations.list']+[v.id for v in PROJECT_COORDINATION_OPERATIONS],
            project_coordination={'repository_resource_id':f.repository.id,
                'child_profiles':[{'profile_id':p, 'revision':1} for p in ('engineer','scanner')], 'max_delivery_threads':2}))
        self.publish(self.payload)
        binding = f.service.prepare_bootstrap(bootstrap_id='bootstrap-governor', execution_id='execution-governor',
            project_id='home', sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=f.repository.id, execution_profile_id='project-governor')
        claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(), actor=self.worker_actor, assignment_id=binding.assignment_id)
        self.assignment = f.workers.start(f.worker.id, binding.assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.worker_actor)
        self.bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(f.sqlite))
        self.sessions = AgentSessionService(AgentSessionStore(f.sqlite), AgentRuntimeRegistry())
        self.index = ThreadIndexRepository(f.sqlite, Path(f.temp.name)/'threads.json')
        self.projects = ProjectService(SimpleNamespace(load=lambda:[f.project]))
        self.scope = ThreadScopeService(self.projects, self.sessions, self.index)
        self.runtime = ProjectRuntimeService(self.projects)
        self.manager = _IsolatedTestManager(f, self.worker_actor)
        profiles = self.profiles
        async def route(request, *, actor):
            child,_ = profiles.resolve_for_execution(request.agent_profile_id, revision=request.agent_profile_revision,
                actor=actor, project_id=request.project_id)
            return SimpleNamespace(selected_runtime=SimpleNamespace(execution_binding=lambda:f.service.runtime_binding),
                agent_profile=profiles.binding_for(child))
        self.settings = _Settings()
        self.thread_service = ThreadService(runtime_transport=None, runtime_request_for_thread=None,
            event_sink=lambda event:None, binding_service=f.service, session_manager=self.manager,
            bootstrap_bindings=self.bindings, control_actor=self.actor, agent_sessions=self.sessions,
            agent_profiles=self.profiles, routing_service=SimpleNamespace(route=route),
            project_runtime=self.runtime, settings=self.settings, thread_index=self.index,
            thread_scope=self.scope, active_turn_getter=lambda thread:None)
        self.host = _Host()
        execution = TurnExecutionService(self.host)
        execution.schedule_queue_drain = Mock()  # No autonomous test execution.
        self.turn_service = TurnService(projects=self.runtime, settings=self.settings, recovery=_Recovery(),
            resume_runtime=_Resume(), bindings=_Bindings(), queue_policy=SimpleNamespace(depth=self.host._thread_queue_depth, queue=self.host._thread_queue),
            execution=execution, event_sink=lambda event:None, truncate_text=lambda s,n:str(s)[:n],
            binding_public=lambda value:value, agent_profiles=self.profiles)
        self.memory = OrganizationalMemoryService(OrganizationalMemoryStore(f.sqlite),
            DataGovernanceService(DataGovernanceStore(f.sqlite)), self.authority)
        events = CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(f.sqlite)))
        self.approvals = ApprovalRequestService(ApprovalRequestStore(f.sqlite), f.identity, events)
        self.attention = AttentionService(AttentionStore(f.sqlite), events)
        self.service = ControlPlaneBrokerService(identity=f.identity, authority=self.authority,
            work_items=_WorkItems({}), operator=_Operator(), execution_profiles=f.execution_profiles,
            execution_workspaces=f.workspaces, audit=ControlPlaneBrokerAuditStore(f.sqlite),
            limits=ControlPlaneBrokerLimits(max_requests_per_minute=600,max_response_bytes=131072,max_request_bytes=65536))
        self.coordination = ProjectCoordinationService(self.service, threads=self.thread_service, turns=self.turn_service,
            scope=self.scope, bindings=self.bindings, workers=f.workers, memory=self.memory,
            approvals=self.approvals, attention=self.attention)
        self.service.project_coordination = self.coordination
        actions = ActionProviderRegistry(ActionProviderStateStore(f.sqlite))
        actions.register(ProjectCoordinationActionProvider(self.coordination))
        self.action_binding = actions.bind(ActionProviderBindingCreate(provider_type='project-coordination',
            provider_instance='canonical', project_id='home', resource_ids=(f.repository.id,)), actor=self.actor, resources=f.resources)
        self.intents = ActionIntentService(ActionIntentStore(f.sqlite), ActionExecutionService(actions,f.resources),
            authority=self.authority, identity=f.identity, execution_workers=f.workers, execution_profiles=f.execution_profiles)
        self.service.action_intents = self.intents
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id=f.worker.id, worker_actor=self.worker_actor, fence=self.assignment.fence,
            validator=lambda:f.workers.store.assignment(self.assignment.id))
        await self.broker.start()
        self.addAsyncCleanup(self.broker.stop)

    def publish(self,payload):
        record=self.registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
            kind='execution-profile-catalog',definition_schema_version='1.2',scope_type='project',scope_id='home',
            payload=payload,actor='fixture'))
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))
        return record

    async def call(self,method,path,payload=None,scope=True):
        query=('?project_id=home&repository_resource_id='+self.f.repository.id) if scope else ''
        return await self._request(method,'/api/project-coordination'+path+query,payload=payload)

    async def create(self,profile='engineer',key='create'):
        status,_,result=await self.call('POST','/threads',dict(profile_id=profile,profile_revision=1,idempotency_key=key))
        self.assertEqual(status,200,result)
        self.assertEqual(result['intent']['status'],'succeeded',result)
        self.assertTrue(result['verifications'][0]['verified'])
        return result['receipts'][0]['result']['output']['thread_id'],result

    async def test_two_real_bootstrap_threads_monitor_message_checkpoint_and_receipts(self):
        one,receipt=await self.create()
        two,_=await self.create('scanner','scanner')
        status,_,value=await self.call('GET','/threads')
        self.assertEqual(status,200,value)
        self.assertEqual({v['thread_id'] for v in value['items']},{one,two})
        self.assertEqual(value['count'],2)
        self.assertTrue(all(v['runner']['live'] for v in value['items']))
        status,_,message=await self.call('POST',f'/threads/{one}/messages',{'message':'bounded genuine task','idempotency_key':'message'})
        self.assertEqual(status,200,message)
        self.assertEqual(message['intent']['status'],'succeeded',message)
        self.assertEqual(len(self.host._thread_queue(one)),1)
        self.assertFalse(message['receipts'][0]['result']['output']['native_start_verified'])
        _,_,same=await self.call('POST',f'/threads/{one}/messages',{'message':'bounded genuine task','idempotency_key':'message'})
        self.assertEqual(same['intent']['id'],message['intent']['id'])
        self.assertEqual(len(self.host._thread_queue(one)),1)
        status,_,checkpoint=await self.call('POST','/memory/checkpoint',{'key':'progress','summary':'two owners',
            'content':'Structured retained evidence; not authority','idempotency_key':'checkpoint'})
        self.assertEqual(status,200,checkpoint)
        self.assertEqual(checkpoint['intent']['status'],'succeeded',checkpoint)
        memory_id=checkpoint['receipts'][0]['result']['output']['records'][0]['id']
        status,_,knowledge=await self.call('GET','/memory/'+memory_id)
        self.assertEqual(status,200,knowledge)
        self.assertEqual(knowledge['item']['project_id'],'home')
        status,_,again=await self.call('POST','/threads',{'profile_id':'engineer','profile_revision':1,'idempotency_key':'create'})
        self.assertEqual(again['intent']['id'],receipt['intent']['id'])
        self.assertEqual(self.manager.starts,2)
        _,_,evidence=await self.call('GET','/actions/'+receipt['intent']['id'])
        self.assertTrue(evidence['verifications'][0]['verified'])

    async def test_scope_and_input_injection_fail_before_effect(self):
        cases=[('/threads',False),('/threads?project_id=foreign',False),
            ('/threads?project_id=home&repository_resource_id=resource-foreign',False),
            ('/threads?project_id=home&project_id=foreign&repository_resource_id='+self.f.repository.id,False)]
        for path,scope in cases:
            status,_,value=await self.call('GET',path,scope=scope)
            self.assertEqual(status,403,value)
        for extra in ('actor','provider_id','sandbox','execution_profile_id','repository_resource_id','project_id','force'):
            status,_,value=await self.call('POST','/threads',dict(profile_id='engineer',profile_revision=1,
                idempotency_key=extra,**{extra:'injection'}))
            self.assertEqual(status,422,value)
        self.assertEqual(self.manager.starts,0)

    async def test_stale_fence_lease_deadline_requester_and_workspace_deny(self):
        original=self.assignment
        for updates in ({'fence':2},{'deadline_at':1},{'lease':original.lease.model_copy(update={'expires_at':1})},
                        {'created_by':'missing-requester'},{'project_id':'foreign'}, {'workspace_id':'foreign'}):
            self.f.workers.store.update(lambda s:s.model_copy(update={'assignments':[
                original.model_copy(update=updates) if v.id==original.id else v for v in s.assignments]}))
            status,_,value=await self.call('GET','/threads')
            self.assertEqual(status,403,(updates,value))
        self.f.workers.store.update(lambda s:s.model_copy(update={'assignments':[
            original if v.id==original.id else v for v in s.assignments]}))
        workspace=self.f.workspaces.store.workspace(original.execution_workspace_id)
        self.f.workspaces.store.update(lambda s:s.model_copy(update={'leases':[
            v.model_copy(update={'expires_at':1}) if v.id==workspace.lease_id else v for v in s.leases]}))
        status,_,value=await self.call('GET','/threads')
        self.assertEqual(status,403,value)

    async def test_default_worker_never_gains_governor_operations(self):
        _,reference=self.f.execution_profiles.resolve('repository-write',organization_id='local',workspace_id='default',project_id='home')
        original=self.assignment
        changed=original.model_copy(update={'execution_profile_id':'repository-write','execution_profile_definition':reference})
        self.f.workers.store.update(lambda s:s.model_copy(update={'assignments':[changed]}))
        status,_,value=await self.call('GET','/threads')
        self.assertEqual(status,403,value)
        catalog=self.service.public_assignment_capability(changed)
        self.assertFalse(any(v['id'].startswith('project.') for v in catalog['operations']))

    async def test_definition_supersession_revokes_old_assignment(self):
        self.publish(self.payload)
        status,_,value=await self.call('GET','/threads')
        self.assertEqual(status,403,value)

    async def test_child_profile_escalation_and_unknown_outcome_do_not_replay(self):
        for profile,revision in [('project-governor',1),('engineer',2)]:
            status,_,value=await self.call('POST','/threads',dict(profile_id=profile,profile_revision=revision,idempotency_key=profile))
            self.assertEqual(status,200,value)
            self.assertNotEqual(value['intent']['status'],'succeeded')
        self.manager.uncertain=True
        status,_,first=await self.call('POST','/threads',dict(profile_id='engineer',profile_revision=1,idempotency_key='unknown'))
        self.assertEqual(status,200,first)
        self.assertNotEqual(first['intent']['status'],'succeeded')
        count=self.manager.starts
        status,_,second=await self.call('POST','/threads',dict(profile_id='engineer',profile_revision=1,idempotency_key='unknown'))
        self.assertEqual(status,200,second)
        self.assertEqual(second['intent']['id'],first['intent']['id'])
        self.assertEqual(self.manager.starts,count)

    def replace_assignment(self, target):
        self.f.workers.store.update(lambda s: s.model_copy(update={'assignments':[
            target if v.id == target.id else v for v in s.assignments]}))

    async def test_foreign_canonical_binding_assignment_and_memory_never_leak(self):
        one,_ = await self.create()
        binding = self.bindings.get_by_thread(one, self.actor)
        original = self.f.workers.store.assignment(binding.assignment_id)
        for updates in ({'project_id':'foreign'}, {'organization_id':'foreign'},
                        {'resource_ids':('resource-foreign',)}, {'workspace_id':'foreign'}):
            self.replace_assignment(original.model_copy(update=updates))
            status,_,rows = await self.call('GET','/threads?limit=1',scope=False)
            self.assertEqual(status,403,rows)  # Explicit scope is mandatory.
            status,_,rows = await self.call('GET','/threads')
            self.assertEqual(status,200,rows)
            self.assertEqual(rows['count'],0)
            status,_,detail = await self.call('GET','/threads/'+one)
            self.assertIn(status,(403,404),detail)
            status,_,detail = await self.call('GET','/assignments/'+original.id)
            self.assertEqual(status,403,detail)
        self.replace_assignment(original)
        from codex_web.organizational_memory import KnowledgeCreate
        foreign = self.memory.create(KnowledgeCreate(logical_key='shared',object_type='project',
            title='Foreign memory',summary='PRIVATE_FOREIGN',content='PRIVATE_FOREIGN',
            provenance={'source_kind':'other','source_ref':'fixture'}),actor=self.actor)
        status,_,rows = await self.call('GET','/memory')
        self.assertEqual(status,200,rows)
        self.assertEqual(rows['count'],0)
        status,_,detail = await self.call('GET','/memory/'+foreign.id)
        self.assertEqual(status,403,detail)
        self.assertNotIn('PRIVATE_FOREIGN',json.dumps(detail))

    async def test_real_authority_denial_despite_operation_declaration(self):
        record = self.authority.catalog_record(actor=self.actor, project_id='home')
        self.registry.quarantine(record.record_id, actor='fixture', reason='Revoked authority catalog')
        status,_,value = await self.call('GET','/threads')
        self.assertEqual(status,403,value)
        self.assertEqual(self.manager.starts,0)

    async def test_revocation_during_native_await_retains_effect_without_replay(self):
        def revoke():
            self.replace_assignment(self.assignment.model_copy(update={'deadline_at':1}))
        self.manager.before_reply = revoke
        payload = dict(profile_id='engineer',profile_revision=1,idempotency_key='race')
        status,_,value = await self.call('POST','/threads',payload)
        self.assertEqual(status,403,value)
        self.assertEqual(self.manager.starts,1)
        self.replace_assignment(self.assignment)
        self.manager.before_reply = None
        status,_,value = await self.call('POST','/threads',payload)
        self.assertEqual(status,200,value)
        self.assertNotEqual(value['intent']['status'],'succeeded')
        self.assertEqual(self.manager.starts,1)

    async def test_concurrent_duplicate_creation_and_two_lane_bound(self):
        payload = dict(profile_id='engineer',profile_revision=1,idempotency_key='concurrent')
        results = await asyncio.gather(*(self.call('POST','/threads',payload) for _ in range(2)))
        self.assertTrue(all(v[0] == 200 for v in results),results)
        self.assertEqual(results[0][2]['intent']['id'],results[1][2]['intent']['id'])
        self.assertEqual(self.manager.starts,1)
        await self.create('scanner','second')
        status,_,value = await self.call('POST','/threads',dict(payload,idempotency_key='third'))
        self.assertEqual(status,200,value)
        self.assertNotEqual(value['intent']['status'],'succeeded')
        self.assertEqual(self.manager.starts,2)

    async def test_workspace_quota_and_maintenance_stop_before_native_creation(self):
        from codex_web.execution_workspaces import WorkspaceQuota
        self.intents.maintenance_guard = lambda item: False
        payload = dict(profile_id='engineer',profile_revision=1,idempotency_key='maintenance')
        status,_,value = await self.call('POST','/threads',payload)
        self.assertEqual(status,200,value)
        self.assertEqual(value['intent']['status'],'pending')
        self.assertEqual(self.manager.starts,0)
        self.intents.maintenance_guard = None
        self.f.workspaces.quota = WorkspaceQuota(max_requested_disk_bytes=1)
        status,_,value = await self.call('POST','/threads',dict(payload,idempotency_key='quota'))
        self.assertEqual(status,200,value)
        self.assertNotEqual(value['intent']['status'],'succeeded')
        self.assertEqual(self.manager.starts,0)

    async def test_drain_race_and_denial_feedback_are_truthful(self):
        self.f.workers.store.update(lambda s:s.model_copy(update={'workers':[
            v.model_copy(update={'lifecycle':'draining'}) if v.id == self.f.worker.id else v for v in s.workers]}))
        status,_,value = await self.call('POST','/threads',dict(profile_id='engineer',profile_revision=1,idempotency_key='drain'))
        self.assertEqual(status,403,value)
        self.assertEqual(self.manager.starts,0)
        status,_,value = await self.call('GET','/elevation')
        self.assertEqual(status,200,value)
        denial = next(v for v in value['items'] if v['kind'] == 'broker_denial')
        self.assertTrue(denial['correlation_id'])
        self.assertIn('do not retry',denial['next_action'])

    async def test_pagination_rejects_changed_snapshot_and_blank_scope_duplicates(self):
        await self.create()
        await self.create('scanner','second')
        path = '/threads?project_id=home&repository_resource_id='+self.f.repository.id+'&limit=1'
        status,_,value = await self.call('GET',path,scope=False)
        self.assertEqual(status,200,value)
        self.assertEqual(value['count'],2)
        self.assertTrue(value['next_cursor'])
        self.host.queues['native-1'] = [SimpleNamespace(id='changed')]
        status,_,value = await self.call('GET',path+'&cursor='+value['next_cursor'],scope=False)
        self.assertEqual(status,409,value)
        status,_,value = await self.call('GET',path+'&project_id=',scope=False)
        self.assertEqual(status,403,value)

    async def test_current_authority_approval_requirement_is_not_bypassed(self):
        import copy
        active = self.authority.catalog_record(actor=self.actor, project_id='home')
        payload = copy.deepcopy(active.payload)
        payload['roles'][0]['grants'][0]['approvals']['count'] = 1
        record = self.registry.create_draft(DefinitionDraftCreate(definition_id=active.definition_id,
            kind=active.kind,definition_schema_version=active.definition_schema_version,
            scope_type=active.scope_type,scope_id=active.scope_id,payload=payload,actor='fixture'))
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))
        status,_,value = await self.call('POST','/threads',dict(profile_id='engineer',profile_revision=1,idempotency_key='approval'))
        self.assertEqual(status,403,value)
        self.assertIn('approval',json.dumps(value))
        self.assertEqual(self.manager.starts,0)

class CoordinationSchemaCompatibilityTests(unittest.TestCase):
    def test_opt_in_schema_cannot_widen_legacy_or_named_profiles(self):
        from codex_web.execution_profiles import (validate_execution_profile_catalog,
            validate_administrative_execution_profile_catalog, validate_coordination_execution_profile_catalog)
        payload = execution_profile_catalog_seed_payload()
        self.assertEqual(validate_coordination_execution_profile_catalog(payload),payload)
        declaration = {'repository_resource_id':'resource-repository','child_profiles':[{'profile_id':'engineer','revision':1}]}
        payload['profiles'].append(dict(id='project-governor',name='Governor',description='Opt-in',
            workspace_mode='repository',repository_access='mutable',required_worker_capabilities=['git','command_execution'],
            control_plane_operations=['project.threads.list'],project_coordination=declaration))
        for validate in (validate_execution_profile_catalog,validate_administrative_execution_profile_catalog):
            with self.assertRaises(ValueError): validate(payload)
        validate_coordination_execution_profile_catalog(payload)
        payload['profiles'][-1]['id'] = 'repository-write'
        with self.assertRaises(ValueError): validate_coordination_execution_profile_catalog(payload)
