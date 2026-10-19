"""Real scoped worker/workspace admission, with native transport as the seam."""
import unittest
from unittest.mock import patch

from codex_web.execution_workers import ExecutionAssignmentCreate, AssignmentClaimRequest, AssignmentStartRequest
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, LeaseMode
from codex_web.resource_context import ReadOnlyResourceContext
from codex_web.resources import ResourceCreate
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.execution_workers import WorkerLeaseError
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService
from tests import test_procedure_native_admission as existing


class ReadContextProcedureAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = existing.ProductionNativeProcedureAdmissionTests.asyncTearDown

    async def asyncSetUp(self):
        await existing.ProductionNativeProcedureAdmissionTests.asyncSetUp(self)
        self.database = self.resources.create(ResourceCreate(resource_type='database', name='Synthetic context'), actor=self.actor)
        self.resources.bind_project(project=self.project, resource_id=self.database.id, actor=self.actor)
        self.context = ReadOnlyResourceContext(schema_version='1.0', read_only_resource_ids=(self.database.id,),
            requester_identity_id=self.actor.identity_id, work_item_ref='fixture/source#1530')
        self.workspace = self.workspaces.acquire(ExecutionWorkspaceAcquire(subject=self.assignment.subject,
            execution_id='context-preparation', project_id=self.project.id, resource_ids=(self.repo.id,self.database.id),
            repository_resource_id=self.repo.id, read_only_resource_ids=(self.database.id,), resource_context=self.context), actor=self.actor)
        profile, ref = self.profiles.resolve('repository-write')
        # Register supported schema capability through the real worker boundary.
        worker = self.workers.ensure_local_worker(service_identity_id=self.procedure_worker.service_identity_id,
            version='1.0.0', max_concurrency=4, capabilities=self.procedure_worker.capabilities,
            supported_execution_contract_versions=('1.0','thread-bootstrap/1.1'), actor=self.actor)
        pending = self.workers.create_assignment(ExecutionAssignmentCreate(subject=self.assignment.subject,
            execution_id=self.workspace.execution_id, project_id=self.project.id, resource_ids=(self.repo.id,self.database.id),
            execution_workspace_id=self.workspace.id, execution_contract_version='thread-bootstrap/1.1',
            resource_context=self.context, execution_profile_id=profile.id, execution_profile_definition=ref,
            required_capabilities=profile.required_worker_capabilities, runtime_binding=self.assignment.runtime_binding), actor=self.actor)
        claimed = self.workers.claim(worker.id, AssignmentClaimRequest(lease_seconds=3600),
            actor=self.procedure_worker_actor, assignment_id=pending.id)
        self.native_assignment = self.workers.start(worker.id,pending.id,
            AssignmentStartRequest(fence=claimed.fence,lease_token=claimed.lease.lease_token), actor=self.procedure_worker_actor)
        self.admission = NativeProcedureAdmissionService(self.discovery,self.workers,self.history,self.actor,self.profiles)

    async def admit(self):
        return await self.admission(project=self.project,actor=self.actor,thread_id='context-thread',
            execution_id=self.native_assignment.execution_id,assignment_id=self.native_assignment.id,
            sandbox='workspace-write',source='web')

    async def test_read_context_reaches_common_native_transport_and_keeps_database_read_only(self):
        from types import SimpleNamespace
        from tests import test_turn_execution as turns
        _,binding,sessions,runtime = turns.TurnExecutionStartTests._service(self)
        binding.prepare = binding.prepare_bootstrap = lambda **kw: SimpleNamespace(
            assignment_id=self.native_assignment.id,workspace_id=self.workspace.id,execution_id=self.native_assignment.execution_id)
        sessions.session.validate_current = lambda:self.native_assignment
        from pathlib import Path
        sessions.session.workspace_path = Path(self.workspace.path)
        runtime.procedure_admission = self.admission
        await runtime.start_thread_turn_now('context-thread',project=self.project,message='Review capacity metadata only',
            sandbox='workspace-write',approval_policy='on-request',execution_id=self.native_assignment.execution_id,actor=self.actor)
        self.assertEqual(sum(method=='turn/start' for method,_ in sessions.session.requests),1)
        stored=self.workers.store.assignment(self.native_assignment.id)
        self.assertEqual(stored.procedure_discoveries[-1].outcome,'no_match')
        self.assertEqual(set(stored.procedure_discoveries[-1].resource_ids),{self.repo.id,self.database.id})
        self.assertEqual(stored.resource_context,self.context)
        self.assertEqual(self.workspaces.store.lease(self.workspace.lease_id).resource_modes[self.database.id],'read')
        self.assertFalse(stored.network.enabled)
        self.assertTrue(all(m.resource_id!=self.database.id for m in self.workspace.repository_members))

    async def test_missing_or_mismatched_context_never_qualifies_preparation(self):
        original=self.native_assignment
        for context in (None,self.context.model_copy(update={'requester_identity_id':'other'}),
                        self.context.model_copy(update={'work_item_ref':'different#1'}),
                        self.context.model_copy(update={'read_only_resource_ids':(self.repo.id,)})):
            with self.subTest(context=context), patch.object(self.workers,'assignment_for_execution',
                    return_value=original.model_copy(update={'resource_context':context})):
                with self.assertRaises(ExecutionPrestartRefusal):await self.admit()

    async def test_write_or_missing_database_lease_mode_refuses(self):
        original=self.workspaces.store.lease(self.workspace.lease_id)
        for mode in ('write',None):
            modes=dict(original.resource_modes)
            if mode is None:modes.pop(self.database.id)
            else:modes[self.database.id]=LeaseMode(mode)
            def change(state):
                state.leases=[v.model_copy(update={'resource_modes':modes}) if v.id==original.id else v for v in state.leases]
                return state
            self.workspaces.store.update(change)
            with self.subTest(mode=mode):
                with self.assertRaises(ExecutionPrestartRefusal):await self.admit()

    async def test_stale_fence_and_network_enabled_assignment_stay_refused(self):
        original=self.native_assignment
        for changed in (original.model_copy(update={'fence':original.fence+1}),
                        original.model_copy(update={'network':original.network.model_copy(update={'enabled':True})})):
            with self.subTest(changed=changed),patch.object(self.workers,'assignment_for_execution',return_value=changed):
                with self.assertRaises((ExecutionPrestartRefusal,WorkerLeaseError)):await self.admit()

    async def test_project_binding_without_frozen_context_is_not_native_database_scope(self):
        assignment=self.native_assignment.model_copy(update={'resource_context':None})
        workspace=self.workspace.model_copy(update={'resource_context':None,'read_only_resource_ids':()})
        with patch.object(self.workers,'assignment_for_execution',return_value=assignment), \
                patch.object(self.workspaces,'get',return_value=workspace):
            with self.assertRaises(ExecutionPrestartRefusal):await self.admit()

    async def test_selected_operational_procedure_still_requires_canonical_action(self):
        from copy import deepcopy
        from codex_web.skills import SkillCreate, SkillPublish
        from codex_web.resources import ResourceUpdate, ResourceOperationalContext
        content=deepcopy(self.content)
        content['resource_selectors'][0]['resource_types']=['repository']
        content['service_versions']=[]
        content['required_provider_capabilities']=[]
        step=content['steps'][0]
        step.pop('action')
        step['worker']={'capability':'network','artifact_reference':'artifact-http-validation',
            'artifact_checksum':'a'*64,'resource_selector':'service'}
        step['permissions'][0]['capability']='repository.read'
        content['steps']=[step]
        for resource in (self.repo,self.database):
            self.resources.update(resource.id,ResourceUpdate(operational_context=ResourceOperationalContext(environment='staging')),actor=self.actor)
        skill=self.skills.create(SkillCreate(skill_id='context-required-procedure',name='Required operation',
            instructions='Use canonical actions',operational_procedure=content),actor=self.actor)
        self.skills.publish(skill['skillId'],skill['recordId'],SkillPublish(),actor=self.actor)
        with self.assertRaises(ExecutionPrestartRefusal) as caught:await self.admit()
        self.assertEqual(caught.exception.detail['code'],'procedure_unsupported_enforcement')
        self.assertTrue(caught.exception.detail['procedure_discovery']['selected'])


class PublicReadContextProcedureTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup helpers without collecting the fixture class's test methods.
    from tests import test_read_only_resource_context_reconciliation as fixture
    setUp=fixture.FreshPublicReadContextTests.setUp
    prepare=fixture.FreshPublicReadContextTests.prepare
    _setup_source=fixture.FreshPublicReadContextTests._setup_source
    _claim=fixture.FreshPublicReadContextTests._claim
    _dispatch=fixture.FreshPublicReadContextTests._dispatch

    async def test_authenticated_public_turn_reaches_native_start_through_real_procedure_gate(self):
        from types import SimpleNamespace
        from codex_web.agent_providers import AgentProviderUpsert
        from codex_web.services.agent_providers import AgentProviderService
        from codex_web.services.agent_runtime import AgentRuntimeRegistry
        from codex_web.services.procedure_discovery import (ProcedureDiscoveryService,
            ProcedureExecutionCapabilityService,install_native_procedure_admission)
        from codex_web.storage.agent_providers import AgentProviderStore
        from codex_web.storage.thread_history import ThreadHistoryRepository
        from tests.test_agent_runtime import _Runtime
        from tests import read_only_resource_context_harness as harness_module
        from tests import test_read_only_resource_context_reconciliation as source
        original=harness_module.NativeContextHarness.__init__
        installed=[]
        def with_gate(harness,case,f,**kwargs):
            original(harness,case,f,**kwargs)
            runtimes=AgentRuntimeRegistry();adapter=_Runtime()
            adapter.provider_id=f.service.runtime_binding.provider_id
            adapter.runtime_id=f.service.runtime_binding.runtime_id
            runtimes.register(adapter,capability_revision=f.service.runtime_binding.capability_revision)
            providers=AgentProviderService(AgentProviderStore(f.sqlite))
            providers.upsert(AgentProviderUpsert(id=adapter.provider_id,display_name='Private context runtime',
                declared_capabilities=adapter.capabilities,granted_capabilities=adapter.capabilities,health='healthy'),actor=f.actor)
            discovery=ProcedureDiscoveryService(self.broker.procedure_admission.discovery.skills,f.resources,
                execution_capabilities=ProcedureExecutionCapabilityService(f.workers,runtimes,providers))
            install_native_procedure_admission(harness.runtime,SimpleNamespace(),discovery,f.workers,
                ThreadHistoryRepository(f.sqlite),f.actor,f.execution_profiles)
            installed.append(harness)
        with patch.object(harness_module.NativeContextHarness,'__init__',with_gate):
            await source.FreshPublicReadContextTests.test_fresh_public_route_after_genuine_ack_and_terminal_repo_binding(self)
        harness,=installed
        assignment=self.f.workers.store.assignment(harness.started_assignments[0])
        proof=assignment.procedure_discoveries[-1]
        self.assertTrue(proof.admitted)
        self.assertEqual(proof.outcome,'no_match')
        self.assertIn(self.database.id,proof.resource_ids)
        self.assertEqual(assignment.resource_context.requester_identity_id,harness.requester.identity_id)
        self.assertNotEqual(assignment.created_by,harness.requester.identity_id)
