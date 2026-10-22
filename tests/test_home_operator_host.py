from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from codex_web.agent_profiles import AgentProfileCreate, AgentProfileUpdate
from codex_web.agent_runtime import AgentSession
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest, WorkerLifecycle
from codex_web.identity import MembershipRole
from codex_web.models import HomeOperatorBinding, IndexedThread
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_runtime import AgentSessionService, AgentRuntimeRegistry
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.control_plane_broker import (
    AssignmentBoundControlPlaneBroker, ControlPlaneBrokerService, ControlPlaneBrokerDeniedError,
)
from codex_web.services.home_operator_host import HomeOperatorHostService, HOST_INSPECTION, inspect_host_metadata
from codex_web.services.projects import ProjectService
from codex_web.services.identity import AuthorizationError
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from tests import test_control_plane_broker as broker_fixtures
from tests import test_turn_execution_binding as binding_fixtures


class HomeOperatorHostTests(unittest.IsolatedAsyncioTestCase):
    _request = broker_fixtures.ControlPlaneBrokerTests._request

    async def asyncSetUp(self):
        self.f = f = binding_fixtures.TurnExecutionBindingTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        f._publish_secret()
        f.service._clock = time.time
        f.workers.identity = f.identity
        self.actor = f.actor
        self.worker_actor = f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,
            name='Fixture worker', scope=f.actor.tenant, service_scopes=('execution-worker:run',))
        self.root = Path(f.temp.name)
        self.projects = ProjectService(ProjectRepository(self.root / 'projects.json', store=f.sqlite))
        self.projects.repository.save([f.project])
        f.projects = self.projects
        f.service.projects = self.projects
        self.registry = f.execution_profiles.registry
        self.authority = install_authority_roles(self.registry, f.resources)
        self.profiles = AgentProfileService(AgentProfileStore(f.sqlite), definitions=self.registry,
            authority=self.authority, execution_profiles=f.execution_profiles)
        f.execution_profiles.agent_profiles = self.profiles
        self.payload = execution_profile_catalog_seed_payload()
        self.profile_payload = next(p for p in self.payload['profiles'] if p['id'] == 'orchestration-only')
        self.profile_payload['control_plane_operations'].append(HOST_INSPECTION.id)
        self.publish(self.payload)
        agent = self.profiles.create(AgentProfileCreate(profile_id='home-operator', name='Configured operator',
            execution_profile_id='orchestration-only', authority_role_id='local-admin'), actor=self.actor)
        binding = f.service.prepare_bootstrap(bootstrap_id='host-bootstrap', execution_id='host-execution',
            project_id='home', sandbox='workspace-write', approval_policy='on-request',
            execution_profile_id='orchestration-only', agent_profile=self.profiles.binding_for(agent))
        claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(), actor=self.worker_actor,
                                  assignment_id=binding.assignment_id)
        self.assignment = f.workers.start(f.worker.id, binding.assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.worker_actor)
        self.bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(f.sqlite))
        self.bindings.bind(bootstrap_id='host-bootstrap', thread_id='operator-thread', execution_id='host-execution',
            assignment_id=self.assignment.id, execution_workspace_id=self.assignment.execution_workspace_id, actor=self.actor)
        self.sessions = AgentSessionService(AgentSessionStore(f.sqlite), AgentRuntimeRegistry())
        self.sessions.store.upsert(AgentSession(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, provider_id='openai', runtime_id='codex', runtime_type='native',
            provider_native_session_id='operator-thread', project_id='home', execution_id='host-execution',
            assignment_id=self.assignment.id, execution_workspace_id=self.assignment.execution_workspace_id,
            worker_id=f.worker.id, status='running'))
        self.index = ThreadIndexRepository(f.sqlite, self.root / 'threads.json')
        self.index.upsert(IndexedThread(id='operator-thread', name='Operator', project_id='home'))
        self.scope = ThreadScopeService(self.projects, self.sessions, self.index)
        self.select('operator-thread', 0)
        self.service = ControlPlaneBrokerService(identity=f.identity, authority=self.authority,
            work_items=broker_fixtures._WorkItems({}), operator=broker_fixtures._Operator(), execution_profiles=f.execution_profiles,
            execution_workers=f.workers, execution_workspaces=f.workspaces,
            audit=ControlPlaneBrokerAuditStore(f.sqlite))
        self.inspector = Mock(return_value={'process': {'pid': 123}, 'platform': {'system': 'fixture'},
                                           'home_filesystem': {'total_bytes': 10, 'used_bytes': 4, 'free_bytes': 6}})
        self.service.home_operator_host = HomeOperatorHostService(broker=self.service, projects=self.projects,
            thread_scope=self.scope, thread_index=self.index, bootstrap_bindings=self.bindings, inspector=self.inspector)
        self.service.procedure_admission = AsyncMock()
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id=f.worker.id, worker_actor=self.worker_actor, fence=self.assignment.fence,
            validator=self.current)
        await self.broker.start()
        self.addAsyncCleanup(self.stop)

    async def stop(self):
        await self.broker.stop()
        if self.broker._handler_tasks:
            await asyncio.gather(*self.broker._handler_tasks)

    def current(self):
        return self.f.workers.store.assignment(self.assignment.id)

    def publish(self, payload):
        record = self.registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
            kind='execution-profile-catalog', definition_schema_version='1.0', scope_type='project', scope_id='home',
            payload=payload, actor='fixture'))
        self.registry.publish(record.record_id, DefinitionPublishRequest(actor='fixture'))
        return record

    def select(self, thread, revision):
        self.projects.repository.update_home_operator(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, expected_revision=revision,
            binding=HomeOperatorBinding(thread_id=thread, selected_by=self.actor.identity_id,
                revision=revision + 1, updated_at=time.time()))

    def mutate_assignment(self, **changes):
        self.f.workers.store.update(lambda state: state.model_copy(update={'assignments': [
            item.model_copy(update=changes) if item.id == self.assignment.id else item for item in state.assignments]}))

    async def test_actual_broker_inspection_uses_current_explicit_home_authority_and_audit(self):
        before = self.current().model_dump()
        before_binding = self.bindings.get_by_thread('operator-thread', self.actor)
        status, _, result = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 200, result)
        self.assertEqual(result['metadata'], self.inspector.return_value)
        self.assertEqual(result['assignment_id'], self.assignment.id)
        self.assertEqual(result['operator_binding']['revision'], 1)
        self.inspector.assert_called_once_with(self.projects.get('home').path)
        self.service.procedure_admission.assert_awaited_once()
        self.assertEqual(self.service.procedure_admission.call_args.kwargs['operation'].id, HOST_INSPECTION.id)
        self.assertEqual(self.current().model_dump(), before)
        self.assertEqual(self.bindings.get_by_thread('operator-thread', self.actor), before_binding)
        events = self.service.audit.load().events
        self.assertEqual(events[-1].operation_id, HOST_INSPECTION.id)
        self.assertEqual(events[-1].decision, 'allow')
        self.assertEqual(events[-1].assignment_id, self.assignment.id)

    async def test_arbitrary_commands_paths_queries_and_post_refuse_without_inspection(self):
        for method, path, payload in [('POST', '/api/operator/host', {}),
                ('GET', '/api/operator/host?path=/etc', None),
                ('GET', '/api/operator/host', {'command': 'anything'})]:
            with self.subTest(method=method, path=path):
                status, _, _ = await self._request(method, path, payload=payload)
                self.assertGreaterEqual(status, 400)
        self.inspector.assert_not_called()

    async def test_home_designation_alone_does_not_inherit_undeclared_host_operation(self):
        self.profile_payload['control_plane_operations'].remove(HOST_INSPECTION.id)
        self.publish(self.payload)
        # The existing assignment's old Definition is no longer effective.
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_ordinary_unselected_home_thread_cannot_inspect(self):
        self.select(None, 1)
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_current_administrator_revocation_is_not_inherited_from_created_by(self):
        self.f.identity.store.update(lambda state: state.model_copy(update={'memberships': [
            m.model_copy(update={'roles': [MembershipRole.MEMBER]}) if m.identity_id == self.actor.identity_id else m
            for m in state.memberships]}))
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_changed_agent_profile_refuses_without_inspection(self):
        self.profiles.update('home-operator', AgentProfileUpdate(name='changed', reason='fixture update'), actor=self.actor)
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_changed_fence_and_expired_lease_refuse(self):
        for changes in ({'fence': self.assignment.fence + 1},
                        {'lease': self.assignment.lease.model_copy(update={'expires_at': time.time() - 1})}):
            self.mutate_assignment(**changes)
            status, _, _ = await self._request('GET', '/api/operator/host')
            self.assertEqual(status, 403)
            self.mutate_assignment(fence=self.assignment.fence, lease=self.assignment.lease)
        self.inspector.assert_not_called()

    async def test_canonical_worker_quarantine_refuses(self):
        self.f.workers.store.update(lambda state: state.model_copy(update={'workers': [
            w.model_copy(update={'lifecycle': WorkerLifecycle.QUARANTINED}) for w in state.workers]}))
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_changed_bootstrap_binding_never_follows_successor(self):
        self.bindings.store.update(lambda state: state.model_copy(update={'bindings': [
            b.model_copy(update={'assignment_id': 'successor-assignment'}) for b in state.bindings]}))
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_archived_thread_refuses(self):
        self.index.upsert(IndexedThread(id='operator-thread', name='Operator', project_id='home', archived=True))
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_reselection_during_actual_read_suppresses_result(self):
        self.inspector.side_effect = lambda path: (self.select(None, 1), {'must_not_escape': True})[1]
        status, _, result = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403, result)
        self.assertNotIn('metadata', result)
        self.assertNotIn('must_not_escape', json.dumps(result))
        self.inspector.assert_called_once()

    async def test_profile_revocation_during_read_suppresses_result(self):
        def inspect(path):
            self.profiles.update('home-operator', AgentProfileUpdate(name='changed', reason='revoked during read'), actor=self.actor)
            return {'must_not_escape': True}
        self.inspector.side_effect = inspect
        status, _, result = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403, result)
        self.assertNotIn('metadata', result)

    async def test_metadata_collector_has_fixed_nonsecret_shape(self):
        result = inspect_host_metadata(str(self.root))
        self.assertEqual(set(result), {'process', 'platform', 'home_filesystem'})
        self.assertEqual(set(result['process']), {'pid', 'python', 'implementation'})
        self.assertEqual(set(result['home_filesystem']), {'total_bytes', 'used_bytes', 'free_bytes'})
        self.assertNotIn('environment', result)
        self.assertNotIn('path', result)

    async def test_missing_bootstrap_is_a_typed_refusal_not_an_internal_error(self):
        self.bindings.store.update(lambda state: state.model_copy(update={'bindings': []}))
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_foreign_canonical_native_owner_refuses(self):
        session = self.sessions.store.find_unique_by_native_id('operator-thread', provider_id='openai', runtime_id='codex')
        self.sessions.store.upsert(session.model_copy(update={'project_id': 'foreign-project'}))
        status, _, _ = await self._request('GET', '/api/operator/host')
        self.assertEqual(status, 403)
        self.inspector.assert_not_called()

    async def test_operation_catalog_does_not_advertise_host_access_to_ordinary_home_threads(self):
        current = self.service.public_assignment_capability(self.assignment)
        self.assertTrue(current['assignment_context']['host_inspection_declaration']['declared'])
        self.assertIn(HOST_INSPECTION.id, [op['id'] for op in current['operations']])
        self.select(None, 1)
        ordinary = self.service.public_assignment_capability(self.assignment)
        self.assertFalse(ordinary['assignment_context']['host_inspection_declaration']['declared'])
        self.assertNotIn(HOST_INSPECTION.id, [op['id'] for op in ordinary['operations']])
        self.inspector.assert_not_called()

    async def test_explicit_operation_is_required_even_with_current_definition_and_admin(self):
        self.profile_payload['control_plane_operations'].remove(HOST_INSPECTION.id)
        self.publish(self.payload)
        _, current_ref = self.f.execution_profiles.resolve('orchestration-only',
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, project_id='home')
        new_snapshot = self.assignment.model_copy(update={'execution_profile_definition': current_ref})
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, 'not explicitly declared'):
            self.service.home_operator_host.authorize(new_snapshot, self.actor)
        self.inspector.assert_not_called()


    async def test_native_input_accepts_original_current_human_admin_before_selection(self):
        # Selection is impossible until the native thread exists. Admission is
        # restrictive here; the actual host read still requires its designation.
        self.select(None, 1)
        self.service.home_operator_host.authorize_native_input(
            assignment=self.assignment, actor=self.actor, project=self.projects.get('home'))
        self.inspector.assert_not_called()

    async def test_service_input_cannot_borrow_original_administrator_assignment(self):
        with self.assertRaisesRegex(AuthorizationError, 'exact human administrator'):
            self.service.home_operator_host.authorize_native_input(
                assignment=self.assignment, actor=self.worker_actor, project=self.projects.get('home'))
        self.inspector.assert_not_called()

    async def test_native_input_refreshes_membership_instead_of_trusting_old_admin_actor(self):
        self.f.identity.store.update(lambda state: state.model_copy(update={'memberships': [
            m.model_copy(update={'roles': [MembershipRole.MEMBER]}) if m.identity_id == self.actor.identity_id else m
            for m in state.memberships]}))
        with self.assertRaises(AuthorizationError):
            self.service.home_operator_host.authorize_native_input(
                assignment=self.assignment, actor=self.actor, project=self.projects.get('home'))

    async def test_ordinary_profile_does_not_acquire_new_input_restrictions(self):
        self.profile_payload['control_plane_operations'].remove(HOST_INSPECTION.id)
        self.publish(self.payload)
        _, reference = self.f.execution_profiles.resolve('orchestration-only',
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, project_id='home')
        ordinary = self.assignment.model_copy(update={'execution_profile_definition': reference})
        self.service.home_operator_host.authorize_native_input(
            assignment=ordinary, actor=self.worker_actor, project=self.projects.get('home'))
        self.inspector.assert_not_called()

    async def test_common_native_start_denies_service_input_before_discovery_or_history(self):
        discovery, history = Mock(), Mock()
        admission = NativeProcedureAdmissionService(discovery, self.f.workers, history, self.actor,
                                                    self.f.execution_profiles)
        admission.input_authorizer = self.service.home_operator_host.authorize_native_input
        before = self.current().model_dump()
        with self.assertRaises(ExecutionPrestartRefusal) as refused:
            await admission(project=self.projects.get('home'), actor=self.worker_actor,
                thread_id='operator-thread', execution_id=self.assignment.execution_id,
                assignment_id=self.assignment.id, sandbox=self.assignment.sandbox,
                source='actual-common-native-admission')
        self.assertEqual(refused.exception.status_code, 403)
        self.assertEqual(refused.exception.detail['code'], 'native_input_authority_denied')
        discovery.resource_facts.assert_not_called()
        discovery.discover.assert_not_called()
        history.record_procedure_discovery.assert_not_called()
        self.assertEqual(self.current().model_dump(), before)
        self.inspector.assert_not_called()


    async def test_common_native_start_does_not_treat_fallback_control_actor_as_human_input(self):
        discovery, history = Mock(), Mock()
        admission = NativeProcedureAdmissionService(discovery, self.f.workers, history, self.actor,
                                                    self.f.execution_profiles)
        admission.input_authorizer = self.service.home_operator_host.authorize_native_input
        with self.assertRaises(ExecutionPrestartRefusal) as refused:
            await admission(project=self.projects.get('home'), actor=self.actor, input_actor=None,
                thread_id='operator-thread', execution_id=self.assignment.execution_id,
                assignment_id=self.assignment.id, sandbox=self.assignment.sandbox,
                source='automatic-without-human-actor')
        self.assertEqual(refused.exception.detail['code'], 'native_input_authority_denied')
        discovery.discover.assert_not_called()
        history.record_procedure_discovery.assert_not_called()
        self.inspector.assert_not_called()
