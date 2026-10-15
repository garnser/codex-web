"""Common native turn admission is exercised for every source lane."""
import dataclasses
import unittest
from types import SimpleNamespace

from codex_web.models import Project
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService
from codex_web.storage.thread_history import ThreadHistoryRepository
from tests import test_procedure_discovery as discovery_fixtures
from tests import test_turn_execution as turn_fixtures


class NativeProcedureAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        discovery_fixtures.DiscoveryTests.setUp(self)
        self.history = ThreadHistoryRepository(self.state)

    async def asyncTearDown(self):
        discovery_fixtures.DiscoveryTests.tearDown(self)

    async def test_all_native_sources_refuse_before_provider_input_and_record_discovery(self):
        for source in ('web', 'executive', 'work_item_dispatch', 'queued:web', 'recovery'):
            with self.subTest(source=source):
                _, _, sessions, runtime = turn_fixtures.TurnExecutionStartTests._service(self)
                # The production callback uses canonical assignment resources,
                # not task text. This fixture has no required procedures, but
                # mutable native tools still cannot enforce a future mutation
                # check; the required typed unsupported result is explicit.
                assignment = SimpleNamespace(id='assignment-1', resource_ids=(), fence=1,
                    network=SimpleNamespace(enabled=False), sandbox='read-only')
                recorded = []
                workers = SimpleNamespace(assignment_for_execution=lambda *a, **k: assignment,
                    record_procedure_discovery=lambda *a, **k: recorded.append((a,k)))
                admission = NativeProcedureAdmissionService(self.discovery, workers, self.history, self.admin)
                async def gate(**kwargs):
                    return await admission(**{**kwargs, 'actor': self.admin, 'assignment_id': assignment.id,
                        'project': Project(id='project-a', organization_id=self.admin.organization_id,
                            workspace_id=self.admin.workspace_id, name='Project', path='/workspace/project')})
                runtime.procedure_admission = gate
                with self.assertRaises(ExecutionPrestartRefusal) as caught:
                    await runtime.start_thread_turn_now('t1', project=Project(id='p1', name='Project', path='/workspace/project'),
                        message='Investigate operational logs', sandbox='workspace-write', approval_policy='on-request', source=source)
                self.assertEqual(caught.exception.detail['code'], 'procedure_unsupported_enforcement')
                self.assertFalse(any(method == 'turn/start' for method, _ in sessions.session.requests))
                self.assertEqual(recorded[0][0][1].outcome, 'unsupported_enforcement')
        self.assertEqual(len(self.history.get('t1')['procedureDiscoveries']), 5)

    async def test_actor_scope_and_assignment_changes_are_not_no_match(self):
        workers = SimpleNamespace(assignment_for_execution=lambda *a, **k: SimpleNamespace(id='other-assignment'))
        admission = NativeProcedureAdmissionService(self.discovery, workers, self.history, self.admin)
        project = Project(id='project-a', organization_id=self.admin.organization_id,
            workspace_id=self.admin.workspace_id, name='Project', path='/workspace/project')
        for actor, code in ((self.foreign,'procedure_actor_scope_unavailable'), (self.admin,'procedure_assignment_changed')):
            with self.assertRaises(ExecutionPrestartRefusal) as caught:
                await admission(project=project, actor=actor, thread_id='t1', execution_id='exec', assignment_id='expected', sandbox='read-only', source='web')
            self.assertEqual(caught.exception.detail['code'],code)

    async def test_provider_neutral_continuation_resume_and_objective_cannot_bypass(self):
        from codex_web.agent_runtime import AgentRuntimeSessionRequest, AgentRuntimeTurnRequest, AgentRuntimeObjectiveRequest
        from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
        from codex_web.storage.agent_sessions import AgentSessionStore
        from tests.test_agent_runtime import _Runtime
        for provider in ('provider-a','alternative-provider'):
            with self.subTest(provider=provider):
                registry = AgentRuntimeRegistry()
                adapter = _Runtime(); adapter.provider_id = provider
                registry.register(adapter)
                sessions = AgentSessionService(AgentSessionStore(self.state),registry)
                session = await sessions.create(request=AgentRuntimeSessionRequest(project_id='project-a'),actor=self.admin,
                    provider_id=provider,runtime_id=adapter.runtime_id)
                admission = NativeProcedureAdmissionService(self.discovery,SimpleNamespace(),self.history,self.admin)
                sessions.procedure_admission = admission.agent_session_admission
                for invoke in (
                    lambda: sessions.start_turn(session.id,AgentRuntimeTurnRequest(message='Restart GitLab'),actor=self.admin),
                    lambda: sessions.resume(session.id,request=AgentRuntimeSessionRequest(project_id='project-a'),actor=self.admin),
                    lambda: sessions.set_objective(session.id,AgentRuntimeObjectiveRequest(objective='Repair operational logs'),actor=self.admin),
                ):
                    with self.assertRaises(ExecutionPrestartRefusal) as caught:
                        await invoke()
                    self.assertEqual(caught.exception.detail['code'],'procedure_unsupported_enforcement')
                self.assertEqual([name for name,_ in adapter.calls],['create'])

    async def test_raw_rpc_escape_hatches_refuse_without_native_admission(self):
        _, _, sessions, runtime = turn_fixtures.TurnExecutionStartTests._service(self)
        runtime.procedure_admission = object()
        for method in ('turn/start','thread/resume','thread/goal/set'):
            with self.assertRaises(ExecutionPrestartRefusal) as caught:
                await runtime.request_for_thread('t1',method,{'threadId':'t1'})
            self.assertEqual(caught.exception.detail['code'],'procedure_unsupported_enforcement')
        self.assertEqual(sessions.session.requests,[])


class ProductionNativeProcedureAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pathlib import Path
        from tests.test_procedure_action_admission import ProcedureActionAdmissionTests
        from tests.test_execution_workspaces import _FakeBackend
        from codex_web.resources import ResourceCreate, ResourceType
        from codex_web.services.execution_workspaces import ExecutionWorkspaceService
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
        from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
        from codex_web.execution_workers import ExecutionAssignmentCreate, AssignmentClaimRequest, AssignmentStartRequest
        await ProcedureActionAdmissionTests.asyncSetUp(self)
        self.project = Project(id='native-project', organization_id='local', workspace_id='default',
            name='Native project', path=str(Path(self.temp.name) / 'repository'))
        Path(self.project.path).mkdir()
        self.repo = self.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY, name='Development'), actor=self.actor)
        self.workspaces = ExecutionWorkspaceService(ExecutionWorkspaceStateStore(self.sqlite),
            _FakeBackend(Path(self.temp.name) / 'workspaces'), self.resources, lambda _: self.project)
        self.workers.workspaces = self.workspaces
        self.profiles = install_execution_profile_definitions(self.definition_registry)
        profile, ref = self.profiles.resolve('repository-write')
        self.workspace = self.workspaces.acquire(ExecutionWorkspaceAcquire(subject=self.assignment.subject,
            execution_id='native-execution', project_id=self.project.id, resource_ids=(self.repo.id,),
            repository_resource_id=self.repo.id), actor=self.actor)
        if getattr(self, 'full_access', False):
            from codex_web.services.turn_execution_binding import TurnExecutionBindingService
            from codex_web.services.configuration import ConfigurationService
            from codex_web.storage.configuration_registry import ConfigurationRegistryStore
            from codex_web.services.codex_worker_configuration import install_codex_worker_configuration, CODEX_WORKER_ACCESS_TOKEN_CONFIG
            from tests.test_turn_execution_binding import TurnExecutionBindingTests, _Projects
            from codex_web.agent_profiles import AgentProfileExecutionBinding
            self.configuration = ConfigurationService(ConfigurationRegistryStore(self.sqlite))
            install_codex_worker_configuration(self.configuration)
            TurnExecutionBindingTests._publish_secret(self, scope_id=self.project.id)
            self.resources.bind_project(project=self.project, resource_id=self.repo.id, actor=self.actor)
            service = TurnExecutionBindingService(self.configuration, _Projects(self.project), self.resources,
                self.workspaces, self.workers, control_actor=self.actor, execution_profiles=self.profiles,
                runtime_binding=self.assignment.runtime_binding,
                runtime_credential_configs={(self.adapter.provider_id, self.adapter.runtime_id): CODEX_WORKER_ACCESS_TOKEN_CONFIG})
            # Provision through the real assignment builder, not preselected gate booleans.
            self.workspaces.release(self.workspace.id, __import__('codex_web.execution_workspaces', fromlist=['ExecutionWorkspaceRelease']).ExecutionWorkspaceRelease(), actor=self.actor)
            binding = service._prepare_subject(subject=self.assignment.subject, thread_id=None,
                execution_id='full-access-execution', project_id=self.project.id, sandbox='danger-full-access',
                approval_policy='on-request', execution_contract_version='1.0', session_seconds=3600,
                max_session_seconds=3600, limits=None, explicit_repository_id=self.repo.id,
                agent_profile=AgentProfileExecutionBinding(profile_id='codex-web-vp-engineering',
                    profile_revision=10, profile_record_id='vp-revision-10'),
                execution_profile_id='repository-write')
            pending = self.workers.assignment_for_execution(binding.execution_id, actor=self.actor)
            self.workspace = self.workspaces.get(binding.workspace_id, self.actor)
        else:
            pending = self.workers.create_assignment(ExecutionAssignmentCreate(subject=self.assignment.subject,
                execution_id='native-execution', project_id=self.project.id, resource_ids=(self.repo.id,),
                execution_workspace_id=self.workspace.id, execution_contract_version='1.0',
                execution_profile_id=profile.id, execution_profile_definition=ref,
                required_capabilities=profile.required_worker_capabilities, runtime_binding=self.assignment.runtime_binding), actor=self.actor)
        claimed = self.workers.claim(self.procedure_worker.id, AssignmentClaimRequest(lease_seconds=3600),
            actor=self.procedure_worker_actor, assignment_id=pending.id)
        self.native_assignment = self.workers.start(self.procedure_worker.id, pending.id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.procedure_worker_actor)
        self.history = ThreadHistoryRepository(self.sqlite)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_production_wiring_preserves_no_match_write_for_all_native_sources(self):
        from codex_web.services.procedure_discovery import install_native_procedure_admission
        for source in ('web', 'executive', 'work_item_dispatch', 'queued:web', 'recovery'):
            with self.subTest(source=source):
                host, binding, sessions, runtime = turn_fixtures.TurnExecutionStartTests._service(self)
                # Only the transport/binding provisioning is a test double. Gate,
                # registry, profiles, worker, assignment and workspace are real.
                binding.prepare = binding.prepare_bootstrap = lambda **kw: SimpleNamespace(
                    assignment_id=self.native_assignment.id, workspace_id=self.workspace.id,
                    execution_id=self.native_assignment.execution_id)
                sessions.session.validate_current = lambda: self.native_assignment
                sessions.session.workspace_path = __import__('pathlib').Path(self.workspace.path)
                install_native_procedure_admission(runtime, SimpleNamespace(), self.discovery, self.workers,
                    self.history, self.actor, self.profiles)
                await runtime.start_thread_turn_now('t1', project=self.project, message='Improve repository tests',
                    sandbox='workspace-write', approval_policy='on-request', source=source,
                    execution_id=self.native_assignment.execution_id, actor=self.actor)
                self.assertTrue(any(method == 'turn/start' for method, _ in sessions.session.requests))
        evidence = self.workers.assignment_for_execution(self.native_assignment.execution_id, actor=self.actor).procedure_discoveries
        self.assertEqual(len(evidence), 5)
        self.assertTrue(all(d.admitted and d.outcome == 'no_match' for d in evidence))
        self.assertTrue(all(d.policy_source.startswith('definition:') for d in evidence))

    async def test_protected_scope_refuses_without_tool_execution(self):
        from codex_web.services.procedure_discovery import install_native_procedure_admission
        admission = install_native_procedure_admission(SimpleNamespace(), SimpleNamespace(), self.discovery,
            self.workers, self.history, self.actor, self.profiles)
        # A broader requested sandbox cannot inherit the isolated preparation policy.
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await admission(project=self.project, actor=self.actor, thread_id='t2', execution_id=self.native_assignment.execution_id,
                assignment_id=self.native_assignment.id, sandbox='danger-full-access', source='web')
        self.assertEqual(caught.exception.detail['code'], 'procedure_unsupported_enforcement')

    async def test_effective_profile_revision_change_invalidates_native_envelope(self):
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.services.procedure_discovery import NativeProcedureAdmissionService
        record = self.profiles.record()
        updated = self.profiles.registry.create_draft(DefinitionDraftCreate(kind=record.kind,
            definition_id=record.definition_id, definition_schema_version=record.definition_schema_version,
            payload=record.payload, actor=self.actor.identity_id, reason='Reviewed replacement'))
        self.profiles.registry.publish(updated.record_id, DefinitionPublishRequest(actor=self.actor.identity_id))
        admission = NativeProcedureAdmissionService(self.discovery, self.workers, self.history, self.actor, self.profiles)
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await admission(project=self.project, actor=self.actor, thread_id='t3', execution_id=self.native_assignment.execution_id,
                assignment_id=self.native_assignment.id, sandbox='workspace-write', source='recovery')
        self.assertEqual(caught.exception.detail['code'], 'procedure_unsupported_enforcement')
        self.assertEqual(caught.exception.detail['procedure_discovery']['policy_source'], 'canonical:execution-profile-pin-changed')


