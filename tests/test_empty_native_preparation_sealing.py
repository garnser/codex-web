"""Real pending bootstrap custody; no native process, HTTP, or test authority grant."""
import unittest
from unittest.mock import patch

from codex_web.definitions import DefinitionLifecycle
from codex_web.services.definitions import DefinitionNotFoundError
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentCompleteRequest, AssignmentStartRequest, ExecutionWorkerRegister
from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.codex_worker_configuration import install_codex_worker_configuration
from codex_web.services.configuration import ConfigurationService
from codex_web.services.turn_execution_binding import TurnExecutionBindingService
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.skills import SkillCreate, SkillPublish
from tests import test_native_provider_preparation as preparation
from tests.test_turn_execution_binding import _Projects


class EmptyNativePreparationSealingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = preparation.NativeProviderPreparationTests()
        await self.env.asyncSetUp()
        self.addAsyncCleanup(self.env.asyncTearDown)
        f = self.f = self.env.fixture
        workspace = await self.env.make_empty_scratch()
        old = f.native_assignment
        runtime = old.runtime_binding
        f.workers.complete(f.procedure_worker.id, old.id, AssignmentCompleteRequest(
            lease_token=old.lease.lease_token, fence=old.fence, succeeded=False,
            failure_code='private_fixture_replaced'), actor=f.procedure_worker_actor)
        f.workspaces.release(workspace.id, ExecutionWorkspaceRelease(), actor=f.actor)
        # The inherited legacy worker declares only contract1.0. Provision a
        # normal separate worker pool advertising the actual bootstrap contract.
        legacy_worker = f.procedure_worker
        f.procedure_worker = f.workers.register(ExecutionWorkerRegister(
            service_identity_id=f.procedure_worker_actor.identity_id,
            pool='private-empty-bootstrap', version=legacy_worker.version,
            capabilities=legacy_worker.capabilities,
            supported_execution_contract_versions=tuple(dict.fromkeys((
                *legacy_worker.supported_execution_contract_versions, 'thread-bootstrap/1.0'))),
            supported_sandbox_profiles=legacy_worker.supported_sandbox_profiles,
            max_concurrency=1), actor=f.actor)
        configuration = ConfigurationService(ConfigurationRegistryStore(f.sqlite))
        install_codex_worker_configuration(configuration)
        builder = TurnExecutionBindingService(configuration, _Projects(f.project), f.resources,
            f.workspaces, f.workers, control_actor=f.actor, execution_profiles=f.profiles,
            runtime_binding=runtime)
        bound = builder.prepare_bootstrap(bootstrap_id='private-empty-seal',
            execution_id='thread-bootstrap-private-empty-seal', project_id=f.project.id,
            sandbox='read-only', approval_policy='on-request', execution_profile_id='orchestration-only')
        claimed = f.workers.claim(f.procedure_worker.id, AssignmentClaimRequest(lease_seconds=3600),
            actor=f.procedure_worker_actor, assignment_id=bound.assignment_id)
        self.assertIsNotNone(claimed)
        f.native_assignment = f.workers.start(f.procedure_worker.id, bound.assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token),
            actor=f.procedure_worker_actor)
        custody = f.native_assignment.native_credential_custody
        self.assertIsNotNone(custody)
        self.assertIsNone(custody.admitted_at)
        self.assertEqual(f.native_assignment.resource_ids, ())
        self.assertEqual(custody.procedures, ())
        self.assertEqual(f.native_assignment.secret_refs, ())

    def current(self):
        return self.f.workers.store.assignment(self.f.native_assignment.id)

    def assert_unsealed(self):
        current = self.current()
        self.assertIsNone(current.native_credential_custody.admitted_at)
        self.assertEqual(current.native_credential_custody.procedures, ())
        self.assertEqual(current.secret_refs, ())
        self.assertEqual(current.procedure_discoveries, ())
        self.assertEqual(self.f.ops.calls, [])

    async def test_unchanged_attested_empty_scope_seals_real_pending_bootstrap(self):
        f = self.f
        original = self.env.owner.admit_initial_native_credentials
        observations = []
        discover = f.discovery.discover
        in_cas = False
        def observe(context, **options):
            if in_cas:
                observations.append(options.get('_include_supporting_context'))
            return discover(context, **options)
        def admit(assignment, selected, actor, validate):
            def inside(current):
                nonlocal in_cas
                in_cas = True
                try:
                    return validate(current)
                finally:
                    in_cas = False
            return original(assignment, selected, actor, inside)
        with patch.object(f.discovery, 'discover', side_effect=observe), patch.object(
                self.env.owner, 'admit_initial_native_credentials', side_effect=admit):
            result, _ = await self.env.admit()
        self.assertTrue(result.admitted)
        self.assertEqual(result.selected, ())
        self.assertTrue(any('native_physical_scope_empty' in c.reasons for c in result.candidates))
        self.assertIn('canonical:native-empty-physical-scope:', result.policy_source)
        self.assertEqual(observations, [False])
        sealed = self.current().native_credential_custody
        self.assertIsNotNone(sealed.admitted_at)
        self.assertEqual(sealed.procedures, ())
        self.assertEqual(self.current().secret_refs, ())
        self.assertEqual(f.ops.calls, [])
        self.env.admission.preparation_input_guard(result, project=f.project, actor=f.actor,
            execution_id=f.native_assignment.execution_id, assignment_id=f.native_assignment.id,
            sandbox=f.native_assignment.sandbox, request_checksum='a'*64)()

    async def test_genuine_no_candidate_path_keeps_plain_empty_seal(self):
        self.f.definition_registry.store.update(lambda rows: [r for r in rows if r.kind != 'agent.skill'])
        result, _ = await self.env.admit()
        self.assertTrue(result.admitted)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.selected, ())
        self.assertIsNotNone(self.current().native_credential_custody.admitted_at)
        self.assertEqual(self.f.ops.calls, [])

    async def changed_before_cas(self, change, expected):
        original = self.env.owner.admit_initial_native_credentials
        calls = []
        def admit(assignment, selected, actor, validate):
            calls.append(1)
            change()
            return original(assignment, selected, actor, validate)
        with patch.object(self.env.owner, 'admit_initial_native_credentials', side_effect=admit):
            with self.assertRaises(expected) as caught:
                await self.env.admit()
        self.assertEqual(calls, [1])
        self.assert_unsealed()
        return caught.exception

    async def test_new_catalog_candidate_cannot_disappear_during_seal(self):
        def add():
            f = self.f
            created = f.skills.create(SkillCreate(skill_id='private-new-provider-candidate',
                name='New provider candidate', instructions='Exact private provider declaration',
                operational_procedure=self.env.content), actor=f.actor)
            f.skills.publish(created['skillId'], created['recordId'], SkillPublish(), actor=f.actor)
        await self.changed_before_cas(add, ValueError)

    async def test_current_empty_workspace_lease_must_remain_exactly_empty(self):
        def change():
            f = self.f
            workspace = f.workspaces.get(f.native_assignment.execution_workspace_id, f.actor)
            f.workspaces.store.update(lambda state: state.model_copy(update={'leases': [
                lease.model_copy(update={'resource_ids': (f.repo.id,), 'resource_modes': {f.repo.id: 'write'}})
                if lease.id == workspace.lease_id else lease for lease in state.leases]}))
        await self.changed_before_cas(change, ValueError)

    async def test_changed_current_profile_refuses_without_sealing(self):
        def change():
            ref = self.f.native_assignment.execution_profile_definition
            self.f.definition_registry.store.update(lambda rows: [r.model_copy(update={
                'lifecycle': DefinitionLifecycle.QUARANTINED}) if r.record_id == ref.record_id else r for r in rows])
        await self.changed_before_cas(change, DefinitionNotFoundError)

    async def test_changed_policy_is_typed_local_preinput_refusal(self):
        original = self.env.admission._native_policy
        def change():
            def changed(*args, **kwargs):
                enforcement, source = original(*args, **kwargs)
                return enforcement, source + ';private-current-policy-changed'
            self.env.admission._native_policy = changed
        error = await self.changed_before_cas(change, ExecutionPrestartRefusal)
        self.assertEqual(error.detail['code'], 'procedure_native_preparation_changed')

    async def test_attestor_failure_is_not_reclassified_or_ignored(self):
        marker = RuntimeError('private attestor unavailable')
        def change():
            self.env.attestor.side_effect = marker
        error = await self.changed_before_cas(change, RuntimeError)
        self.assertIs(error, marker)
