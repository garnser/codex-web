"""Actual canonical GitHub projection/reconcile -> handoff/ACK -> native scope.

Only GitHub/native transport and dispatch delivery are synthetic. Resources,
Project bindings, keyed WorkItems, worker identities/claims, Role Definitions,
procedure admission, leases/fences and bootstrap/native capture are real services
in a fresh private StateStore. No production data or credentials are used.
"""
import copy
import httpx
from fastapi import FastAPI,HTTPException
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from codex_web.agent_profiles import AgentProfileCreate,AgentProfileExecutionBinding
from codex_web.authority import (AUTHORITY_ROLE_CATALOG_ID,AUTHORITY_ROLE_CATALOG_KIND,
    AuthorityRoleCatalogDefinition,AuthorityRoleDefinition,AuthorityGrant,AuthorityRoleBinding)
from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
from codex_web.execution_workers import AssignmentCancelRequest,AssignmentClaimRequest,AssignmentStartRequest
from codex_web.models import TaskSourceConfiguration,GitHubTaskSourceSettings
from codex_web.services.control_plane_broker import ControlPlaneBrokerService,AssignmentBoundControlPlaneBroker
from codex_web.services.github_task_source import GitHubTaskSource
from codex_web.services.procedure_discovery import BrokerProcedureAdmissionService,ProcedureDiscoveryService
from codex_web.services.skills import SkillService
from codex_web.services.task_source_runtime import TaskSourceRegistry
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_items import WorkItemService
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from tests import test_read_only_resource_context as fixtures
from tests import test_turn_execution_binding as binding_fixture
from tests.test_recovery_scheduled_postgres import _PostgresFixture


class _GitHubTransport:
    def __init__(self):
        self.issue_value={'number':1530,'title':'Synthetic preservation scope','state':'open',
            'repository':{'full_name':'fixture/reconciled'},'labels':[],
            'assignees':[{'login':'fixture-dev'}],'updated_at':'2026-10-18T00:00:00Z'}
        self.reads=0
    async def issue(self,api,repo,number,*,token):
        assert repo=='fixture/reconciled' and number==1530
        self.reads+=1
        return copy.deepcopy(self.issue_value)
    async def request_empty(self,method,api,path,*,token):
        assert method=='GET' and '/assignees/' in path
    async def request_json(self,method,api,path,*,token,json_body):
        assert method=='POST' and path=='repos/fixture/reconciled/issues/1530/assignees'
        for login in json_body['assignees']:
            if not any(a['login']==login for a in self.issue_value['assignees']):
                self.issue_value['assignees'].append({'login':login})
        return copy.deepcopy(self.issue_value)


class ReconciledReadContextTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.ReadOnlyResourceContextTests.setUp
    prepare=fixtures.ReadOnlyResourceContextTests.prepare

    def _setup_source(self):
        f=self.f
        self.ref='fixture/reconciled#1530'
        # New Project begins with ONLY its real repository binding. The DB is
        # not bound here until the explicit canonical bind/reconcile step below.
        project=f.project.model_copy(update={'id':'reconciled-project',
            'authoritative_task_source':TaskSourceConfiguration(source_type='github',
                source_instance='https://api.github.com',scope='fixture/reconciled')})
        f.project=project;f.projects.project=project
        f._publish_secret(scope_id=project.id)
        f.resources.bind_project(project=project,resource_id=f.repository.id,actor=f.actor)
        reviewer=self.profiles.create(AgentProfileCreate(profile_id='codex-web-vp-engineering',name='Synthetic current VP',
            execution_profile_id='repository-write'),actor=f.actor)
        self.profile=reviewer
        self.pin=AgentProfileExecutionBinding(profile_id=reviewer.profile_id,profile_revision=reviewer.revision,
            profile_record_id=reviewer.record_id,execution_profile_id=reviewer.execution_profile_id)
        developer=self.profiles.create(AgentProfileCreate(profile_id='context-developer',name='Synthetic Developer',
            execution_profile_id='repository-write'),actor=f.actor)
        self.developer_pin=AgentProfileExecutionBinding(profile_id=developer.profile_id,
            profile_revision=developer.revision,profile_record_id=developer.record_id,
            execution_profile_id=developer.execution_profile_id)
        self.context=self.context.model_copy(update={'work_item_ref':self.ref})
        root=Path(f.temp.name)
        host=SimpleNamespace(DATA_DIR=root,WORK_ITEM_EVENTS_FILE=root/'never-private-events.jsonl',
            _load_work_item_states=self.items.load,_save_work_item_states=self.items.save,
            _get_work_item_state_record=self.items.get,
            _put_work_item_state_record=lambda item:self.items.put(item.ref,item),
            _load_projects=lambda:[project],
            _resource_ids_for_project=lambda project_id,**kw:f.resources.resource_ids_for_project(project,**kw))
        dependencies=WorkItemRuntimeDependencies.from_host(host)
        machine=WorkItemStateMachine(store=f.sqlite,dependencies=dependencies)
        transport=_GitHubTransport()
        source=GitHubTaskSource('https://api.github.com','synthetic-fixture-only',client=transport,
            settings=GitHubTaskSourceSettings(assignee_mapping={developer.profile_id:'fixture-dev',
                self.profile.profile_id:'fixture-vp'}))
        registry=TaskSourceRegistry();registry.register('github',lambda state:source)
        registry.register_project('github',lambda *args:source)
        projector=TaskSourceWorkItemProjector(None,machine,dependencies=dependencies)
        initial=projector.upsert(source,source._snapshot(transport.issue_value),project_id=project.id)
        self.assertEqual(initial.resource_ids,[f.repository.id])
        self.assertEqual(initial.current_owner,developer.profile_id)
        self.assertNotIn(self.database.id,initial.resource_ids)
        dispatches=[]
        def record(*args,**kwargs):dispatches.append((args,kwargs))
        continuity=SimpleNamespace(schedule_structured_handoff_dispatch=record,
            schedule_handoff_continuity_check=record,schedule_actionable_owner_dispatch=record,
            schedule_actionable_owner_continuity_check=record)
        work_items=WorkItemService(host,state_machine=machine,task_source_registry=registry,
            task_source_projector=projector,work_item_dependencies=dependencies,continuity=continuity,
            recovery=SimpleNamespace(schedule=record))
        self.machine,self.work_items,self.transport=machine,work_items,transport
        # Fixture-only canonical Role publication for the real worker principal;
        # no production grant/catalog or principal mutation is performed.
        worker_actor=f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,
            name='Private existing worker',scope=f.actor.tenant,service_scopes=('execution-worker:run','secret:use'))
        active=f.execution_profiles.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        catalog=AuthorityRoleCatalogDefinition.model_validate(active.payload)
        role=AuthorityRoleDefinition(id='synthetic-context-coordination',name='Synthetic context coordination',
            description='Private fixture authority for canonical handoff and acknowledgement.',
            grants=tuple(AuthorityGrant(id='context.'+name,capability='work_item.'+name,level='execute',
                project_ids=(project.id,)) for name in ('handoff','acknowledge')))
        catalog=catalog.model_copy(update={'roles':(*catalog.roles,role),'bindings':(*catalog.bindings,
            AuthorityRoleBinding(id='synthetic-current-worker',role_id=role.id,subject_kind='identity',
                subject_id=worker_actor.identity_id,organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id))})
        draft=f.execution_profiles.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version='1.0',payload=catalog.model_dump(mode='json'),
            actor=f.actor.identity_id,reason='Private coordination fixture authority'))
        f.execution_profiles.registry.approve_publication(draft.record_id,actor='fixture-approver',
            reference='synthetic-only',reason='Private fixture publication')
        f.execution_profiles.registry.publish(draft.record_id,DefinitionPublishRequest(actor=f.actor.identity_id,
            reason='Private fixture publication',expected_active_revision=active.revision))
        self.worker_actor=worker_actor
        broker=ControlPlaneBrokerService(identity=f.identity,authority=self.authority,work_items=work_items,
            audit=ControlPlaneBrokerAuditStore(f.sqlite),execution_workspaces=f.workspaces,execution_workers=f.workers)
        broker.procedure_admission=BrokerProcedureAdmissionService(
            ProcedureDiscoveryService(SkillService(f.execution_profiles.registry),f.resources),f.workers,f.actor)
        self.broker=broker

    def _claim(self,binding):
        f=self.f
        claim=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=binding.assignment_id)
        return f.workers.start(f.worker.id,claim.id,AssignmentStartRequest(fence=claim.fence,
            lease_token=claim.lease.lease_token),actor=self.worker_actor)

    async def _dispatch(self,assignment,operation,payload):
        proxy=AssignmentBoundControlPlaneBroker(self.broker,assignment=assignment,worker_id=self.f.worker.id,
            worker_actor=self.worker_actor,fence=assignment.fence,
            validator=lambda:self.f.workers.assignment_for_execution(assignment.execution_id,actor=self.f.actor))
        # Real proxy/fence admission and canonical dispatch; no socket/process or
        # manually adopted assignment is needed by this synthetic transport.
        current=proxy._validate_current()
        return await self.broker.dispatch(assignment=current,worker_actor=self.worker_actor,method='POST',
            raw_target='/api/work-items/'+quote(self.ref,safe='')+'/'+operation,
            body=json.dumps(payload).encode(),assignment_validator=proxy._validate_current)

    async def test_github_reconcile_handoff_ack_then_same_thread_read_admission(self):
        from codex_web.services.turn_execution_binding import TurnExecutionBindingError
        self._setup_source();f=self.f
        developer=f.service.prepare_bootstrap(bootstrap_id='synthetic-developer',execution_id='synthetic-developer-exec',
            project_id=f.project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=f.repository.id,agent_profile=self.developer_pin)
        developer_assignment=self._claim(developer)
        self.assertEqual(developer_assignment.agent_profile.profile_id,'context-developer')
        _,handoff,*_=await self._dispatch(developer_assignment,'handoff',
            {'from_agent':developer_assignment.agent_profile.profile_id,'to_agent':self.profile.profile_id,
             'reason':'Bounded capacity-review custody','expected_action':'Read original synthetic capacity facts'})
        pending=self.items.get(self.ref)
        self.assertEqual(pending.current_owner,'context-developer')
        self.assertEqual(pending.handoff.status,'pending')
        self.assertEqual(pending.source_projection.status,'succeeded')
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        f.workers.cancel_bootstrap(developer.assignment_id,AssignmentCancelRequest(
            expected_fence=developer_assignment.fence,reason='Private terminal sender custody'),actor=f.actor)
        reviewer=f.service.prepare_bootstrap(bootstrap_id='synthetic-reviewer',execution_id='synthetic-reviewer-exec',
            project_id=f.project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=f.repository.id,agent_profile=self.pin)
        reviewer_assignment=self._claim(reviewer)
        self.assertEqual(reviewer_assignment.agent_profile.profile_id,self.profile.profile_id)
        for actor,timestamp in (('quinn',pending.handoff.requested_at),
                (self.profile.profile_id,pending.handoff.requested_at-1)):
            with self.assertRaises(HTTPException) as denied:
                await self._dispatch(reviewer_assignment,'ack',{'actor':actor,
                    'expected_handoff_requested_at':timestamp,'accepted':True})
            self.assertEqual(getattr(denied.exception,'status_code',None),409)
            self.assertEqual(self.items.get(self.ref).handoff.status,'pending')
        await self._dispatch(reviewer_assignment,'ack',{'actor':reviewer_assignment.agent_profile.profile_id,
            'expected_handoff_requested_at':pending.handoff.requested_at,'accepted':True})
        accepted=self.items.get(self.ref)
        self.assertEqual(accepted.current_owner,self.profile.profile_id)
        self.assertEqual(accepted.validation_owner,'quinn')
        self.assertEqual(accepted.handoff.status,'accepted')
        self.assertEqual(accepted.source_projection.status,'succeeded')
        # Optional supported source reconciliation materializes flat membership too.
        # It is not required for the independently admitted immutable context.
        f.resources.bind_project(project=f.project,resource_id=self.database.id,actor=f.actor)
        self.assertNotIn(self.database.id,self.items.get(self.ref).resource_ids)
        before=accepted.handoff.model_dump(mode='json')
        from codex_web.api.work_items import build_work_items_router
        from codex_web.api.authorization import install_api_authorization
        app=FastAPI()
        app.include_router(build_work_items_router(self.work_items))
        api_authority=install_api_authorization(app,self.authority)
        @app.middleware('http')
        async def current_operator(request,call_next):
            # Actual private-fixture identity/current canonical authority; no
            # caller-selected actor can substitute for this request principal.
            actor=f.identity.actor_for_identity(f.actor.identity_id,scope=f.actor.tenant)
            request.state.identity_actor=actor
            request.state.tenant_scope=actor.tenant
            api_authority.authorize_request(request,actor)
            return await call_next(request)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as client:
            response=await client.post('/api/work-items/'+quote(self.ref,safe='')+'/reconcile',
                json={'reason':'Refresh exact canonical source and real Project Resource membership',
                      'actor':'ignored-caller-attribution'})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        current=self.items.get(self.ref)
        self.assertIn(self.database.id,current.resource_ids)
        self.assertEqual(current.source_identity.external_id,self.ref)
        self.assertEqual(current.handoff.model_dump(mode='json'),before)
        self.assertEqual(current.current_owner,self.profile.profile_id)
        self.assertGreater(self.transport.reads,0)
        self.assertEqual(result['item']['ref'],self.ref)
        self.assertCountEqual(current.resource_ids,[f.repository.id,self.database.id])
        event=self.machine.event_store.load()[-1]
        self.assertEqual(event.event_type,'operator_reconciled')
        self.assertEqual(event.actor,f.actor.identity_id)
        self.bindings.bind(bootstrap_id='synthetic-reviewer',thread_id='same-thread',execution_id=reviewer.execution_id,
            assignment_id=reviewer.assignment_id,execution_workspace_id=reviewer.workspace_id,actor=f.actor)
        terminal=f.workers.cancel_bootstrap(reviewer.assignment_id,AssignmentCancelRequest(
            expected_fence=reviewer_assignment.fence,reason='Owned terminal scope rollover'),actor=f.actor)
        fresh=self.prepare()
        self.bindings.rebind(bootstrap_id='bootstrap-context-execution',thread_id='same-thread',execution_id=fresh.execution_id,
            assignment_id=fresh.assignment_id,execution_workspace_id=fresh.workspace_id,actor=f.actor,
            expected_assignment_id=reviewer.assignment_id,expected_assignment_fence=terminal.fence)
        self.composed_binding=fresh
        await fixtures.ReadOnlyResourceContextTests.test_turn_queue_preflight_and_canonical_native_assignment_scope(self)
        actual=f.workers.store.assignment(fresh.assignment_id)
        self.assertEqual(actual.resource_context,self.context)
        self.assertEqual(self.bindings.get_by_thread('same-thread',f.actor).assignment_id,fresh.assignment_id)
        self.assertEqual(self.items.get(self.ref).current_owner,self.profile.profile_id)
        self.assertEqual(self.items.get(self.ref).validation_owner,'quinn')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated PostgreSQL DSN required')
