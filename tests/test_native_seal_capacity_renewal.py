"""Canonical capacity metadata must not trap physical renewal in the seal writer.

The first outside read demonstrates real competing renewal. A second gate is
used only if admission actually reads Configuration while owning the worker writer.
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
from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest, ConfigurationScope
from codex_web.services.configuration import ConfigurationService
from codex_web.services.procedure_configuration import (
    PROCEDURE_CONTEXT_CHARACTERS, ProcedureContextCapacityResolver, install_procedure_configuration)
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from tests.test_native_seal_metadata_renewal import SelectedNativeSealIdentityRenewalTests


class NativeSealCapacityRenewalTests(SelectedNativeSealIdentityRenewalTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        f = self.fixture.f
        self.fixture.env.admission.discovery.projects = f.skills.procedure_scope_actor.projects
        self.configuration = ConfigurationService(ConfigurationRegistryStore(f.sqlite))
        install_procedure_configuration(self.configuration)
        draft = self.configuration.create_draft(ConfigurationDraftCreate(
            key=PROCEDURE_CONTEXT_CHARACTERS.key, scope_type=ConfigurationScope.DEPLOYMENT,
            value=12000, actor=f.actor.identity_id, reason='Private canonical capacity regression'))
        self.configuration.publish(draft.id, ConfigurationPublishRequest(actor=f.actor.identity_id,
            reason='Retain actual default capacity in private fixture'))
        # This is the same code-owned adapter installed by application.py, not a
        # test-only resolver or a callback whose result is declared trusted.
        self.resolver = ProcedureContextCapacityResolver(self.configuration)
        self.fixture.env.admission.discovery.context_capacity_resolver = self.resolver
        self.pending = self.fixture.current().native_credential_custody.model_copy(deep=True)
        self.pending_refs = self.fixture.current().secret_refs

    def assert_pending_custody(self):
        assignment = self.fixture.current()
        self.assertEqual(assignment.native_credential_custody, self.pending)
        self.assertEqual(assignment.secret_refs, self.pending_refs)
        self.assertIsNone(assignment.native_credential_custody.admitted_at)

    async def test_capacity_metadata_read_cannot_trap_actual_renewal_in_seal_writer(self):
        f = self.fixture.f
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
            if namespace != self.configuration.store.namespace:
                return result
            if threading.current_thread() in threads:
                return result
            if lock._is_owned():
                writer_reads.append(namespace)
                if not writer_entered.is_set():
                    writer_entered.set()
                    if not writer_release.wait(30):
                        raise AssertionError('Fixture did not release writer Configuration read')
            elif not outside_entered.is_set():
                outside_entered.set()
                if not outside_release.wait(30):
                    raise AssertionError('Fixture did not release outside Configuration read')
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
                'Canonical Configuration metadata trapped finite physical renewal: '
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
                    self.assertTrue(outside_entered.is_set(), 'Actual canonical Configuration read was not reached')
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
                    self.assertEqual(writer_reads, [], 'Configuration metadata still read under physical seal writer')
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

    async def test_published_capacity_change_refuses_before_credential_updater(self):
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
            draft = self.configuration.create_draft(ConfigurationDraftCreate(
                key=PROCEDURE_CONTEXT_CHARACTERS.key, scope_type=ConfigurationScope.DEPLOYMENT,
                value=11000, actor=self.fixture.f.actor.identity_id, reason='Changed after observation'))
            self.configuration.publish(draft.id, ConfigurationPublishRequest(
                actor=self.fixture.f.actor.identity_id, expected_active_revision=1,
                reason='Current published capacity drift'))
            return proof
        with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed), \
                patch.object(self.fixture.f.workers.store, 'transition_assignment', observed_transition):
            with self.assertRaises(ExecutionPrestartRefusal) as refused:
                await self.fixture.env.admit()
        self.assertEqual(refused.exception.detail['code'], 'procedure_unsupported_enforcement')
        self.assertIn({'stage':'fallback', 'exception_type':'ValueError'},
            refused.exception.detail['native_preparation_diagnostics'])
        self.assertEqual(updates, [])
        self.assert_pending_custody()

    async def test_configuration_resolve_hook_change_cannot_use_observed_data(self):
        prepare = self.session.prepare_native_provider_preparation
        original = self.configuration.resolve
        substituted = []
        def resolve(*args, **kwargs):
            substituted.append(True)
            return original(*args, **kwargs)
        def changed(*args):
            proof = prepare(*args)
            self.configuration.resolve = resolve
            return proof
        try:
            with patch.object(self.session, 'prepare_native_provider_preparation', side_effect=changed):
                with self.assertRaises(ExecutionPrestartRefusal) as refused:
                    await self.fixture.env.admit()
            self.assertEqual(refused.exception.detail['code'], 'procedure_credential_metadata_owner_changed')
            self.assertEqual(substituted, [])
            self.assert_pending_custody()
        finally:
            self.configuration.resolve = original

    async def test_custom_capacity_callback_keeps_current_fallback(self):
        calls = []
        original = self.configuration.resolve
        def callback():
            calls.append(self.fixture.f.workers.store._lock._is_owned())
            return original(PROCEDURE_CONTEXT_CHARACTERS.key)
        self.fixture.env.admission.discovery.context_capacity_resolver = callback
        result, _ = await self.fixture.env.admit()
        self.assertTrue(result.admitted)
        self.assertIn(True, calls)


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(NativeSealCapacityRenewalTests(name)
        for name in NativeSealCapacityRenewalTests.__dict__ if name.startswith('test_'))
