"""Real pending-bootstrap CAS must not monopolize renewal during preparation.

Events control ordering; cleanup bounds detect deadlock, never response latency.
No app, process, provider or model is started by this isolated fixture.
"""
import asyncio
import threading
import time
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from codex_web.execution_workers import AssignmentRenewRequest, WorkerHeartbeatRequest, WorkerLifecycle
from codex_web.services import execution_workers as worker_module
from codex_web.services.agent_process_session import (
    AssignmentBoundAgentProcessSession, AssignmentBoundAgentProcessSessionStaleError,
)
from codex_web.services.codex_worker_session import TrustedLocalCodexCredentialProvider
from codex_web.services.control_plane_broker import (
    AssignmentBoundControlPlaneBroker, ControlPlaneBrokerService, ControlPlaneBrokerDeniedError,
)
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.local_execution_backend import BubblewrapExecutionBackend, LocalIsolationStatus
from codex_web.runtime.execution import TurnExecutionService, ExecutionPrestartRefusal
from tests.test_empty_native_preparation_sealing import EmptyNativePreparationSealingTests
from tests.test_native_procedure_credential_custody import NativeProcedureCredentialCustodyTests


class NativeSealRenewalCouplingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = EmptyNativePreparationSealingTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.env.asyncTearDown)
        await self.install_real_owners()

    async def install_real_owners(self):
        f = self.fixture.f
        self.now = [time.time()]
        assignment = self.fixture.current()
        backend = BubblewrapExecutionBackend(executable='/private-fixture-never-executed')
        backend.probe = Mock(return_value=LocalIsolationStatus(backend='bubblewrap', ready=True,
            reason=None, supports_network_disabled=True, supports_network_allowlist=False,
            supports_resource_limits=True))
        backend.execution_disk_usage = Mock(return_value=0)
        backend.process_tree_rss_bytes = Mock(return_value=0)
        home = Path(f.temp.name) / 'private-trusted-home'
        home.mkdir()
        credentials = TrustedLocalCodexCredentialProvider(codex_home=home,
            executable='/private-fixture-never-executed', clock=lambda: self.now[0])
        local = SimpleNamespace(worker=f.procedure_worker, worker_service=f.workers,
            worker_actor=f.procedure_worker_actor, backend=backend,
            readonly_disk_bytes=lambda _assignment: 0,
            _pending_assignment=lambda assignment_id: f.workers.store.assignment(assignment_id))
        session = self.session = AssignmentBoundAgentProcessSession(local, SimpleNamespace(), assignment.id,
            runtime_factory=Mock(), credential_provider=credentials, runtime_binding=assignment.runtime_binding,
            clock=lambda: self.now[0])
        self.addCleanup(session._supervision_executor.shutdown, wait=True, cancel_futures=True)
        self.addCleanup(session._diagnostic_executor.shutdown, wait=True, cancel_futures=True)
        session.fence = assignment.fence
        session._bound_assignment = assignment
        session.delegation = credentials._grant(assignment, worker_id=f.procedure_worker.id, fence=assignment.fence)
        session.workspace_path = Path(f.temp.name)
        session.started_monotonic = time.monotonic()
        ready = threading.Event()
        ready.set()
        session.runtime = SimpleNamespace(proc=SimpleNamespace(pid=4321, poll=Mock(return_value=None)),
            generation='private-native-generation', ready=ready)
        session._native_preparation_backend = backend
        service = ControlPlaneBrokerService(identity=f.identity, authority=f.authority, work_items=None,
            operator=SimpleNamespace(), audit=ControlPlaneBrokerAuditStore(f.sqlite), clock=lambda: self.now[0])
        service.assignment_reasoning = SimpleNamespace(procedures=self.fixture.env.owner)
        broker = self.broker = AssignmentBoundControlPlaneBroker(service, assignment=assignment,
            worker_id=f.procedure_worker.id, worker_actor=f.procedure_worker_actor,
            fence=assignment.fence, validator=session._validate_egress_state)
        session.control_plane_broker = broker
        await broker.start()
        self.addAsyncCleanup(broker.stop)
        self.manager = SimpleNamespace(get=lambda key: session if key == assignment.id else None)
        self.execution = object.__new__(TurnExecutionService)
        self.execution.session_manager = self.manager
        self.execution.session_managers = {}
        native = self.fixture.env.admission
        native.native_session_attestor = self.execution.attest_native_provider_preparation
        native.native_session_preparer = getattr(self.execution, 'prepare_native_provider_preparation', None)

    async def test_real_seal_preparation_allows_same_owner_renewal_before_finite_expiry(self):
        f = self.fixture.f
        owner = self.fixture.env.owner
        origin = self.now[0]
        now = self.now
        entered = threading.Event()
        release = threading.Event()
        probe_done = threading.Event()
        within_initial_admission = threading.local()
        observations = {}
        real_admit = owner.admit_initial_native_credentials
        real_resource_validation = self.session._validate_resource_bounds
        original_clock = owner.clock
        owner.clock = lambda: now[0]
        self.addCleanup(setattr, owner, 'clock', original_clock)
        assignment_id = f.native_assignment.id
        lock = f.workers.store._lock

        def renew(current):
            return f.workers.renew(f.procedure_worker.id, current.id,
                AssignmentRenewRequest(lease_token=current.lease.lease_token,
                    fence=current.fence, lease_seconds=120), actor=f.procedure_worker_actor)

        def observe_initial_admission(*args, **kwargs):
            within_initial_admission.active = True
            try:
                # The authoritative CAS callback is passed through unchanged.
                return real_admit(*args, **kwargs)
            finally:
                within_initial_admission.active = False

        def gated_resource_validation(*args, **kwargs):
            result = real_resource_validation(*args, **kwargs)
            if getattr(within_initial_admission, 'active', False) and not entered.is_set():
                # Gate this actual physical/session-attestation component. A
                # correct implementation may prepare its proof outside locks
                # while retaining cheap authoritative CAS checks inside.
                entered.set()
                if not release.wait(30):
                    raise AssertionError('Fixture cleanup did not release preparation')
            return result

        def heartbeat_and_renew():
            try:
                # Probe only chooses causal clock/release ordering. The actual
                # assertion concerns REAL heartbeat+renewal and finite expiry;
                # mutex availability alone is never the pass/fail oracle.
                acquired = lock.acquire(blocking=False)
                observations['mutex_available'] = acquired
                if acquired:
                    lock.release()
                probe_done.set()
                f.workers.heartbeat(f.procedure_worker.id,
                    WorkerHeartbeatRequest(version=f.procedure_worker.version), actor=f.procedure_worker_actor)
                observations['renewed'] = renew(f.workers.store.assignment(assignment_id))
            except BaseException as exc:
                observations['error'] = exc
                probe_done.set()

        admission = None
        renewal = threading.Thread(target=heartbeat_and_renew, name='fixture-real-seal-renewal')
        # Only this module's canonical service clock is controlled; production
        # authority uses its ordinary finite lease, token and fence checks.
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: now[0])), \
                patch.object(owner, 'admit_initial_native_credentials', side_effect=observe_initial_admission), \
                patch.object(self.session, '_validate_resource_bounds', side_effect=gated_resource_validation):
            initial = renew(f.workers.store.assignment(assignment_id))
            self.assertEqual(initial.lease.expires_at, origin + 120)
            try:
                admission = asyncio.create_task(self.fixture.env.admit())
                self.assertTrue(await asyncio.to_thread(entered.wait, 30), 'Actual seal session attestation was not reached')
                now[0] = origin + 30
                renewal.start()
                self.assertTrue(await asyncio.to_thread(probe_done.wait, 30), 'Original renewal did not enter')
                if observations.get('mutex_available') is False:
                    # Old path: the real renewal cannot pass this known-held
                    # mutex until validation returns. Advance its genuine
                    # finite authority clock, then release; never fake a grant.
                    now[0] = origin + 121
                    release.set()
                await asyncio.to_thread(renewal.join, 30)
                self.assertFalse(renewal.is_alive(), 'Fixture renewal thread did not settle')
                self.assertNotIn('error', observations,
                    'Real same-owner heartbeat/renewal lost finite custody while initial seal session attestation held its writer: '
                    + type(observations.get('error')).__name__)
                renewed = observations['renewed']
                self.assertEqual(renewed.lease.expires_at, origin + 150)
                self.assertEqual(renewed.lease.lease_token, initial.lease.lease_token)
                self.assertEqual(renewed.lease.acquired_at, initial.lease.acquired_at)
                self.assertEqual(renewed.fence, initial.fence)
                self.assertFalse(release.is_set())
                # The original finite lease is now expired. Only the real
                # successfully renewed SAME custody may allow the seal.
                now[0] = origin + 121
                self.assertLess(initial.lease.expires_at, now[0])
                self.assertGreater(renewed.lease.expires_at, now[0])
                release.set()
                result, _ = await asyncio.wait_for(asyncio.shield(admission), 30)
                self.assertTrue(result.admitted)
                current = f.workers.store.assignment(assignment_id)
                self.assertEqual(current.lease, renewed.lease)
                self.assertIsNotNone(current.native_credential_custody.admitted_at)
                self.assertEqual(current.native_credential_custody.procedures, ())
                self.assertEqual(f.ops.calls, [])
            finally:
                release.set()
                if renewal.ident is not None:
                    await asyncio.to_thread(renewal.join, 30)
                    self.assertFalse(renewal.is_alive(), 'Original renewal must join before fixture exits')
                if admission is not None:
                    # Always join the actual original admission before clock,
                    # callback patches and canonical fixture resources expire.
                    await asyncio.wait_for(asyncio.gather(admission, return_exceptions=True), 30)

    async def test_prepared_observation_refuses_changed_process_session_broker_and_backend(self):
        current = self.fixture.current()
        session = self.session
        for changed in ('generation', 'process', 'session', 'broker', 'broker-validator', 'backend', 'stopping'):
            with self.subTest(changed=changed):
                proof = self.execution.prepare_native_provider_preparation(current, self.fixture.env.owner)
                runtime, broker, backend = session.runtime, session.control_plane_broker, session.local_worker.backend
                original_process, original_generation = runtime.proc, runtime.generation
                original_get = self.manager.get
                original_validator = broker.validator
                try:
                    if changed == 'generation':
                        runtime.generation = 'changed-generation'
                    elif changed == 'process':
                        runtime.proc = SimpleNamespace(pid=4321, poll=Mock(return_value=None))
                    elif changed == 'session':
                        self.manager.get = lambda _key: object()
                    elif changed == 'broker':
                        session.control_plane_broker = object()
                    elif changed == 'broker-validator':
                        broker.validator = lambda: current
                    elif changed == 'backend':
                        session.local_worker.backend = object()
                    else:
                        session._stopping = True
                    with self.assertRaises((ValueError, AssignmentBoundAgentProcessSessionStaleError)):
                        proof.validate(current, self.fixture.env.owner)
                    self.fixture.assert_unsealed()
                finally:
                    runtime.proc, runtime.generation = original_process, original_generation
                    session.control_plane_broker, session.local_worker.backend = broker, backend
                    self.manager.get = original_get
                    broker.validator = original_validator
                    session._stopping = False

    async def test_prepared_observation_refuses_revoked_lease_deadline_and_custody(self):
        f = self.fixture.f
        original = self.fixture.current()
        for changed in ('lease', 'deadline', 'token', 'fence', 'definition'):
            with self.subTest(changed=changed):
                proof = self.execution.prepare_native_provider_preparation(original, self.fixture.env.owner)
                if changed == 'lease':
                    updates = {'lease': original.lease.model_copy(update={'expires_at': self.now[0]-1})}
                elif changed == 'deadline':
                    updates = {'deadline_at': self.now[0]-1}
                elif changed == 'token':
                    updates = {'lease': original.lease.model_copy(update={'lease_token': 'private-changed-token'})}
                elif changed == 'fence':
                    updates = {'fence': original.fence+1,
                        'lease': original.lease.model_copy(update={'fence': original.fence+1})}
                else:
                    updates = {'execution_profile_definition': original.execution_profile_definition.model_copy(
                        update={'checksum': 'f'*64})}
                changed_assignment = original.model_copy(update=updates)
                def put(value):
                    return f.workers.store.update_assignment(f.procedure_worker.id, original.id,
                        lambda state: state.model_copy(update={'assignments': [value]}))
                try:
                    put(changed_assignment)
                    with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
                        proof.validate(changed_assignment, self.fixture.env.owner)
                    self.fixture.assert_unsealed()
                finally:
                    put(original)

    async def test_custom_attestor_keeps_its_original_guarded_validation(self):
        owner = self.fixture.env.owner
        native = self.fixture.env.admission
        attest, admit = native.native_session_attestor, owner.admit_initial_native_credentials
        initial = threading.local()
        refused = []
        def custom_attestor(*args):
            result = attest(*args)
            if getattr(initial, 'active', False):
                refused.append(True)
                raise ExecutionPrestartRefusal(status_code=409, detail={'code': 'custom_current_policy_refusal'})
            return result
        def initial_admit(*args, **kwargs):
            initial.active = True
            try:
                return admit(*args, **kwargs)
            finally:
                initial.active = False
        native.native_session_attestor = custom_attestor
        try:
            with patch.object(owner, 'admit_initial_native_credentials', side_effect=initial_admit):
                with self.assertRaises(ExecutionPrestartRefusal):
                    await self.fixture.env.admit()
            self.assertEqual(refused, [True])
            self.fixture.assert_unsealed()
        finally:
            native.native_session_attestor = attest

    async def test_prepared_observation_refuses_revoked_canonical_worker(self):
        current = self.fixture.current()
        proof = self.execution.prepare_native_provider_preparation(current, self.fixture.env.owner)
        f = self.fixture.f
        f.workers.set_lifecycle(f.procedure_worker.id, WorkerLifecycle.REVOKED,
            actor=f.actor, reason='Private worker revocation after physical observation')
        with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
            proof.validate(current, self.fixture.env.owner)
        self.fixture.assert_unsealed()

    async def test_custom_broker_validator_keeps_its_current_policy_guard(self):
        current = self.fixture.current()
        original = self.broker.validator
        allowed = [True]
        calls = []
        def custom():
            calls.append(True)
            if not allowed[0]:
                raise ValueError('Private current broker policy revoked')
            return original()
        self.broker.validator = custom
        try:
            proof = self.execution.prepare_native_provider_preparation(current, self.fixture.env.owner)
            before = len(calls)
            allowed[0] = False
            with self.assertRaises(ControlPlaneBrokerDeniedError):
                proof.validate(current, self.fixture.env.owner)
            self.assertGreater(len(calls), before)
            self.fixture.assert_unsealed()
        finally:
            self.broker.validator = original

    async def test_custom_session_attestor_revocation_after_observation_refuses_guarded_seal(self):
        original = self.session.attest_native_provider_preparation
        prepare = self.session.prepare_native_provider_preparation
        allowed = [True]
        denied = []
        observed = []
        def custom_session_attestor(session, expected, procedures):
            current = original(expected, procedures)
            if not allowed[0]:
                denied.append(True)
                raise AssignmentBoundAgentProcessSessionStaleError('Private current session policy revoked')
            return current
        self.session.attest_native_provider_preparation = MethodType(custom_session_attestor, self.session)
        def revoke_after_observation(*args):
            proof = prepare(*args)
            observed.append(True)
            allowed[0] = False
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=revoke_after_observation):
                with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
                    await self.fixture.env.admit()
            self.assertEqual(observed, [True])
            self.assertEqual(denied, [True])
            self.fixture.assert_unsealed()
        finally:
            self.session.attest_native_provider_preparation = original

    async def test_session_attestor_identity_change_after_observation_refuses_guarded_seal(self):
        original = self.session.attest_native_provider_preparation
        prepare = self.session.prepare_native_provider_preparation
        observed = []
        def replacement(session, expected, procedures):
            return original(expected, procedures)
        def replace_after_observation(*args):
            proof = prepare(*args)
            self.session.attest_native_provider_preparation = MethodType(replacement, self.session)
            observed.append(True)
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=replace_after_observation):
                with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
                    await self.fixture.env.admit()
            self.assertEqual(observed, [True])
            self.fixture.assert_unsealed()
        finally:
            self.session.attest_native_provider_preparation = original

    async def test_custom_full_broker_guard_revocation_after_observation_refuses_seal(self):
        original = self.broker._validate_current
        prepare = self.session.prepare_native_provider_preparation
        allowed = [True]
        denied = []
        observed = []
        def custom_broker_guard(broker):
            current = original()
            if not allowed[0]:
                denied.append(True)
                raise ControlPlaneBrokerDeniedError('Private full broker policy revoked')
            return current
        self.broker._validate_current = MethodType(custom_broker_guard, self.broker)
        def revoke_after_observation(*args):
            proof = prepare(*args)
            allowed[0] = False
            observed.append(True)
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=revoke_after_observation):
                with self.assertRaises(ControlPlaneBrokerDeniedError):
                    await self.fixture.env.admit()
            self.assertEqual(observed, [True])
            self.assertEqual(denied, [True])
            self.fixture.assert_unsealed()
        finally:
            self.broker._validate_current = original

    async def test_full_broker_guard_identity_change_after_observation_refuses_seal(self):
        original = self.broker._validate_current
        prepare = self.session.prepare_native_provider_preparation
        observed = []
        def replacement(broker):
            return original()
        def replace_after_observation(*args):
            proof = prepare(*args)
            self.broker._validate_current = MethodType(replacement, self.broker)
            observed.append(True)
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=replace_after_observation):
                with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
                    await self.fixture.env.admit()
            self.assertEqual(observed, [True])
            self.fixture.assert_unsealed()
        finally:
            self.broker._validate_current = original

    async def test_attestor_replacement_after_physical_observation_cannot_seal(self):
        native = self.fixture.env.admission
        attestor = native.native_session_attestor
        prepare = self.session.prepare_native_provider_preparation
        observed = []
        def changed(*args):
            proof = prepare(*args)
            native.native_session_attestor = lambda *_args: None
            observed.append(True)
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed):
                with self.assertRaises(ValueError):
                    await self.fixture.env.admit()
            self.assertEqual(observed, [True])
            self.fixture.assert_unsealed()
        finally:
            native.native_session_attestor = attestor


