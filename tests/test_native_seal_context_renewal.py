"""Canonical Project metadata must not trap physical renewal in the seal writer.

The first outside read demonstrates real competing renewal. A second gate is
used only if admission actually reads Projects while owning the worker writer.
Events determine ordering; host elapsed time is not the lease authority clock.
"""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_workers import AssignmentRenewRequest, WorkerHeartbeatRequest
from codex_web.services import execution_workers as worker_module
from codex_web.runtime.execution import ExecutionPrestartRefusal
from tests.test_native_seal_metadata_renewal import SelectedNativeSealIdentityRenewalTests


class NativeSealProjectContextRenewalTests(SelectedNativeSealIdentityRenewalTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.projects = self.fixture.f.skills.procedure_scope_actor.projects
        self.fixture.env.admission.discovery.projects = self.projects
        current = self.fixture.current()
        self.pending_custody = current.native_credential_custody.model_dump(mode='json')
        self.pending_secrets = current.secret_refs

    def _assert_pending_custody_preserved(self):
        current = self.fixture.current()
        self.assertEqual(current.native_credential_custody.model_dump(mode='json'), self.pending_custody)
        self.assertEqual(current.secret_refs, self.pending_secrets)

    async def test_project_metadata_read_cannot_trap_actual_renewal_in_seal_writer(self):
        f = self.fixture.f
        projects = f.skills.procedure_scope_actor.projects
        self.fixture.env.admission.discovery.projects = projects
        self.assertTrue(f.skills.can_observe_metadata_reads(f.identity, f.resources))
        store, lock = f.sqlite, f.workers.store._lock
        raw_get = store.get
        origin = self.now[0]
        outside_entered, outside_release = threading.Event(), threading.Event()
        writer_entered, writer_release = threading.Event(), threading.Event()
        threads, observers = [], []
        writer_reads = []

        def metadata_read(namespace):
            result = raw_get(namespace)
            if namespace != projects.repository.namespace:
                return result
            if threading.current_thread() in threads:
                return result
            if lock._is_owned():
                writer_reads.append(namespace)
                if not writer_entered.is_set():
                    writer_entered.set()
                    if not writer_release.wait(30):
                        raise AssertionError('Fixture did not release writer Project read')
            elif not outside_entered.is_set():
                outside_entered.set()
                if not outside_release.wait(30):
                    raise AssertionError('Fixture did not release outside Project read')
            return result

        def renew():
            f.workers.heartbeat(f.procedure_worker.id,
                WorkerHeartbeatRequest(version=f.procedure_worker.version), actor=f.procedure_worker_actor)
            current = self.fixture.current()
            return f.workers.renew(f.procedure_worker.id, current.id,
                AssignmentRenewRequest(lease_token=current.lease.lease_token,
                    fence=current.fence, lease_seconds=120), actor=f.procedure_worker_actor)

        async def competing_renewal(*, writer):
            outcome, probed = {}, threading.Event()
            def run():
                try:
                    available = lock.acquire(blocking=False)
                    outcome['writer_available'] = available
                    if available:
                        lock.release()
                    probed.set()
                    outcome['assignment'] = renew()
                except BaseException as error:
                    outcome['error'] = error
                    probed.set()
            thread = threading.Thread(target=run, name='private-context-renewal')
            threads.append(thread)
            thread.start()
            self.assertTrue(await asyncio.to_thread(probed.wait, 30))
            if writer and not outcome['writer_available']:
                # The previous genuine renewal expires at origin+150. Only
                # this causally held canonical writer prevents timely renewal.
                self.now[0] = origin + 151
                writer_release.set()
            await asyncio.to_thread(thread.join, 30)
            self.assertFalse(thread.is_alive())
            self.assertNotIn('error', outcome,
                'Canonical Project metadata trapped finite physical renewal: '
                + type(outcome.get('error')).__name__)
            return outcome['assignment']

        admission = None
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: self.now[0])):
            initial = renew()
            self.assertEqual(initial.lease.expires_at, origin + 120)
            try:
                with patch.object(store, 'get', side_effect=metadata_read):
                    admission = asyncio.create_task(self.fixture.env.admit())
                    outside = asyncio.create_task(asyncio.to_thread(outside_entered.wait, 30))
                    observers.append(outside)
                    await asyncio.wait((admission, outside), return_when=asyncio.FIRST_COMPLETED)
                    self.assertTrue(outside_entered.is_set(), 'Actual canonical Project read was not reached')
                    self.now[0] = origin + 30
                    first = await competing_renewal(writer=False)
                    self.assertEqual(first.lease.expires_at, origin + 150)
                    self.assertEqual(first.lease.lease_token, initial.lease.lease_token)
                    self.assertEqual(first.fence, initial.fence)
                    outside_release.set()
                    inside = asyncio.create_task(asyncio.to_thread(writer_entered.wait, 30))
                    observers.append(inside)
                    await asyncio.wait((admission, inside), return_when=asyncio.FIRST_COMPLETED)
                    if writer_entered.is_set():
                        self.now[0] = origin + 31
                        await competing_renewal(writer=True)
                    result, _ = await asyncio.wait_for(asyncio.shield(admission), 30)
                    self.assertTrue(result.admitted)
                    self.assertEqual(writer_reads, [], 'Project metadata still read under physical seal writer')
                    self.assertEqual(f.ops.calls, [])
            finally:
                outside_release.set(); writer_release.set()
                outside_entered.set(); writer_entered.set()
                for thread in threads:
                    await asyncio.to_thread(thread.join, 30)
                    self.assertFalse(thread.is_alive())
                if admission is not None:
                    await asyncio.wait_for(asyncio.gather(admission, return_exceptions=True), 30)
                if observers:
                    await asyncio.wait_for(asyncio.gather(*observers, return_exceptions=True), 30)

    async def test_project_change_after_physical_preparation_refuses_before_updater(self):
        prepare = self.session.prepare_native_provider_preparation
        transition = self.fixture.f.workers.store.transition_assignment
        updates = []
        def observed_transition(worker, assignment, updater, **kwargs):
            def apply(state):
                updates.append(True)
                return updater(state)
            return transition(worker, assignment, apply, **kwargs)
        def changed(*args):
            proof = prepare(*args)
            self.projects.repository.save([self.fixture.f.project.model_copy(
                update={'name':'Changed after physical preparation'})])
            return proof
        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed), \
                patch.object(self.fixture.f.workers.store, 'transition_assignment', observed_transition):
            with self.assertRaises(ExecutionPrestartRefusal) as refused:
                await self.fixture.env.admit()
        self.assertEqual(refused.exception.detail['code'], 'procedure_unsupported_enforcement')
        self.assertIn({'stage':'fallback', 'exception_type':'ValueError'},
            refused.exception.detail['native_preparation_diagnostics'])
        self.assertEqual(updates, [])
        self._assert_pending_custody_preserved()

    async def test_changed_project_lookup_hook_is_not_cached_authority(self):
        prepare = self.session.prepare_native_provider_preparation
        original = self.projects.get
        substituted = []
        def get(*args, **kwargs):
            substituted.append(True)
            return original(*args, **kwargs)
        def changed(*args):
            proof = prepare(*args)
            self.projects.get = get
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed):
                with self.assertRaises(ExecutionPrestartRefusal) as refused:
                    await self.fixture.env.admit()
            self.assertEqual(refused.exception.detail['code'], 'procedure_credential_metadata_owner_changed')
            self.assertEqual(substituted, [])
            self._assert_pending_custody_preserved()
        finally:
            self.projects.get = original

    async def test_custom_observation_hooks_cannot_opt_themselves_in(self):
        repository = self.projects.repository
        observed = []
        def fake_observe(callback):
            observed.append(True)
            raise AssertionError('Custom observation owner must not be invoked')
        with patch.object(repository, 'can_observe_reads', return_value=True), \
                patch.object(repository, 'observe_reads', side_effect=fake_observe):
            self.assertFalse(self.fixture.env.owner.can_observe_initial_seal_projects())
            result, _ = await self.fixture.env.admit()
        self.assertTrue(result.admitted)
        self.assertEqual(observed, [])

    async def test_foreign_observation_is_rejected_without_running_callback(self):
        calls = []
        fake = SimpleNamespace(repository=self.projects.repository,
            require_current=lambda *_: calls.append('fake guard'))
        from codex_web.services.procedure_execution import ProcedureExecutionError
        with self.assertRaises(ProcedureExecutionError) as refused:
            self.fixture.env.owner.admit_initial_native_credentials(self.fixture.current(),
                (self.fixture.env.reference,), self.fixture.f.actor,
                lambda *_: calls.append('updater'), _projects=fake)
        self.assertEqual(refused.exception.code, 'procedure_credential_metadata_owner_changed')
        self.assertEqual(calls, [])
        self._assert_pending_custody_preserved()


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(NativeSealProjectContextRenewalTests(name)
        for name in NativeSealProjectContextRenewalTests.__dict__ if name.startswith('test_'))
