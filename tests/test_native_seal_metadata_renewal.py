"""Gate canonical raw metadata I/O, not a replaced policy/security validator."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from codex_web.execution_workers import AssignmentRenewRequest, WorkerHeartbeatRequest
from codex_web.services import execution_workers as worker_module
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionStaleError
from codex_web.runtime.execution import ExecutionPrestartRefusal
from tests.test_native_seal_renewal_coupling import (
    NativeSealRenewalCouplingTests, SelectedNativeSealRenewalCouplingTests)


class NativeSealMetadataRenewalTests(NativeSealRenewalCouplingTests):
    async def test_canonical_definition_reader_does_not_trap_finite_renewal_in_seal_writer(self):
        f = self.fixture.f
        store = f.sqlite
        raw_get = store.get
        lock = f.workers.store._lock
        origin = self.now[0]
        entered, release, probed = threading.Event(), threading.Event(), threading.Event()
        observations = {'writer_metadata_reads': 0}
        metadata_namespace = getattr(self, 'canonical_metadata_namespace', f.definition_registry.store.namespace)

        def metadata_read(namespace):
            result = raw_get(namespace)
            # Instrument only the canonical storage boundary. Identity/Definition,
            # Skill, admission and security methods retain their exact owners.
            if namespace == metadata_namespace:
                # The competing heartbeat/renewal legitimately validates its
                # current Identity under its own worker writer. Exclude ONLY
                # that actual Thread object, not arbitrary names/executors.
                # All admission-side and unexpected-thread reads still count.
                observations['writer_metadata_reads'] += int(
                    lock._is_owned() and threading.current_thread() is not renewal)
                # Hold an actual canonical outside read too: green must prove
                # real competing renewal, never merely skip the old branch.
                if not entered.is_set():
                    entered.set()
                    if not release.wait(30):
                        raise AssertionError('Fixture metadata reader was not released')
            return result

        def renew():
            try:
                available = lock.acquire(blocking=False)
                observations['mutex_available'] = available
                if available:
                    lock.release()
                probed.set()
                f.workers.heartbeat(f.procedure_worker.id,
                    WorkerHeartbeatRequest(version=f.procedure_worker.version), actor=f.procedure_worker_actor)
                current = f.workers.store.assignment(f.native_assignment.id)
                observations['renewed'] = f.workers.renew(f.procedure_worker.id, current.id,
                    AssignmentRenewRequest(lease_token=current.lease.lease_token,
                        fence=current.fence, lease_seconds=120), actor=f.procedure_worker_actor)
            except BaseException as exc:
                observations['error'] = exc
                probed.set()

        renewal = threading.Thread(target=renew, name='private-canonical-metadata-renewal')
        admission = observer = None
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: self.now[0])):
            current = self.fixture.current()
            initial = f.workers.renew(f.procedure_worker.id, current.id,
                AssignmentRenewRequest(lease_token=current.lease.lease_token, fence=current.fence,
                    lease_seconds=120), actor=f.procedure_worker_actor)
            try:
                with patch.object(store, 'get', side_effect=metadata_read):
                    admission = asyncio.create_task(self.fixture.env.admit())
                    observer = asyncio.create_task(asyncio.to_thread(entered.wait, 30))
                    await asyncio.wait((admission, observer), return_when=asyncio.FIRST_COMPLETED)
                    self.assertTrue(entered.is_set(), 'Canonical metadata read did not occur')
                    if entered.is_set():
                        self.now[0] = origin + 30
                        renewal.start()
                        self.assertTrue(await asyncio.to_thread(probed.wait, 30))
                        if not observations['mutex_available']:
                            # The exact known-held writer causally prevents
                            # renewal; advance the real finite authority clock.
                            self.now[0] = origin + 121
                            release.set()
                        await asyncio.to_thread(renewal.join, 30)
                        self.assertFalse(renewal.is_alive())
                        self.assertNotIn('error', observations,
                            'Canonical metadata read trapped actual finite heartbeat/renewal: '
                            + type(observations.get('error')).__name__)
                        renewed = observations['renewed']
                        self.assertEqual(renewed.lease.expires_at, origin + 150)
                        self.assertEqual(renewed.lease.lease_token, initial.lease.lease_token)
                        self.assertEqual(renewed.fence, initial.fence)
                        self.assertFalse(release.is_set())
                    release.set()
                    result, _ = await asyncio.wait_for(asyncio.shield(admission), 30)
                    self.assertTrue(result.admitted)
                    self.assertEqual(observations['writer_metadata_reads'], 0,
                        'Canonical metadata reads still occur under the seal writer')
                    self.assertIn('renewed', observations)
                    self.assertEqual(f.ops.calls, [])
                    # Settle the observer even if no writer-owned metadata I/O
                    # remains; no detached executor survives fixture cleanup.
                    entered.set()
                    await observer
            finally:
                release.set(); entered.set()
                if renewal.ident is not None:
                    await asyncio.to_thread(renewal.join, 30)
                    self.assertFalse(renewal.is_alive())
                if admission is not None:
                    await asyncio.wait_for(asyncio.gather(admission, return_exceptions=True), 30)
                if observer is not None:
                    await asyncio.wait_for(asyncio.gather(observer, return_exceptions=True), 30)

    async def test_definition_change_after_actual_preparation_does_not_use_stale_data(self):
        prepare = self.session.prepare_native_provider_preparation
        calls = []
        def changed(*args):
            proof = prepare(*args)
            calls.append(True)
            self.fixture.f.definition_registry.quarantine(self.fixture.env.reference.record_id,
                actor=self.fixture.f.actor.identity_id, reason='Changed after actual metadata observation')
            return proof
        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed):
            with self.assertRaises(ValueError):
                await self.fixture.env.admit()
        self.assertEqual(calls, [True])
        self.fixture.assert_unsealed()

    async def test_expired_real_lease_after_preparation_is_not_authorized_by_cached_data(self):
        prepare = self.session.prepare_native_provider_preparation
        calls = []
        def expired(*args):
            proof = prepare(*args)
            calls.append(True)
            self.now[0] = self.fixture.current().lease.expires_at + 1
            return proof
        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=expired):
            with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
                await self.fixture.env.admit()
        self.assertEqual(calls, [True])
        self.fixture.assert_unsealed()

    async def test_original_three_positional_projection_override_keeps_guarded_fallback(self):
        owner = self.fixture.env.owner
        original = owner._native_credential_projection
        calls = []
        def projection(assignment, selected, actor):
            inside = self.fixture.f.workers.store._lock._is_owned()
            calls.append(inside)
            if inside:
                raise ValueError('Actual current custom projection refusal')
            return original(assignment, selected, actor)
        with patch.object(owner, '_native_credential_projection', side_effect=projection):
            with self.assertRaisesRegex(ValueError, 'Actual current custom projection refusal'):
                await self.fixture.env.admit()
        self.assertIn(False, calls)
        self.assertIn(True, calls)
        self.fixture.assert_unsealed()

    async def test_projection_hook_changed_after_preparation_refuses_before_cached_use(self):
        owner = self.fixture.env.owner
        prepare = self.session.prepare_native_provider_preparation
        original = owner._native_credential_projection
        substituted = []
        def projection(*args, **kwargs):
            substituted.append(True)
            return original(*args, **kwargs)
        def changed(*args):
            proof = prepare(*args)
            owner._native_credential_projection = projection
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed):
                with self.assertRaises(ExecutionPrestartRefusal) as caught:
                    await self.fixture.env.admit()
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(caught.exception.detail['code'], 'procedure_credential_metadata_owner_changed')
            self.assertEqual(substituted, [])
            self.fixture.assert_unsealed()
        finally:
            owner._native_credential_projection = original


class SelectedNativeSealMetadataRenewalTests(SelectedNativeSealRenewalCouplingTests):
    test_selected_definition_reader_does_not_trap_finite_renewal_in_seal_writer = (
        NativeSealMetadataRenewalTests.test_canonical_definition_reader_does_not_trap_finite_renewal_in_seal_writer)


class SelectedNativeSealIdentityRenewalTests(SelectedNativeSealRenewalCouplingTests):
    # The real canonical Identity owner remains installed. Only its underlying
    # storage read is observed; a prepared Definition view cannot substitute it.
    canonical_metadata_namespace = 'identity_state'

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from codex_web.services.procedure_scope_actor import ProcedureDefinitionScopeActor
        from codex_web.services.projects import ProjectService
        from codex_web.storage.projects import ProjectRepository
        from codex_web.workspaces import WorkspaceMapper
        f = self.fixture.f
        root = Path(f.temp.name)
        projects = ProjectService(ProjectRepository(root/'metadata-projects.json',
            store=f.sqlite, workspace_mapper=WorkspaceMapper(root=root)))
        projects.repository.save([f.project])
        # The inherited publishing fixture uses a custom lambda. Real metadata
        # observations deliberately opt out for that owner; compose production's
        # actual scope resolver before asserting any Identity-I/O optimization.
        f.skills.procedure_scope_actor = ProcedureDefinitionScopeActor(f.identity, projects)
        self.assertTrue(f.skills.can_observe_metadata_reads(f.identity, f.resources))
    test_selected_identity_reader_does_not_trap_finite_renewal_in_seal_writer = (
        NativeSealMetadataRenewalTests.test_canonical_definition_reader_does_not_trap_finite_renewal_in_seal_writer)

    async def disable_after_observation(self, *, worker):
        prepare = self.session.prepare_native_provider_preparation
        f = self.fixture.f
        observed = []
        updater_calls = []
        transition = f.workers.store.transition_assignment
        def traced_transition(worker_id, assignment_id, updater, **options):
            def apply(state):
                updater_calls.append(True)
                return updater(state)
            return transition(worker_id, assignment_id, apply, **options)
        def changed(*args):
            proof = prepare(*args)
            identity_id = f.procedure_worker.service_identity_id if worker else self.fixture.current().created_by
            def disable(state):
                members = state.services if worker else state.humans
                selected = next(item for item in members if item.id == identity_id)
                selected.disabled_at = self.now[0]
                return state
            f.identity.store.update(disable)
            observed.append(identity_id)
            return proof
        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed), \
                patch.object(f.workers.store, 'transition_assignment', traced_transition):
            with self.assertRaises(ExecutionPrestartRefusal):
                await self.fixture.env.admit()
        self.assertEqual(len(observed), 1)
        self.assertEqual(updater_calls, [], 'Changed raw Identity reached credential updater')
        self.assertIsNone(self.fixture.current().native_credential_custody.admitted_at)
        self.assertEqual(self.fixture.current().secret_refs, ())
        self.assertEqual(f.ops.calls, [])

    async def test_requester_disabled_after_observation_refuses_initial_seal(self):
        await self.disable_after_observation(worker=False)

    async def test_worker_disabled_after_observation_refuses_initial_seal(self):
        await self.disable_after_observation(worker=True)

    async def test_custom_identity_reader_keeps_current_guarded_fallback(self):
        f = self.fixture.f
        original = f.identity.actor_for_identity
        inside = []
        def custom(identity_id, *, scope):
            guarded = f.workers.store._lock._is_owned()
            inside.append(guarded)
            if guarded:
                raise ValueError('Private custom Identity refusal')
            return original(identity_id, scope=scope)
        with patch.object(f.identity, 'actor_for_identity', custom):
            with self.assertRaises(ExecutionPrestartRefusal):
                await self.fixture.env.admit()
        self.assertIn(False, inside)
        self.assertIn(True, inside)
        self.assertIsNone(self.fixture.current().native_credential_custody.admitted_at)
        self.assertEqual(f.ops.calls, [])

    async def test_identity_hook_changed_after_preparation_refuses_cached_use(self):
        f = self.fixture.f
        prepare = self.session.prepare_native_provider_preparation
        original = f.identity.actor_for_identity
        substituted = []
        def custom(identity_id, *, scope):
            substituted.append(identity_id)
            return original(identity_id, scope=scope)
        def changed(*args):
            proof = prepare(*args)
            f.identity.actor_for_identity = custom
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed):
                with self.assertRaises(ExecutionPrestartRefusal) as caught:
                    await self.fixture.env.admit()
            self.assertEqual(caught.exception.detail['code'], 'procedure_credential_metadata_owner_changed')
            self.assertEqual(substituted, [])
            self.assertIsNone(self.fixture.current().native_credential_custody.admitted_at)
            self.assertEqual(f.ops.calls, [])
        finally:
            f.identity.actor_for_identity = original


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(case(name) for case in (
        NativeSealMetadataRenewalTests, SelectedNativeSealMetadataRenewalTests, SelectedNativeSealIdentityRenewalTests)
        for name in case.__dict__ if name.startswith('test_'))