class SelectedNativeSealRenewalCouplingTests(NativeSealRenewalCouplingTests):
    async def asyncSetUp(self):
        custody = NativeProcedureCredentialCustodyTests()
        await custody.asyncSetUp()
        self.addAsyncCleanup(custody.asyncTearDown)
        custody.bootstrap()
        f = custody.f
        f.procedure_worker, f.procedure_worker_actor = custody.worker, custody.worker_actor
        f.native_assignment = custody.assignment
        self.fixture = SimpleNamespace(f=f, current=custody.current,
            env=SimpleNamespace(owner=custody.owner, admission=custody.admission,
                reference=custody.fixture.reference, admit=custody.admit))
        await self.install_real_owners()

    async def test_changed_selected_candidate_after_preparation_never_seals_credentials(self):
        native = self.fixture.env.admission
        reference = self.fixture.env.reference
        prepare = self.session.prepare_native_provider_preparation
        observed = []
        def quarantine_after_actual_observation(*args):
            proof = prepare(*args)
            self.fixture.env.owner.skills.definitions.quarantine(reference.record_id,
                actor=self.fixture.f.actor.identity_id, reason='Private candidate revoked after physical observation')
            observed.append(True)
            return proof
        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=quarantine_after_actual_observation):
            with self.assertRaises(ExecutionPrestartRefusal):
                await self.fixture.env.admit()
            self.assertEqual(observed, [True])
            current = self.fixture.current()
            self.assertIsNone(current.native_credential_custody.admitted_at)
            self.assertEqual(current.secret_refs, ())
            self.assertEqual(self.fixture.f.ops.calls, [])