class PostgresReconciledReadContextTests(_PostgresFixture,ReconciledReadContextTests):
    def setUp(self):
        with patch.object(binding_fixture,'SQLiteStateStore',self.state_store_factory):
            super().setUp()


class FreshPublicReadContextTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.ReadOnlyResourceContextTests.setUp
    prepare=fixtures.ReadOnlyResourceContextTests.prepare
    _setup_source=ReconciledReadContextTests._setup_source
    _claim=ReconciledReadContextTests._claim
    _dispatch=ReconciledReadContextTests._dispatch

    async def test_fresh_public_route_after_genuine_ack_and_terminal_repo_binding(self):
        from codex_web.services.turn_execution_binding import TurnExecutionBindingError
        from tests.read_only_resource_context_harness import NativeContextHarness,create_requester
        self._setup_source();f=self.f;f.database=self.database
        f.resources.bind_project(project=f.project,resource_id=self.database.id,actor=f.actor)
        requester=create_requester(self,f.identity,f.execution_profiles.registry,f.actor,f.project,
            (f.repository.id,self.database.id))
        developer=f.service.prepare_bootstrap(bootstrap_id='fresh-dev',execution_id='fresh-dev-exec',
            project_id=f.project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=f.repository.id,agent_profile=self.developer_pin)
        dev=self._claim(developer)
        await self._dispatch(dev,'handoff',{'from_agent':dev.agent_profile.profile_id,
            'to_agent':'codex-web-vp-engineering','reason':'Private actual bounded VP recipient task'})
        pending=self.items.get(self.ref)
        harness=NativeContextHarness(self,f,items=self.items,profiles=self.profiles,pin=self.pin,bindings=self.bindings,
            requester=requester,worker_actor=self.worker_actor,ref=self.ref)
        denied=await harness.submit()
        self.assertEqual(denied.status_code,409)
        self.assertEqual(harness.started_assignments,[])
        self.assertEqual(harness.host._thread_queue('same-thread'),[])
        f.workers.cancel_bootstrap(dev.id,AssignmentCancelRequest(expected_fence=dev.fence,
            reason='Private terminal sender custody'),actor=f.actor)
        reviewer=f.service.prepare_bootstrap(bootstrap_id='fresh-vp-old',execution_id='fresh-vp-old-exec',
            project_id=f.project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=f.repository.id,agent_profile=self.pin)
        vp=self._claim(reviewer)
        self.assertNotIn(self.database.id,vp.resource_ids)
        await self._dispatch(vp,'ack',{'actor':vp.agent_profile.profile_id,
            'expected_handoff_requested_at':pending.handoff.requested_at,'accepted':True})
        self.assertEqual(self.items.get(self.ref).current_owner,'codex-web-vp-engineering')
        self.assertNotIn(self.database.id,self.items.get(self.ref).resource_ids)
        self.bindings.bind(bootstrap_id='fresh-vp-old',thread_id='same-thread',execution_id=reviewer.execution_id,
            assignment_id=reviewer.assignment_id,execution_workspace_id=reviewer.workspace_id,actor=f.actor)
        terminal=f.workers.cancel_bootstrap(vp.id,AssignmentCancelRequest(expected_fence=vp.fence,
            reason='Private terminal repo-only scope rollover'),actor=f.actor)
        before=[a.id for a in f.workers.store.load().assignments if a.resource_context is not None]
        self.assertEqual(before,[])
        actual,session=await harness.finish_one()
        self.assertNotEqual(actual.id,vp.id)
        self.assertEqual(actual.resource_context.requester_identity_id,requester.identity_id)
        self.assertEqual(actual.agent_profile.profile_id,'codex-web-vp-engineering')
        workspace=f.workspaces.get(actual.execution_workspace_id,f.actor)
        lease=f.workspaces.store.lease(workspace.lease_id)
        self.assertEqual(lease.resource_modes[self.database.id],'read')
        self.assertEqual(lease.resource_modes[f.repository.id],'write')
        self.assertEqual(f.workers.store.assignment(vp.id).fence,terminal.fence)
        self.assertNotIn(self.database.id,self.items.get(self.ref).resource_ids)
        self.assertEqual(self.items.get(self.ref).validation_owner,'quinn')
        self.assertEqual(session.validate_current(),actual)


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated PostgreSQL DSN required')
class PostgresFreshPublicReadContextTests(_PostgresFixture,FreshPublicReadContextTests):
    def setUp(self):
        with patch.object(binding_fixture,'SQLiteStateStore',self.state_store_factory):
            super().setUp()
