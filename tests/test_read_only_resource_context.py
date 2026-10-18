"""Synthetic composed ordinary input; canonical identity/catalog/lease/assignment.

Only repository provisioning and native JSON-RPC transport are fixture I/O.
No production data, SDK install, credentials, DB mounts or capacity proof.
"""
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from codex_web.agent_profiles import AgentProfileCreate,AgentProfileExecutionBinding
from codex_web.execution_workers import (AssignmentClaimRequest,
    AssignmentStartRequest,AssignmentCancelRequest)
from codex_web.models import Project,TurnCreate,ThreadRunSettings,WorkItemState,QueuedTurn
from codex_web.resource_context import ReadOnlyResourceContext
from codex_web.resources import ResourceCreate,ResourceUpdate
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.read_only_resource_context import ReadOnlyResourceContextAdmission
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.turns import TurnService
from codex_web.services.turn_execution_binding import TurnExecutionBindingError
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.runtime.execution import TurnExecutionService
from tests import test_turn_execution_binding as binding_fixture
from tests import test_turn_execution as runtime_fixture


class ReadOnlyResourceContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=binding_fixture.TurnExecutionBindingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f=self.fixture
        f.service._clock=time.time
        self.f=f
        f._publish_secret()
        self.authority=install_authority_roles(f.execution_profiles.registry)
        self.profiles=AgentProfileService(AgentProfileStore(f.sqlite),definitions=f.execution_profiles.registry,
            authority=self.authority,execution_profiles=f.execution_profiles)
        self.profile=self.profiles.create(AgentProfileCreate(profile_id='context-reviewer',name='Context reviewer',
            execution_profile_id='repository-write'),actor=f.actor)
        self.pin=AgentProfileExecutionBinding(profile_id=self.profile.profile_id,
            profile_revision=self.profile.revision,profile_record_id=self.profile.record_id,
            execution_profile_id=self.profile.execution_profile_id)
        self.database=f.resources.create(ResourceCreate(resource_type='database',name='Synthetic metadata context'),actor=f.actor)
        f.resources.bind_project(project=f.project,resource_id=self.database.id,actor=f.actor)
        self.ref='fixture/source#1530'
        self.items=ModelMapRepository(f.sqlite,namespace='resource_context_fixture_items',
            legacy_path=Path(f.temp.name)/'never-created.json',model=WorkItemState)
        now=time.time()
        self.items.put(self.ref,WorkItemState(ref=self.ref,project_id=f.project.id,project_path='fixture/source',
            organization_id=f.project.organization_id,workspace_id=f.project.workspace_id,
            current_owner=self.profile.profile_id,current_stage='implementation_active',resource_ids=[f.repository.id,self.database.id],
            created_at=now,updated_at=now,last_meaningful_update_at=now))
        f.service.resource_context_admission=ReadOnlyResourceContextAdmission(f.identity,self.authority,
            f.resources,self.items.get,self.profiles)
        self.context=ReadOnlyResourceContext(schema_version='1.0',read_only_resource_ids=(self.database.id,),
            requester_identity_id=f.actor.identity_id,work_item_ref=self.ref)
        worker_before=f.worker
        f.worker=f.workers.ensure_local_worker(service_identity_id=worker_before.service_identity_id,
            version=worker_before.version,capabilities=worker_before.capabilities,
            supported_execution_contract_versions=('1.0','thread-turn/1.0','thread-bootstrap/1.0',
                'thread-turn/1.1','thread-bootstrap/1.1'),max_concurrency=worker_before.max_concurrency,
            supported_sandbox_profiles=worker_before.supported_sandbox_profiles,actor=f.actor)
        self.assertEqual(f.worker.id,worker_before.id)
        self.assertEqual(f.worker.pool,worker_before.pool)
        self.assertCountEqual(f.worker.capabilities,worker_before.capabilities)
        self.assertEqual(f.worker.max_concurrency,worker_before.max_concurrency)
        self.bindings=ThreadBootstrapBindingService(ThreadBootstrapBindingStore(f.sqlite))
        self.bindings.assignment_store=f.workers.store

    def prepare(self, execution='context-execution', context=None):
        return self.f.service.prepare_bootstrap(bootstrap_id='bootstrap-'+execution,execution_id=execution,
            project_id=self.f.project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=self.f.repository.id,agent_profile=self.pin,
            resource_context=self.context if context is None else context)

    async def test_real_bootstrap_mixed_read_write_and_immutable_reuse(self):
        binding=self.prepare()
        assignment=self.f.workers.store.assignment(binding.assignment_id)
        workspace=self.f.workspaces.get(binding.workspace_id,self.f.actor)
        lease=self.f.workspaces.store.lease(workspace.lease_id)
        self.assertEqual(assignment.resource_context,self.context)
        self.assertEqual(assignment.execution_contract_version,'thread-bootstrap/1.1')
        self.assertEqual(lease.resource_modes[self.database.id],'read')
        self.assertEqual(lease.resource_modes[self.f.repository.id],'write')
        self.assertEqual(workspace.read_only_resource_ids,(self.database.id,))
        self.assertTrue(all(m.resource_id!=self.database.id for m in workspace.repository_members))
        self.f.service.validate_assignment_resource_context(assignment)
        self.assertEqual(self.prepare().assignment_id,assignment.id)
        changed=self.context.model_copy(update={'work_item_ref':'different#1'})
        with self.assertRaises(TurnExecutionBindingError):self.prepare(context=changed)
        self.assertEqual(self.f.workers.store.assignment(binding.assignment_id),assignment)

    async def test_current_owner_resource_and_requester_denials_precede_workspace(self):
        f=self.f
        f.resources.update(self.database.id,ResourceUpdate(lifecycle='disabled'),actor=f.actor)
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        self.assertEqual(f.workspaces.list(f.actor),[])
        f.resources.update(self.database.id,ResourceUpdate(lifecycle='active'),actor=f.actor)
        self.items.put(self.ref,self.items.get(self.ref).model_copy(update={'current_owner':'another-owner'}))
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        self.assertEqual(f.workspaces.list(f.actor),[])
        self.items.put(self.ref,self.items.get(self.ref).model_copy(update={'current_owner':self.profile.profile_id}))
        foreign=self.context.model_copy(update={'requester_identity_id':'absent-original-requester'})
        with self.assertRaises(TurnExecutionBindingError):self.prepare(context=foreign)
        self.assertEqual(f.workspaces.list(f.actor),[])

    async def test_wrong_selector_and_missing_admission_never_materialize(self):
        wrong=self.context.model_copy(update={'read_only_resource_ids':(self.f.repository.id,)})
        with self.assertRaises(TurnExecutionBindingError):self.prepare(context=wrong)
        self.f.service.resource_context_admission=None
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        self.assertEqual(self.f.workspaces.list(self.f.actor),[])

    async def test_actual_lease_mode_drift_is_not_read_context(self):
        binding=self.prepare()
        assignment=self.f.workers.store.assignment(binding.assignment_id)
        workspace=self.f.workspaces.get(binding.workspace_id,self.f.actor)
        def change(state):
            state.leases=[l.model_copy(update={'resource_modes':{**l.resource_modes,self.database.id:'write'}})
                if l.id==workspace.lease_id else l for l in state.leases]
            return state
        self.f.workspaces.store.update(change)
        with self.assertRaises(TurnExecutionBindingError):self.f.service.validate_assignment_resource_context(assignment)

    async def test_empty_snapshot_equivalence_and_strict_context_version(self):
        base=dict(id='q',thread_id='t',project_id=self.f.project.id,message='original',created_at=1.)
        old=QueuedTurn(**base).model_dump(mode='json')
        self.assertNotIn('resource_context',old)
        self.assertEqual(QueuedTurn(**base,resource_context=None).model_dump(mode='json'),old)
        encoded=QueuedTurn(**base,resource_context=self.context).model_dump(mode='json')
        self.assertEqual(QueuedTurn.model_validate(encoded).resource_context,self.context)
        encoded['resource_context']['schema_version']='future'
        with self.assertRaises(ValueError):QueuedTurn.model_validate(encoded)
        for ids in ((self.database.id,)*2,tuple('x'+str(i) for i in range(9))):
            with self.assertRaises(ValueError):TurnCreate(message='input',read_only_resource_ids=ids)

    async def test_fence_cas_refuses_and_preserves_existing_binding(self):
        first=self.prepare()
        f=self.f
        bound=self.bindings.bind(bootstrap_id='bootstrap-context-execution',thread_id='same-thread',
            execution_id=first.execution_id,assignment_id=first.assignment_id,execution_workspace_id=first.workspace_id,actor=f.actor)
        cancelled=f.workers.cancel_bootstrap(first.assignment_id,AssignmentCancelRequest(expected_fence=0,
            reason='Fixture terminal custody rollover'),actor=f.actor)
        with self.assertRaises(ValueError):
            self.bindings.rebind(bootstrap_id='new-uncommitted',thread_id='same-thread',execution_id='new',
                assignment_id='new',execution_workspace_id='new',actor=f.actor,
                expected_assignment_id=first.assignment_id,expected_assignment_fence=cancelled.fence+1)
        self.assertEqual(self.bindings.get_by_thread('same-thread',f.actor),bound)

    async def test_turn_queue_preflight_and_canonical_native_assignment_scope(self):
        f=self.f
        binding=getattr(self,'composed_binding',None) or self.prepare()
        if getattr(self,'composed_binding',None) is None:
            self.bindings.bind(bootstrap_id='bootstrap-context-execution',thread_id='same-thread',
                execution_id=binding.execution_id,assignment_id=binding.assignment_id,
                execution_workspace_id=binding.workspace_id,actor=f.actor)
        worker_actor=f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,
            name='Private synthetic worker',scope=f.actor.tenant,service_scopes=('execution-worker:run',))
        claimed=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=worker_actor,assignment_id=binding.assignment_id)
        assignment=f.workers.start(f.worker.id,claimed.id,AssignmentStartRequest(fence=claimed.fence,
            lease_token=claimed.lease.lease_token),actor=worker_actor)
        class Session(runtime_fixture._Session):
            def __init__(self):
                super().__init__()
                self.assignment=assignment
                self.workspace_path=Path(f.workspaces.get(binding.workspace_id,f.actor).path)
            def validate_current(self):
                value=f.workers.store.assignment(binding.assignment_id)
                f.service.validate_assignment_resource_context(value)
                return value
            def _validate_current_authority(self):return self.validate_current()
            def _lease_budget(self):return 5
            async def _supervise(self,phase,fn,**kwargs):return await asyncio.to_thread(fn)
            def status(self):return SimpleNamespace(worker_id=f.worker.id,fence=assignment.fence)
        session=Session()
        class Manager:
            def get(self, assignment_id):return session if assignment_id==binding.assignment_id else None
            async def start(self, assignment_id):return session
        manager=Manager()
        manager.local_worker=SimpleNamespace(_pending_assignment=lambda aid:f.workers.store.assignment(aid))
        host=runtime_fixture._Host()
        settings={}
        host._project=lambda _:f.project
        host.work_item_states[self.ref]=self.items.get(self.ref)
        setting_service=ThreadExecutionSettingsService(load_settings=lambda:settings,
            save_settings=lambda values:settings.update(values),bindings=SimpleNamespace(for_thread=lambda _:[]),
            load_bindings=lambda:[],save_bindings=lambda _:None,gitlab_routing_enabled_for_project=lambda _:False,
            binding_report_name=lambda _:None,binding_prefix=lambda _:None)
        host._thread_run_settings=setting_service.get
        host._remember_thread_run_settings=setting_service.remember
        host._truncate_text=lambda value,limit:value[:limit]
        preflight=ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        runtime=TurnExecutionService(host,binding_service=f.service,session_manager=manager,
            bootstrap_bindings=self.bindings,control_actor=f.actor,
            actor_resolver=lambda identity,project:f.identity.actor_for_identity(identity,scope=f.actor.tenant))
        runtime.queued_preflight=preflight
        runtime.schedule_queue_drain=lambda _:None
        web=TurnService(projects=f.projects,settings=setting_service,
            recovery=SimpleNamespace(raise_if_thread_replaced=lambda _:None,release_stale_active_turn=lambda *_:None),
            resume_runtime=SimpleNamespace(),bindings=SimpleNamespace(for_thread=lambda _:[]),
            queue_policy=SimpleNamespace(depth=lambda t:len(host._thread_queue(t))),execution=runtime,
            event_sink=host._append_bot_event,truncate_text=lambda value,limit:value[:limit],
            binding_public=lambda x:x.model_dump(),preflight=preflight,agent_profiles=self.profiles)
        response=await web.start('same-thread',TurnCreate(message='One original scoped task',project_id=f.project.id,
            defer_start=True,repository_resource_id=f.repository.id,read_only_resource_ids=(self.database.id,),
            resource_context_work_item_ref=self.ref,agent_profile_id=self.profile.profile_id,
            agent_profile_revision=self.profile.revision),actor=f.actor)
        self.assertTrue(response['queued'])
        queued=host._thread_queue('same-thread')[0]
        self.assertEqual(queued.resource_context,self.context)
        retained=preflight.retain_queued(queued,organization_id=f.project.organization_id,workspace_id=f.project.workspace_id)
        self.assertEqual(retained.resource_context,self.context)
        self.assertEqual(QueuedTurn.model_validate(retained.queued_turn_snapshot).resource_context,self.context)
        await runtime.drain_thread_queue('same-thread')
        self.assertTrue(any(method=='turn/start' for method,_ in session.requests))
        self.assertEqual(host.active['same-thread'].resource_context,self.context)
        self.assertEqual(f.workers.store.assignment(binding.assignment_id).resource_ids,assignment.resource_ids)
        self.assertEqual(len(host._thread_queue('same-thread')),0)


    async def test_current_read_authority_not_project_association(self):
        from codex_web.identity import HumanUserCreate
        identity,_=self.f.identity.create_human_user(HumanUserCreate(display_name='No Resource role'),actor=self.f.actor)
        context=self.context.model_copy(update={'requester_identity_id':identity.id})
        with self.assertRaises(TurnExecutionBindingError):self.prepare(context=context)
        self.assertEqual(self.f.workspaces.list(self.f.actor),[])

    async def test_foreign_project_and_tenant_refuse_before_provisioning(self):
        original=self.items.get(self.ref)
        for updates in ({'project_id':'foreign-project'},{'workspace_id':'foreign-workspace'}):
            self.items.put(self.ref,original.model_copy(update=updates))
            with self.assertRaises(TurnExecutionBindingError):self.prepare()
            self.assertEqual(self.f.workspaces.list(self.f.actor),[])

    async def test_after_provisioning_authority_change_cleans_owned_candidate(self):
        original=self.f.workers.create_assignment
        created=[]
        def create(*args,**kwargs):
            result=original(*args,**kwargs)
            created.append(result.id)
            self.items.put(self.ref,self.items.get(self.ref).model_copy(update={'current_owner':'revoked-owner'}))
            return result
        with patch.object(self.f.workers,'create_assignment',side_effect=create):
            with self.assertRaises(TurnExecutionBindingError):self.prepare()
        self.assertEqual(len(created),1)
        value=self.f.workers.store.assignment(created[0])
        self.assertEqual(value.status,'cancelled')
        self.assertIsNone(value.lease)
        self.assertEqual(value.resource_context,self.context)
        self.assertEqual(self.bindings.list(self.f.actor),())

    async def test_scope_cannot_expand_existing_repo_only_assignment(self):
        old=self.f.service.prepare_bootstrap(bootstrap_id='bootstrap-context-execution',execution_id='context-execution',
            project_id=self.f.project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=self.f.repository.id,agent_profile=self.pin)
        with self.assertRaises(TurnExecutionBindingError) as denied:self.prepare()
        self.assertEqual(denied.exception.code,'resource_context_rollover_required')
        self.assertIsNone(self.f.workers.store.assignment(old.assignment_id).resource_context)
        self.assertNotIn(self.database.id,self.f.workers.store.assignment(old.assignment_id).resource_ids)

    async def test_terminal_binding_fence_cas_success_keeps_same_thread(self):
        first=self.prepare()
        bound=self.bindings.bind(bootstrap_id='bootstrap-context-execution',thread_id='same-thread',
            execution_id=first.execution_id,assignment_id=first.assignment_id,execution_workspace_id=first.workspace_id,actor=self.f.actor)
        terminal=self.f.workers.cancel_bootstrap(first.assignment_id,AssignmentCancelRequest(expected_fence=0,
            reason='Owned synthetic terminal custody'),actor=self.f.actor)
        second=self.prepare('fresh-context-execution')
        result=self.bindings.rebind(bootstrap_id='bootstrap-fresh-context-execution',thread_id=bound.thread_id,
            execution_id=second.execution_id,assignment_id=second.assignment_id,execution_workspace_id=second.workspace_id,
            actor=self.f.actor,expected_assignment_id=first.assignment_id,expected_assignment_fence=terminal.fence)
        self.assertEqual(result.thread_id,bound.thread_id)
        self.assertEqual(result.assignment_id,second.assignment_id)
        self.assertEqual(self.f.workers.store.assignment(second.assignment_id).resource_context,self.context)
        self.assertEqual(self.f.workers.store.assignment(first.assignment_id).fence,terminal.fence)


    async def test_proxy_checks_actual_fence_and_immutable_scope(self):
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker,ControlPlaneBrokerDeniedError
        binding=self.prepare()
        actor=self.f.identity.bootstrap_service_actor(identity_id=self.f.worker.service_identity_id,
            name='Synthetic worker',scope=self.f.actor.tenant,service_scopes=('execution-worker:run',))
        claim=self.f.workers.claim(self.f.worker.id,AssignmentClaimRequest(),actor=actor,assignment_id=binding.assignment_id)
        value=self.f.workers.start(self.f.worker.id,claim.id,AssignmentStartRequest(fence=claim.fence,
            lease_token=claim.lease.lease_token),actor=actor)
        # No socket/transport needed: exercise the real proxy admission method
        # with canonical assignment/lease and its immutable startup snapshot.
        proxy=object.__new__(AssignmentBoundControlPlaneBroker)
        proxy.snapshot=value.model_copy(deep=True)
        proxy.worker_id=self.f.worker.id
        proxy.fence=value.fence
        proxy.worker_service_identity_validator=None
        proxy.service=SimpleNamespace(_clock=time.time)
        current=[value]
        proxy.validator=lambda:current[0]
        self.assertEqual(proxy._validate_current().resource_context,self.context)
        current[0]=value.model_copy(update={'fence':value.fence+1})
        with self.assertRaises(ControlPlaneBrokerDeniedError):proxy._validate_current()
        current[0]=value.model_copy(update={'resource_context':None})
        with self.assertRaises(ControlPlaneBrokerDeniedError):proxy._validate_current()
        self.assertEqual(self.f.workers.store.assignment(value.id),value)

    async def test_current_authority_rechecked_on_same_assignment_reuse(self):
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID,AUTHORITY_ROLE_CATALOG_KIND,AuthorityRoleCatalogDefinition
        from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
        binding=self.prepare()
        registry=self.f.execution_profiles.registry
        active=registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        catalog=AuthorityRoleCatalogDefinition.model_validate(active.payload)
        payload=catalog.model_copy(update={'bindings':()}).model_dump(mode='json')
        draft=registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version='1.0',payload=payload,
            actor=self.f.actor.identity_id,reason='Synthetic READ authority revocation'))
        registry.approve_publication(draft.record_id,actor='independent-fixture-approver',
            reference='synthetic-only',reason='Synthetic authority lifecycle')
        registry.publish(draft.record_id,DefinitionPublishRequest(actor=self.f.actor.identity_id,
            reason='Synthetic authority lifecycle',expected_active_revision=active.revision))
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        self.assertEqual(self.f.workers.store.assignment(binding.assignment_id).resource_context,self.context)

    async def test_versioned_public_route_and_legacy_refusal(self):
        import httpx
        from fastapi import FastAPI
        from codex_web.api.turns import build_turns_router
        app=FastAPI()
        f=self.f
        @app.middleware('http')
        async def current_identity(request,call_next):
            request.state.identity_actor=f.actor
            return await call_next(request)
        delegate=SimpleNamespace(start=AsyncMock(return_value={'queued':True}))
        scope=SimpleNamespace(for_thread=lambda thread,actor,requested:f.project)
        app.include_router(build_turns_router(delegate,scope))
        body={'message':'Distinct synthetic original','project_id':f.project.id,
            'read_only_resource_ids':[self.database.id],'resource_context_work_item_ref':self.ref}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as client:
            old=await client.post('/api/threads/same-thread/turns',json=body)
            self.assertEqual(old.status_code,409)
            delegate.start.assert_not_awaited()
            admitted=await client.post('/api/threads/same-thread/turns/resource-context-v1',json=body)
            self.assertEqual(admitted.status_code,200)
            delegate.start.assert_awaited_once()
            self.assertEqual(delegate.start.await_args.kwargs['actor'],f.actor)
            self.assertEqual(delegate.start.await_args.args[1].read_only_resource_ids,(self.database.id,))


    async def test_restart_preserves_original_context_not_current_settings(self):
        host=runtime_fixture._Host()
        host._project=lambda _:self.f.project
        host._thread_run_settings=lambda _:ThreadRunSettings(resource_context=
            self.context.model_copy(update={'work_item_ref':'different-current-settings#1'}))
        runtime=TurnExecutionService(host)
        runtime.mark_thread_active('same-thread',project_id=self.f.project.id,
            resource_context=self.context,agent_profile=self.pin,agent_profile_actor_id=self.f.actor.identity_id)
        runtime.start_thread_turn_now=AsyncMock(return_value={'turn':{'id':'same-native-result'}})
        await runtime.resume_active_threads_after_startup({'same-thread'})
        runtime.start_thread_turn_now.assert_awaited_once()
        call=runtime.start_thread_turn_now.await_args
        self.assertEqual(call.args[0],'same-thread')
        self.assertEqual(call.kwargs['resource_context'],self.context)
        self.assertTrue(call.kwargs['resource_context_supplied'])
        self.assertEqual(host.active['same-thread'].resource_context,self.context)

    async def test_normal_replacement_carries_context_and_limits_with_terminal_fence(self):
        first=self.prepare()
        self.bindings.bind(bootstrap_id='bootstrap-context-execution',thread_id='same-thread',
            execution_id=first.execution_id,assignment_id=first.assignment_id,
            execution_workspace_id=first.workspace_id,actor=self.f.actor)
        original=self.f.workers.store.assignment(first.assignment_id)
        self.f.workers.cancel_bootstrap(first.assignment_id,AssignmentCancelRequest(expected_fence=0,
            reason='Genuine terminal synthetic custody'),actor=self.f.actor)
        class Manager:
            def get(self,assignment_id):return None
        manager=Manager()
        manager.local_worker=SimpleNamespace(_pending_assignment=lambda aid:self.f.workers.store.assignment(aid),
            worker_service=self.f.workers,control_actor=self.f.actor)
        host=runtime_fixture._Host()
        runtime=TurnExecutionService(host,binding_service=self.f.service,session_manager=manager,
            bootstrap_bindings=self.bindings,control_actor=self.f.actor)
        result=await runtime._supersede_thread_bootstrap(thread_id='same-thread',project=self.f.project,
            runtime_binding=original.runtime_binding,sandbox=original.sandbox,approval_policy=original.approval_policy,
            execution_profile_id=original.execution_profile_id,agent_profile=original.agent_profile,
            explicit_repository_id=self.f.repository.id,resource_context=original.resource_context,
            previous_assignment_id=original.id,retirement_reason='procedure_assignment_exhausted',
            renewal_limits=original.limits)
        actual=self.f.workers.store.assignment(result.assignment_id)
        self.assertEqual(result.thread_id,'same-thread')
        self.assertNotEqual(actual.id,original.id)
        self.assertEqual(actual.resource_context,self.context)
        self.assertEqual(actual.limits,original.limits)
        lease=self.f.workspaces.store.lease(self.f.workspaces.get(actual.execution_workspace_id,self.f.actor).lease_id)
        self.assertEqual(lease.resource_modes[self.database.id],'read')
        self.assertEqual(lease.resource_modes[self.f.repository.id],'write')


    async def test_existing_write_reservation_blocks_new_read_context(self):
        from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
        writer=self.f.workspaces.acquire(ExecutionWorkspaceAcquire(execution_id='synthetic-database-writer',
            project_id=self.f.project.id,resource_ids=(self.f.repository.id,self.database.id),
            repository_resource_id=self.f.repository.id,lease_mode='write'),actor=self.f.actor)
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        lease=self.f.workspaces.store.lease(writer.lease_id)
        self.assertEqual(lease.resource_modes[self.database.id],'write')
        from codex_web.services.execution_workers import AssignmentNotFoundError
        with self.assertRaises(AssignmentNotFoundError):
            self.f.workers.assignment_for_execution('context-execution',actor=self.f.actor)


    async def test_verification_context_requester_never_falls_back_to_controller(self):
        from codex_web.identity import HumanUserCreate
        from codex_web.services.worker_verifications import WorkerVerificationAdapter
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        binding=self.prepare()
        identity,_=self.f.identity.create_human_user(HumanUserCreate(display_name='Independent context requester'),actor=self.f.actor)
        value=self.f.workers.store.assignment(binding.assignment_id)
        context=self.context.model_copy(update={'requester_identity_id':identity.id})
        adapter=WorkerVerificationAdapter(SimpleNamespace(identity=self.f.identity))
        # A boundary-only negative: this modified value is never persisted or
        # treated as admitted; it must not silently borrow created_by authority.
        observed=adapter.context_requester(value.model_copy(update={'resource_context':context}))
        self.assertEqual(observed.identity_id,identity.id)
        self.assertNotEqual(observed.identity_id,value.created_by)
        absent=context.model_copy(update={'requester_identity_id':'no-such-current-identity'})
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            adapter.context_requester(value.model_copy(update={'resource_context':absent}))


    async def test_project_binding_without_work_item_membership_refuses(self):
        item=self.items.get(self.ref)
        self.items.put(self.ref,item.model_copy(update={'resource_ids':[self.f.repository.id]}))
        self.assertIn(self.database.id,self.f.resources.resource_ids_for_project(self.f.project))
        with self.assertRaises(TurnExecutionBindingError):self.prepare()
        self.assertEqual(self.f.workspaces.list(self.f.actor),[])

    async def test_legacy_ignoring_readers_are_not_claimed_safe_for_rollback(self):
        from pydantic import BaseModel,create_model
        from copy import deepcopy
        # These two persisted legacy models really use extra=ignore. Recreate
        # their pre-context field sets, and prove the unsafe information loss.
        # No automatic conversion or old-reader refusal is promised by rollout.
        for model,values in ((ThreadRunSettings,{}),(QueuedTurn,dict(id='old',thread_id='same-thread',
                project_id=self.f.project.id,message='original',created_at=1.))):
            legacy=create_model('Legacy'+model.__name__,__base__=BaseModel,
                **{name:(field.annotation,deepcopy(field)) for name,field in model.model_fields.items() if name!='resource_context'})
            encoded=model(**values,resource_context=self.context).model_dump(mode='json')
            self.assertNotIn('resource_context',legacy.model_validate(encoded).model_dump())
            self.assertEqual(model.model_validate(encoded).resource_context,self.context)


    async def test_membership_removed_after_admission_denies_current_session_scope(self):
        binding=self.prepare()
        assignment=self.f.workers.store.assignment(binding.assignment_id)
        item=self.items.get(self.ref)
        self.items.put(self.ref,item.model_copy(update={'resource_ids':[self.f.repository.id]}))
        with self.assertRaises(TurnExecutionBindingError):
            self.f.service.validate_assignment_resource_context(assignment)
        self.assertEqual(self.f.workers.store.assignment(binding.assignment_id).resource_context,self.context)
