"""Private fixture composition; no production application import or SDK process.

Only native JSON-RPC transport is synthetic. Ordinary API/thread scope, current
session identity, authority, TurnService, retained queue/preflight, real binding/
workspace/worker claims, credential metadata/delegation and native capture run.
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from codex_web.api.authorization import install_api_authorization
from codex_web.api.turns import build_turns_router
from codex_web.agent_runtime import AgentSession
from codex_web.configuration import ConfigurationDraftCreate,ConfigurationPublishRequest,SecretReference
from codex_web.execution_workers import AssignmentClaimRequest,AssignmentStartRequest
from codex_web.models import QueuedTurn
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.agent_runtime import AgentRuntimeRegistry,AgentSessionService
from codex_web.services.codex_auth_delegation import CodexAuthDelegationService
from codex_web.services.codex_worker_configuration import install_codex_worker_configuration,CODEX_WORKER_ACCESS_TOKEN_CONFIG
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.projects import ProjectService
from codex_web.services.secrets import SecretBroker
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.turns import TurnService
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionStaleError
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.runtime.execution import TurnExecutionService
from tests import test_turn_execution as native_fixture


class NativeContextHarness:
    def __init__(self,case,f,*,items,profiles,pin,bindings,requester,worker_actor,ref,thread_id='same-thread'):
        self.case,self.f,self.items,self.pin=case,f,items,pin
        self.requester,self.worker_actor,self.ref,self.thread_id=requester,worker_actor,ref,thread_id
        root=Path(f.temp.name)
        self.secrets=SecretBroker(SecretStateStore(f.sqlite),{'local':LocalFileSecretBackend(root/'synthetic-runtime-material')})
        credential=self.secrets.create(SecretCreate(name='Synthetic bounded native credential',
            value='private-fixture-not-a-real-provider-token',provider='openai',purpose='codex_access_token',
            allowed_identity_ids=[worker_actor.identity_id],expires_at=time.time()+120),actor=f.actor)
        install_codex_worker_configuration(f.service.configuration)
        draft=f.service.configuration.create_draft(ConfigurationDraftCreate(key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            scope_type='project',scope_id=f.project.id,value=SecretReference(secret_id=credential.id),actor=f.actor.identity_id))
        f.service.configuration.publish(draft.id,ConfigurationPublishRequest(actor=f.actor.identity_id))
        f.service.secrets=self.secrets
        self.credential_provider=CodexAuthDelegationService(self.secrets)
        self.sessions={};self.started_assignments=[]
        harness=self
        class Session(native_fixture._Session):
            def __init__(self,assignment):
                super().__init__();self.assignment=assignment;self.pinned=assignment.model_copy(deep=True)
                self.workspace_path=Path(f.workspaces.get(assignment.execution_workspace_id,f.actor).path)
                self.credential_provider=harness.credential_provider
                self.delegation=self.credential_provider.issue(assignment,worker_id=f.worker.id,
                    fence=assignment.fence,actor=worker_actor)
            def validate_current(self):
                current=f.workers.store.assignment(self.pinned.id)
                if (current is None or current.status!='running' or current.fence!=self.pinned.fence
                        or current.assigned_worker_id!=f.worker.id or current.resource_context!=self.pinned.resource_context
                        or current.lease is None or current.lease.expires_at<=time.time()):
                    raise AssignmentBoundAgentProcessSessionStaleError('Actual private fixture lease/fence changed')
                f.service.validate_assignment_resource_context(current)
                self.credential_provider.validate_current(self.delegation,current,actor=worker_actor)
                return current
            def _validate_current_authority(self):return self.validate_current()
            def _lease_budget(self):return 5
            async def _supervise(self,phase,fn,**kwargs):return await asyncio.to_thread(fn)
            def status(self):return SimpleNamespace(worker_id=f.worker.id,fence=self.pinned.fence,running=True)
        class Manager:
            def get(self,assignment_id):return harness.sessions.get(assignment_id)
            async def start(self,assignment_id):
                case.assertIsNone(f.workers.store.assignment(assignment_id).lease)
                claim=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=worker_actor,assignment_id=assignment_id)
                actual=f.workers.start(f.worker.id,claim.id,AssignmentStartRequest(fence=claim.fence,
                    lease_token=claim.lease.lease_token),actor=worker_actor)
                harness.started_assignments.append(actual.id)
                session=Session(actual);harness.sessions[actual.id]=session
                return session
        manager=Manager()
        manager.local_worker=SimpleNamespace(_pending_assignment=lambda aid:f.workers.store.assignment(aid),
            worker_service=f.workers,control_actor=f.actor)
        self.manager=manager
        host=native_fixture._Host();host._project=lambda _:f.project
        host.work_item_states=items.load()
        self.host=host
        settings={}
        settings_service=ThreadExecutionSettingsService(load_settings=lambda:settings,
            save_settings=lambda values:settings.update(values),bindings=SimpleNamespace(for_thread=lambda _:[]),
            load_bindings=lambda:[],save_bindings=lambda _:None,gitlab_routing_enabled_for_project=lambda _:False,
            binding_report_name=lambda _:None,binding_prefix=lambda _:None)
        host._thread_run_settings=settings_service.get;host._remember_thread_run_settings=settings_service.remember
        host._truncate_text=lambda value,limit:value[:limit]
        preflight=ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        runtime=TurnExecutionService(host,binding_service=f.service,session_manager=manager,bootstrap_bindings=bindings,
            control_actor=f.actor,actor_resolver=lambda identity,project:f.identity.actor_for_identity(identity,scope=requester.tenant))
        runtime.queued_preflight=preflight;runtime.schedule_queue_drain=lambda _:None
        self.runtime,self.preflight=runtime,preflight
        service=TurnService(projects=f.projects,settings=settings_service,
            recovery=SimpleNamespace(raise_if_thread_replaced=lambda _:None,release_stale_active_turn=lambda *_:None),
            resume_runtime=SimpleNamespace(),bindings=SimpleNamespace(for_thread=lambda _:[]),
            queue_policy=SimpleNamespace(depth=lambda t:len(host._thread_queue(t))),execution=runtime,
            event_sink=host._append_bot_event,truncate_text=lambda value,limit:value[:limit],
            binding_public=lambda x:x.model_dump(),preflight=preflight,agent_profiles=profiles)
        self.web=service
        sessions=AgentSessionService(AgentSessionStore(f.sqlite),AgentRuntimeRegistry())
        self.native_session=sessions.store.upsert(AgentSession(organization_id=f.project.organization_id,
            workspace_id=f.project.workspace_id,project_id=f.project.id,provider_id='openai',runtime_id='codex',
            runtime_type='app-server',provider_native_session_id=thread_id))
        self.session_store=sessions.store
        scope=ThreadScopeService(ProjectService(SimpleNamespace(load=lambda:[f.project])),sessions,
            ThreadIndexRepository(f.sqlite,root/'synthetic-thread-index.json'))
        app=FastAPI();app.include_router(build_turns_router(service,scope))
        api=install_api_authorization(app,f.service.resource_context_admission.authority)
        credentials=f.identity.create_session(identity_id=requester.identity_id,scope=requester.tenant,assurance=requester.assurance)
        # Synthetic credentials stay in this in-memory/private fixture only.
        @app.middleware('http')
        async def actual_session(request,call_next):
            authenticated=f.identity.authenticate_session(request.headers['x-private-fixture-session'],touch=False)
            request.state.identity_actor=authenticated.actor;request.state.tenant_scope=authenticated.actor.tenant
            api.authorize_request(request,authenticated.actor)
            return await call_next(request)
        self.app,self.session_token=app,credentials.session_token
        # Unrelated original held input and its attempts stay exact; the tested
        # new task never consumes/repurposes it or mutates its immutable snapshot.
        held=QueuedTurn(id='held-original',thread_id='held-thread',project_id=f.project.id,message='unknown original',
            created_at=1.,attempts=3,execution_id='unknown-original')
        host.queues['held-thread']=[held];self.held=held.model_dump(mode='json')

    async def submit(self):
        self.host.work_item_states=self.items.load()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic') as client:
            return await client.post('/api/threads/'+self.thread_id+'/turns/resource-context-v1',
                headers={'x-private-fixture-session':self.session_token},json={'message':'One distinct admitted original',
                 'project_id':self.f.project.id,'repository_resource_id':self.f.repository.id,
                 'read_only_resource_ids':[self.f.database.id],'resource_context_work_item_ref':self.ref,
                 'agent_profile_id':self.pin.profile_id,'agent_profile_revision':self.pin.profile_revision,'defer_start':True})

    async def finish_one(self):
        response=await self.submit();self.case.assertEqual(response.status_code,200,response.text)
        self.case.assertTrue(response.json()['queued'])
        self.case.assertEqual(len(self.host._thread_queue(self.thread_id)),1)
        original=self.host._thread_queue(self.thread_id)[0]
        self.case.assertEqual(original.resource_context.requester_identity_id,self.requester.identity_id)
        await self.runtime.drain_thread_queue(self.thread_id)
        self.case.assertEqual(len(self.started_assignments),1)
        assignment=self.f.workers.store.assignment(self.started_assignments[0])
        session=self.sessions[assignment.id]
        starts=[params for method,params in session.requests if method=='turn/start']
        self.case.assertEqual(len(starts),1)
        self.case.assertEqual(starts[0]['threadId'],self.thread_id)
        self.case.assertEqual(len(self.host._thread_queue(self.thread_id)),0)
        self.case.assertEqual(self.host.queues['held-thread'][0].model_dump(mode='json'),self.held)
        self.case.assertEqual(assignment.resource_context,original.resource_context)
        self.case.assertNotEqual(assignment.created_by,self.requester.identity_id)
        return assignment,session


def create_requester(case,identity,registry,admin,project,resource_ids):
    """Private fixture policy publication; never a runtime grant/fallback."""
    from codex_web.identity import HumanUserCreate
    from codex_web.authority import (AUTHORITY_ROLE_CATALOG_ID,AUTHORITY_ROLE_CATALOG_KIND,
        AuthorityRoleCatalogDefinition,AuthorityRoleDefinition,AuthorityRoleBinding,AuthorityGrant)
    from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
    human,_=identity.create_human_user(HumanUserCreate(display_name='Synthetic original requester A'),actor=admin)
    actor=identity.actor_for_identity(human.id,scope=admin.tenant)
    active=registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
    catalog=AuthorityRoleCatalogDefinition.model_validate(active.payload)
    role=AuthorityRoleDefinition(id='private-context-requester',name='Private scoped requester',
        description='Private fixture requester authority for admitted inputs and original capacity evidence.',
        grants=(AuthorityGrant(id='requester.thread-input',capability='api.threads.write',level='execute',
            project_ids=(project.id,)),*tuple(AuthorityGrant(id='requester.'+str(i),capability=capability,level=level,
            project_ids=(project.id,),resource_ids=tuple(resource_ids)) for i,(capability,level) in enumerate((
                ('resource.read','read'),('api.verifications.write','execute'),('api.verifications.read','read'),
                ('api.evidence.read','read'),('api.artifacts.read','read'))))))
    catalog=catalog.model_copy(update={'roles':(*catalog.roles,role),'bindings':(*catalog.bindings,
        AuthorityRoleBinding(id='private-context-requester-binding',role_id=role.id,subject_kind='identity',
            subject_id=actor.identity_id,organization_id=admin.organization_id,workspace_id=admin.workspace_id))})
    draft=registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND,
        definition_schema_version='1.0',payload=catalog.model_dump(mode='json'),actor=admin.identity_id,
        reason='Private requester authority fixture'))
    registry.approve_publication(draft.record_id,actor='fixture-approver',reference='synthetic-only',reason='Fixture approval')
    registry.publish(draft.record_id,DefinitionPublishRequest(actor=admin.identity_id,reason='Private fixture publication',
        expected_active_revision=active.revision))
    case.assertNotEqual(actor.identity_id,admin.identity_id)
    return actor