class FullAccessNativePreparationTests(unittest.IsolatedAsyncioTestCase):
    full_access = True
    asyncSetUp = ProductionNativeProcedureAdmissionTests.asyncSetUp
    asyncTearDown = ProductionNativeProcedureAdmissionTests.asyncTearDown

    async def start(self):
        from codex_web.services.procedure_discovery import install_native_procedure_admission
        _, binding, sessions, runtime = turn_fixtures.TurnExecutionStartTests._service(self)
        binding.prepare = binding.prepare_bootstrap = lambda **kw: SimpleNamespace(
            assignment_id=self.native_assignment.id, workspace_id=self.workspace.id,
            execution_id=self.native_assignment.execution_id)
        sessions.session.validate_current = lambda: self.native_assignment
        sessions.session.workspace_path = __import__('pathlib').Path(self.workspace.path)
        install_native_procedure_admission(runtime, SimpleNamespace(), self.discovery, self.workers,
            self.history, self.actor, self.profiles)
        await runtime.start_thread_turn_now('full-access-thread', project=self.project, message='Improve tests',
            sandbox='danger-full-access', approval_policy='on-request', source='executive',
            execution_id=self.native_assignment.execution_id, actor=self.actor)
        return sessions

    async def test_named_full_access_assignment_reaches_shared_native_start(self):
        from codex_web.execution_workers import WorkerCapability
        assignment = self.native_assignment
        self.assertEqual(assignment.sandbox, 'danger-full-access')
        self.assertFalse(assignment.network.enabled)
        self.assertNotIn(WorkerCapability.NETWORK, assignment.required_capabilities)
        self.assertEqual(assignment.agent_profile.profile_id, 'codex-web-vp-engineering')
        self.assertEqual(assignment.agent_profile.profile_revision, 10)
        self.assertEqual(assignment.execution_profile_definition, self.profiles.resolve('repository-write')[1])
        sessions = await self.start()
        self.assertTrue(any(method == 'turn/start' for method, _ in sessions.session.requests))
        saved = self.workers.assignment_for_execution(assignment.execution_id, actor=self.actor)
        self.assertTrue(saved.procedure_discoveries[-1].admitted)

    async def test_full_access_refuses_unsupported_envelopes_and_expired_assignment(self):
        from unittest.mock import patch
        a = self.native_assignment
        for changes in ({'network': a.network.model_copy(update={'enabled': True})},
                        {'lease': a.lease.model_copy(update={'expires_at': 1})},
                        {'execution_profile_definition': a.execution_profile_definition.model_copy(update={'revision': 999})},
                        {'resource_ids': (*a.resource_ids, self.target.id)}):
            with self.subTest(changes=changes), patch.object(self.workers, 'assignment_for_execution',
                    return_value=a.model_copy(update=changes)):
                with self.assertRaises(ExecutionPrestartRefusal):
                    await self.start()
        profile, ref = self.profiles.resolve('repository-write')
        for field in ('network_enabled', 'host_mutation'):
            with self.subTest(field=field), patch.object(self.profiles, 'resolve',
                    return_value=(profile.model_copy(update={field: True}), ref)):
                with self.assertRaises(ExecutionPrestartRefusal):
                    await self.start()

    async def test_full_access_requires_workspace_lease_and_mutable_repository_scope(self):
        from unittest.mock import patch
        lease = self.workspaces.store.lease(self.workspace.lease_id)
        for bad in (lease.model_copy(update={'expires_at': 1}), lease.model_copy(update={'resource_modes': {}})):
            with self.subTest(lease=bad), patch.object(self.workspaces.store, 'lease', return_value=bad):
                with self.assertRaises(ExecutionPrestartRefusal):
                    await self.start()
        profile, ref = self.profiles.resolve('repository-write')
        with patch.object(self.profiles, 'resolve', return_value=(profile.model_copy(update={'repository_access': 'read-only'}), ref)):
            with self.assertRaises(ExecutionPrestartRefusal):
                await self.start()

    async def test_full_access_matching_required_procedure_still_refuses(self):
        from codex_web.skills import SkillCreate, SkillPublish
        from codex_web.resources import ResourceUpdate, ResourceOperationalContext
        # Publish a real required procedure selecting the repository target.
        content = __import__('copy').deepcopy(self.content)
        content['resource_selectors'][0]['resource_types'] = ['repository']
        content['service_versions'] = []
        content['required_provider_capabilities'] = []
        diagnostic = content['steps'][0]
        diagnostic.pop('action')
        diagnostic['worker'] = {'capability': 'network', 'artifact_reference': 'artifact-http-validation',
            'artifact_checksum': 'a' * 64, 'resource_selector': 'service'}
        diagnostic['permissions'][0]['capability'] = 'repository.read'
        content['steps'] = [diagnostic]
        self.resources.update(self.repo.id, ResourceUpdate(operational_context=ResourceOperationalContext(environment='staging')), actor=self.actor)
        skill = self.skills.create(SkillCreate(skill_id='repository-required', name='Required procedure',
            instructions='Use canonical actions', operational_procedure=content), actor=self.actor)
        self.skills.publish(skill['skillId'], skill['recordId'], SkillPublish(), actor=self.actor)
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.start()
        self.assertEqual(caught.exception.detail['code'], 'procedure_unsupported_enforcement')