class CanonicalPreparedSealQueuedIntegrationTests(NativeSealRenewalCouplingTests):
    async def asyncSetUp(self):
        # Register every real owner cleanup on this case, not on an unrun case.
        await super().asyncSetUp()
        from unittest.mock import AsyncMock
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.storage.execution_preflight import ExecutionPreflightStore
        from tests.test_turn_execution import _Host
        self.host = _Host()
        self.host._project = lambda _id: self.fixture.f.project
        self.queued_runtime = TurnExecutionService(self.host)
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.fixture.f.sqlite))
        self.queued_runtime.queued_preflight = self.preflight
        self.queued_runtime.schedule_queue_drain = Mock()
        self.native_write = AsyncMock(return_value={'turn': {'id': 'private-genuine-native-writer'}})
        self.prepare_calls = []
        self.prepared_observations = []

        async def admitted_start(thread_id, **options):
            self.assertEqual(thread_id, 'native-provider-thread')
            self.assertEqual(options['execution_id'], self.fixture.f.native_assignment.execution_id)
            # Actual admission and paired real session owner precede this spy.
            await self.fixture.env.admit()
            return await self.native_write(thread_id, **options)

        self.queued_runtime.start_thread_turn_now = admitted_start
        self.queued = self.queued_runtime.enqueue_turn(thread_id='native-provider-thread',
            project_id=self.fixture.f.project.id, message='Preserve this original architecture request',
            execution_id=self.fixture.f.native_assignment.execution_id, source='web')
        # The live queue object is mutable: drain increments its scheduling
        # attempt count after retaining the immutable original snapshot.
        self.original_queued_snapshot = self.queued.model_dump(mode='json')

    def retained_original(self):
        return self.preflight.store.get('preflight-queued-' + self.queued.id)

    def assert_preserved_original_without_input(self, expected_status, expected_code):
        retained = self.retained_original()
        self.assertEqual(retained.status, expected_status)
        self.assertEqual(retained.blockers[0].code, expected_code)
        self.assertEqual(retained.execution_id, self.queued.execution_id)
        self.assertEqual(retained.queued_turn_snapshot, self.original_queued_snapshot)
        mutable_queue = self.queued.model_dump(mode='json')
        self.assertEqual(mutable_queue['attempts'], self.original_queued_snapshot['attempts'] + 1)
        self.assertEqual(dict(mutable_queue, attempts=self.original_queued_snapshot['attempts']),
            self.original_queued_snapshot)
        self.assertEqual(retained.attempt_number, 1)
        self.assertLessEqual(retained.created_at, retained.updated_at)
        self.assertIsNotNone(retained.queued_admission_claim_id)
        self.assertIsNone(retained.started_at)
        self.assertIsNone(retained.queued_start_receipt)
        self.assertIsNone(retained.queued_start_evidence)
        self.native_write.assert_not_awaited()
        self.queued_runtime.schedule_queue_drain.assert_not_called()
        self.fixture.assert_unsealed()
        self.assertEqual(self.prepare_calls, [self.fixture.f.native_assignment.id])
        return retained

    async def test_canonical_prepared_session_catalog_change_is_known_preinput_refusal(self):
        from codex_web.skills import SkillCreate, SkillPublish
        owner = self.fixture.env.owner
        admit = owner.admit_initial_native_credentials
        prepare = self.session.prepare_native_provider_preparation

        def actual_preparation(*args):
            self.prepare_calls.append(args[0].id)
            proof = prepare(*args)
            self.prepared_observations.append(proof)
            return proof

        def publish_before_cas(*args, **kwargs):
            f = self.fixture.f
            created = f.skills.create(SkillCreate(skill_id='prepared-session-queue-new-candidate',
                name='New current candidate before initial seal',
                instructions='Use the current canonical procedure',
                operational_procedure=self.fixture.env.content), actor=f.actor)
            f.skills.publish(created['skillId'], created['recordId'], SkillPublish(), actor=f.actor)
            # Keep canonical pair and forward its real preparation unchanged.
            return admit(*args, **kwargs)

        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=actual_preparation), \
                patch.object(owner, 'admit_initial_native_credentials', side_effect=publish_before_cas):
            await self.queued_runtime.drain_thread_queue(self.queued.thread_id)
        self.assertEqual(len(self.prepared_observations), 1)
        self.assert_preserved_original_without_input('blocked', 'procedure_native_preparation_changed')
        notices = [event for event in self.host.events if event['type'] == 'queued_turn_retained_for_recovery']
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]['known_unstarted'])

    async def test_canonical_prepared_session_physical_failure_remains_unknown(self):
        prepare = self.session.prepare_native_provider_preparation
        backend = self.session.local_worker.backend

        def unavailable_physical_observation(*args):
            self.prepare_calls.append(args[0].id)
            # Actual backend readiness fails inside the real owning preparer;
            # no fabricated refusal detail or prepared proof is returned.
            with patch.object(backend, 'probe', side_effect=RuntimeError('Private physical observation unavailable')):
                return prepare(*args)

        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=unavailable_physical_observation):
            await self.queued_runtime.drain_thread_queue(self.queued.thread_id)
        retained = self.assert_preserved_original_without_input('reconciliation_required', 'native_start_outcome_unknown')
        self.assertFalse(self.preflight.public(retained, actor=self.fixture.f.actor)['can_retry'])
        before = retained.model_dump(mode='json')
        await self.queued_runtime.drain_thread_queue(self.queued.thread_id)
        self.assertEqual(self.retained_original().model_dump(mode='json'), before)
        self.native_write.assert_not_awaited()
        self.assertEqual(self.prepare_calls, [self.fixture.f.native_assignment.id])


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(case(name) for case in (
        NativeSealRenewalCouplingTests, SelectedNativeSealRenewalCouplingTests,
        CanonicalPreparedSealQueuedIntegrationTests)
        for name, method in case.__dict__.items() if name.startswith('test_') and callable(method))
