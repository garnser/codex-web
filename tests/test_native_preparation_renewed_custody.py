"""Canonical renewal during native preparation, with unchanged custody and real guards."""
import time
import unittest
from unittest.mock import patch

from codex_web.execution_workers import AssignmentRenewRequest, WorkerHeartbeatRequest
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.services.procedure_discovery import OperationContext
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.execution_workers import WorkerLeaseError
import tests.test_native_provider_preparation as native
import tests.test_native_procedure_credential_custody as credential


async def renewed_admission(test, *, f, owner, admission, assignment, worker, worker_actor,
                            phase='discovery', renew=True):
    now = [time.time()]
    origin = now[0]
    owner.clock = lambda: now[0]
    f.discovery.clock = lambda: now[0]
    f.discovery.execution_capabilities.clock = lambda: now[0]
    real_discover = f.discovery.discover
    real_profile = admission.execution_profiles.resolve
    crossed = [False]
    attested = []

    def cross_boundary():
        if crossed[0]:
            return
        crossed[0] = True
        now[0] = origin + 90
        f.workers.heartbeat(worker.id, WorkerHeartbeatRequest(version=worker.version), actor=worker_actor)
        current = f.workers.assignment_for_execution(assignment.execution_id, actor=f.actor)
        if renew:
            current = f.workers.renew(worker.id, current.id, AssignmentRenewRequest(
                lease_token=current.lease.lease_token, fence=current.fence, lease_seconds=120), actor=worker_actor)
            test.assertEqual(current.lease.expires_at, origin + 210)
        now[0] = origin + 121

    def attest(expected, procedures):
        test.assertIs(procedures, owner)
        if phase == 'attestation':
            cross_boundary()
        current = f.workers.assignment_for_execution(expected.execution_id, actor=f.actor)
        test.assertEqual(expected.model_dump(exclude={'lease', 'updated_at'}),
                         current.model_dump(exclude={'lease', 'updated_at'}))
        test.assertEqual(expected.lease.model_dump(exclude={'expires_at', 'renewed_at'}),
                         current.lease.model_dump(exclude={'expires_at', 'renewed_at'}))
        owner._live(current)
        attested.append(current)
        return current

    def discover(context, *args, **kwargs):
        result = real_discover(context, *args, **kwargs)
        if phase == 'discovery':
            cross_boundary()
        return result

    def profile(*args, **kwargs):
        result = real_profile(*args, **kwargs)
        if phase == 'profile':
            cross_boundary()
        return result

    with patch('time.time', side_effect=lambda: now[0]):
        snapshot = f.workers.renew(worker.id, assignment.id, AssignmentRenewRequest(
            lease_token=assignment.lease.lease_token, fence=assignment.fence, lease_seconds=120), actor=worker_actor)
        admission.native_session_attestor = attest
        refusal = None
        result = None
        with patch.object(f.discovery, 'discover', side_effect=discover), \
             patch.object(admission.execution_profiles, 'resolve', side_effect=profile):
            try:
                result, _ = await test.admit()
            except ExecutionPrestartRefusal as exc:
                refusal = exc.detail
            except WorkerLeaseError as exc:
                # Genuine current expiry may also refuse the existing final
                # discovery writer; it must never produce admitted preparation.
                refusal = {'code': 'worker_lease_refused', 'message': str(exc)}
        current = f.workers.assignment_for_execution(assignment.execution_id, actor=f.actor)
        test.assertTrue(crossed[0])
        test.assertLess(snapshot.lease.expires_at, now[0])
        test.assertEqual(current.id, snapshot.id)
        test.assertEqual(current.fence, snapshot.fence)
        test.assertEqual(current.lease.lease_token, snapshot.lease.lease_token)
        test.assertEqual(current.lease.acquired_at, snapshot.lease.acquired_at)
        test.assertEqual(f.ops.calls, [])
        if renew:
            owner._live(current)
            test.assertTrue(attested)
            test.assertGreater(current.lease.expires_at, now[0])
            test.assertIsNone(refusal, 'Current same-custody renewal must remain admissible: '
                              + str((refusal or {}).get('code')))
            test.assertTrue(result.admitted)
        else:
            test.assertLess(current.lease.expires_at, now[0])
            test.assertIsNotNone(refusal)
        return result, current, snapshot


class EmptyPreparationRenewedCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = native.NativeProviderPreparationTests()
        await self.fixture.asyncSetUp()
        await self.fixture.make_empty_scratch()
        self.f, self.owner = self.fixture.fixture, self.fixture.owner

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def admit(self):
        return await self.fixture.admit()

    async def renewal(self, **options):
        return await renewed_admission(self, f=self.f, owner=self.owner,
            admission=self.fixture.admission, assignment=self.f.native_assignment,
            worker=self.f.procedure_worker, worker_actor=self.f.procedure_worker_actor, **options)

    async def test_current_same_owner_renewal_during_discovery_admits_empty_scope(self):
        result, _, _ = await self.renewal()
        self.assertEqual(result.outcome, 'no_match')
        self.assertIn('canonical:native-empty-physical-scope:', result.policy_source)

    async def test_renewal_during_attestation_is_refreshed_after_attestor(self):
        result, _, _ = await self.renewal(phase='attestation')
        self.assertEqual(result.outcome, 'no_match')

    async def test_renewal_during_profile_read_keeps_current_capability_context(self):
        result, _, _ = await self.renewal(phase='profile')
        self.assertEqual(result.outcome, 'no_match')

    async def test_real_expiry_without_renewal_still_refuses(self):
        await self.renewal(renew=False)

    async def test_changed_custody_during_attestation_never_becomes_renewal(self):
        f = self.f
        original = f.workers.assignment_for_execution(f.native_assignment.execution_id, actor=f.actor)
        definition = original.execution_profile_definition
        observed, _ = f.discovery.discover(OperationContext(actor=f.actor, project_id=f.project.id, resource_ids=(), operation=None))
        changes = {
            'worker': {'assigned_worker_id': 'foreign-worker',
                'lease': original.lease.model_copy(update={'worker_id': 'foreign-worker'})},
            'fence': {'fence': original.fence + 1,
                'lease': original.lease.model_copy(update={'fence': original.fence + 1})},
            'token': {'lease': original.lease.model_copy(update={'lease_token': 'changed-token-must-not-be-accepted'})},
            'acquisition': {'lease': original.lease.model_copy(update={'acquired_at': original.lease.acquired_at - 1})},
            'definition': {'execution_profile_definition': definition.model_copy(update={'checksum': 'f' * 64})},
            'profile': {'execution_profile_id': 'repository-write'},
            'agent-profile': {'agent_profile': AgentProfileExecutionBinding(profile_id='changed-profile',
                profile_revision=1, profile_record_id='changed-profile-record')},
            'runtime': {'runtime_binding': original.runtime_binding.model_copy(update={
                'capability_revision': original.runtime_binding.capability_revision + 1})},
            'scope': {'resource_ids': (f.repo.id,)},
            'requester': {'created_by': 'different-requester'},
            'deadline': {'deadline_at': time.time() - 1},
            'input-attribution': {'artifact_ids': ('changed-input-artifact',)},
            'credential': {'secret_refs': ('unexpected-secret-reference',)},
            'history': {'procedure_discoveries': (*original.procedure_discoveries, observed)},
        }
        def save(value):
            return f.workers.store.update_assignment(f.procedure_worker.id, original.id,
                lambda state: state.model_copy(update={'assignments': [value]})).assignments[0]
        for name, update in changes.items():
            with self.subTest(name=name):
                save(original)
                changed = [False]
                def attest(expected, procedures):
                    self.assertIs(procedures, self.owner)
                    changed[0] = True
                    save(original.model_copy(update=update))
                    # Caller/attestor return values cannot confer canonical custody.
                    return expected
                self.fixture.admission.native_session_attestor = attest
                try:
                    with self.assertRaises((ExecutionPrestartRefusal, WorkerLeaseError)) as caught:
                        await self.admit()
                    self.assertTrue(changed[0])
                    if isinstance(caught.exception, WorkerLeaseError):
                        self.assertEqual(name, 'fence')
                        self.assertIn('fence/status changed', str(caught.exception))
                    else:
                        self.assertEqual(caught.exception.detail['code'], 'procedure_inaccessible')
                    self.assertEqual(f.ops.calls, [])
                finally:
                    save(original)

    async def test_attestor_cannot_supply_fabricated_renewed_lease(self):
        f = self.f
        now = [time.time()]
        self.owner.clock = lambda: now[0]
        called = []
        with patch('time.time', side_effect=lambda: now[0]):
            original = f.workers.renew(f.procedure_worker.id, f.native_assignment.id,
                AssignmentRenewRequest(lease_token=f.native_assignment.lease.lease_token,
                    fence=f.native_assignment.fence, lease_seconds=120), actor=f.procedure_worker_actor)
            def forged_attestor(*args):
                called.append(True)
                now[0] = original.lease.expires_at + 1
                return original.model_copy(update={'lease': original.lease.model_copy(
                    update={'expires_at': now[0] + 10000})})
            self.fixture.admission.native_session_attestor = forged_attestor
            with self.assertRaises((ExecutionPrestartRefusal, WorkerLeaseError)):
                await self.admit()
            self.assertTrue(called)
            self.assertEqual(f.workers.store.assignment(original.id).lease, original.lease)
            self.assertLess(original.lease.expires_at, now[0])
        self.assertEqual(f.ops.calls, [])


class SelectedPreparationRenewedCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = credential.NativeProcedureCredentialCustodyTests()
        await self.fixture.asyncSetUp()
        self.fixture.bootstrap()
        self.f, self.owner = self.fixture.f, self.fixture.owner

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def admit(self):
        return await self.fixture.admit()

    async def renewal(self, **options):
        return await renewed_admission(self, f=self.f, owner=self.owner,
            admission=self.fixture.admission, assignment=self.fixture.current(),
            worker=self.fixture.worker, worker_actor=self.fixture.worker_actor, **options)

    async def test_already_sealed_selected_renewal_preserves_exact_credential_scope(self):
        first, _ = await self.admit()
        sealed = self.fixture.current().native_credential_custody
        self.assertIsNotNone(sealed.admitted_at)
        result, current, _ = await self.renewal()
        self.assertEqual(result.selected, first.selected)
        self.assertEqual(current.native_credential_custody, sealed)
        self.assertEqual(current.secret_refs, (self.fixture.provider_secret.id,))

    async def test_initial_selected_seal_still_uses_existing_canonical_cas(self):
        self.assertIsNone(self.fixture.current().native_credential_custody.admitted_at)
        result, current, _ = await self.renewal()
        self.assertEqual(result.selected, (self.fixture.fixture.reference,))
        self.assertIsNotNone(current.native_credential_custody.admitted_at)
        self.assertEqual(current.native_credential_custody.procedures, result.selected)
        self.assertEqual(current.secret_refs, (self.fixture.provider_secret.id,))

    async def test_pending_empty_builder_seal_refreshes_before_existing_cas(self):
        import copy
        from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest
        from codex_web.skills import SkillUpdate, SkillPublish
        # The inherited legacy procedure is rejected by repository selector in
        # selected tests. Empty preparation must validate it before exclusion.
        # Use the same canonical provider-only publication as make_empty_scratch.
        content = copy.deepcopy(self.fixture.fixture.content)
        content['environments'] = ['staging']
        updated = self.f.skills.update('gitlab-procedure', SkillUpdate(
            name='Other provider procedure', instructions='Provider only',
            operational_procedure=content, reason='Private compatible empty-scope catalog'), actor=self.f.actor)
        self.f.skills.publish(updated['skillId'], updated['recordId'], SkillPublish(), actor=self.f.actor)
        options = {**self.fixture.options,
            'bootstrap_id': 'renewed-empty-credential-bootstrap',
            'execution_id': 'renewed-empty-credential-execution',
            'execution_profile_id': 'orchestration-only', 'explicit_repository_id': None}
        bound = self.fixture.builder.prepare_bootstrap(**options)
        claim = self.f.workers.claim(self.fixture.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=self.fixture.worker_actor, assignment_id=bound.assignment_id)
        self.fixture.assignment = self.f.workers.start(self.fixture.worker.id, bound.assignment_id,
            AssignmentStartRequest(fence=claim.fence, lease_token=claim.lease.lease_token),
            actor=self.fixture.worker_actor)
        pending = self.fixture.current()
        self.assertEqual(pending.resource_ids, ())
        self.assertIsNone(pending.native_credential_custody.admitted_at)
        context = self.fixture.admission._preparation_context(pending, self.f.actor, self.f.project,
            pending.sandbox, 'a' * 64, require_operations=False)
        original, _ = self.f.discovery.discover(context)
        healthy, _ = self.fixture.admission._empty_preparation_discovery(pending, self.f.actor,
            self.f.project, pending.sandbox, 'a' * 64,
            tuple(candidate.reference for candidate in original.candidates))
        self.assertTrue(healthy.admitted)
        self.assertEqual(healthy.outcome, 'no_match')
        self.assertEqual(self.fixture.current(), pending)  # Read proof did not seal or append history.
        result, current, _ = await self.renewal()
        self.assertEqual(result.outcome, 'no_match')
        self.assertEqual(current.native_credential_custody.procedures, ())
        self.assertIsNotNone(current.native_credential_custody.admitted_at)
        self.assertEqual(current.secret_refs, ())
