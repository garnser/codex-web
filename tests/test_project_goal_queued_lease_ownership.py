"""Finite Goal custody during the real claimed, pre-native queue drain."""
import asyncio
import hashlib
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_workers import AssignmentCancelRequest, AssignmentRenewRequest, WorkerHeartbeatRequest
from codex_web.autonomy import AutonomyMode
from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
from codex_web.services import execution_workers as worker_module
from codex_web.services import goal_execution_bindings as binding_module
from codex_web.services import project_goal_continuation as project_module
from codex_web.services.goal_continuation_recovery import GoalContinuationRecoveryService
from codex_web.services.goal_execution_bindings import GoalExecutionBindingConflictError
from tests.test_project_goal_worker_admission import ProjectGoalWorkerAdmissionTests


class ProjectGoalQueuedLeaseOwnershipTests(ProjectGoalWorkerAdmissionTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.execution.queued_preparation_owner = self.project_goal.prepare_queued_owned

    async def test_claimed_native_preparation_keeps_finite_goal_lease_owned_until_start(self):
        await self.claimed_preparation()

    async def test_cancelled_drain_joins_same_preparation_and_lease_owner(self):
        await self.claimed_preparation(cancel_observer=True)

    async def test_postclaim_actor_setup_is_offloaded_and_owned_before_native_preparation(self):
        await self.claimed_preparation(setup_gate=True)

    async def claimed_preparation(self, *, cancel_observer=False, setup_gate=False):
        origin = time.time()
        now = [origin]
        clock = SimpleNamespace(time=lambda: now[0])
        loop = asyncio.get_running_loop()
        monotonic_origin = loop.time()
        entered = threading.Event()
        release = threading.Event()
        factory = self.execution.actor_resolver if setup_gate else self.execution.native_input_guard_factory
        captured = []
        unexpected = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unexpected.append(context))

        def gated_factory(*args, **kwargs):
            # Hold either actual actor setup immediately after the claim, or
            # the later factory after real discovery/seal. No native input yet.
            attempt = self.execution.queued_preflight.store.for_thread(self.thread)[0]
            captured.append(attempt)
            entered.set()
            release.wait()
            return factory(*args, **kwargs)

        if setup_gate:
            self.execution.actor_resolver = gated_factory
        else:
            self.execution.native_input_guard_factory = gated_factory
        drain = waiter = None
        try:
            with patch.object(project_module, 'time', clock), \
                    patch.object(worker_module, 'time', clock), \
                    patch.object(binding_module, 'time', clock), \
                    patch.object(loop, 'time', lambda: monotonic_origin + now[0] - origin):
                queued_binding = await self.queued_goal()
                initial_expiry = queued_binding.lease_expires_at
                self.assertGreater(initial_expiry, origin)
                original_delivery = queued_binding.project_continuation.delivery
                drain = asyncio.create_task(self.execution.drain_thread_queue(self.thread))
                waiter = asyncio.create_task(asyncio.to_thread(entered.wait))
                await asyncio.wait({drain, waiter}, return_when=asyncio.FIRST_COMPLETED)
                self.assertTrue(entered.is_set(), self.native_diagnostics())
                self.assertFalse(drain.done(), self.native_diagnostics())
                self.assertEqual(len(captured), 1)
                original = captured[0]
                self.assertEqual(original.status, 'reconciliation_required')
                self.assertTrue(original.queued_admission_claim_id)
                self.assertEqual(self.queue_store.get(self.thread), [])
                self.assertEqual(self.starts(), [])

                if cancel_observer:
                    drain.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(drain.done(), 'Actual preparation must settle before cancellation returns')

                step = self.goal_continuation.lease_seconds / 10
                # Advance BOTH finite custody time and asyncio timer time in
                # small increments. Actual async heartbeat owners get normal
                # timer/executor scheduling; there is no elapsed sleep or jump
                # directly from a live lease to an already-expired lease.
                for index in range(1, 15):
                    now[0] = origin + step * index
                    await asyncio.sleep(0)
                    await asyncio.to_thread(self.f.workers.heartbeat, self.target.assigned_worker_id,
                        WorkerHeartbeatRequest(version=self.f.worker.version), actor=self.worker_actor)
                    current = self.f.workers.store.assignment(self.target.id)
                    await asyncio.to_thread(self.f.workers.renew, current.assigned_worker_id, current.id,
                        AssignmentRenewRequest(lease_token=current.lease.lease_token,
                            fence=current.fence, lease_seconds=120), actor=self.worker_actor)
                    await asyncio.sleep(0)

                self.assertGreater(now[0], initial_expiry)
                current_worker = self.f.workers.store.assignment(self.target.id)
                self.assertGreater(current_worker.lease.expires_at, now[0])
                self.assertEqual(current_worker.fence, self.target.fence)
                recovery = GoalContinuationRecoveryService(self.goal_bindings,
                    self.goal_continuation, self.sessions)
                await recovery.reconcile_retained_worker_scope(scope=self.actor.tenant, now=now[0])
                current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
                self.assertEqual(current.project_continuation.delivery.id, original_delivery.id)
                self.assertEqual(current.project_continuation.reserved_calls, 1)
                release.set()
                if cancel_observer:
                    with self.assertRaises(asyncio.CancelledError):
                        await drain
                else:
                    await drain
                after = self.execution.queued_preflight.store.get(original.id)
                self.assertEqual(after.queued_turn_snapshot, original.queued_turn_snapshot)
                self.assertEqual(after.queued_admission_claim_id, original.queued_admission_claim_id)
                self.assertEqual(len(self.starts()), 1,
                    {'binding_expiry': current.lease_expires_at, 'now': now[0],
                     'worker_expiry': current_worker.lease.expires_at, **self.native_diagnostics()})
                self.assertEqual(after.status, 'started', self.native_diagnostics())
                self.assertIsNotNone(after.queued_start_receipt)
                self.assertEqual(after.queued_start_receipt.requested_execution_id, original_delivery.id)
                self.assertEqual(self.queue_store.get(self.thread), [])
        finally:
            release.set()
            entered.set()  # Also release the observer if preparation refused earlier.
            if drain is not None:
                await asyncio.gather(drain, return_exceptions=True)
            if waiter is not None:
                await asyncio.gather(waiter, return_exceptions=True)
            loop.set_exception_handler(previous_handler)
        self.assertEqual(unexpected, [], 'Unexpected background task failure')

    async def refused_preparation(self, change):
        await self.queued_goal()
        entered = threading.Event()
        release = threading.Event()
        factory = self.execution.native_input_guard_factory
        originals = []
        def gated(**kwargs):
            originals.append(self.execution.queued_preflight.store.for_thread(self.thread)[0])
            entered.set()
            release.wait()
            return factory(**kwargs)
        self.execution.native_input_guard_factory = gated
        drain = asyncio.create_task(self.execution.drain_thread_queue(self.thread))
        observer = asyncio.create_task(asyncio.to_thread(entered.wait))
        try:
            await asyncio.wait({drain, observer}, return_when=asyncio.FIRST_COMPLETED)
            self.assertTrue(entered.is_set(), self.native_diagnostics())
            original = originals[0]
            before = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
            if change == 'policy':
                self.control = self.control.model_copy(update={'mode': AutonomyMode.PAUSED})
            elif change == 'revoke':
                self.goal_bindings.update(before.id, GoalExecutionBindingUpdate(status='cancelled',
                    stop_reason='Actual operator revocation', reason='Revoked during claimed preparation'),
                    scope=self.actor.tenant, actor_id=self.actor.identity_id, application_only=True)
            elif change == 'fence':
                self.f.workers.cancel_bootstrap(self.target.id, AssignmentCancelRequest(
                    expected_fence=self.target.fence, reason='Actual fenced cancellation during preparation'), actor=self.actor)
            else:
                self.fail('Unknown negative case')
            changed = self.goal_bindings.get(before.id, scope=self.actor.tenant)
            release.set()
            await drain
            actual = self.execution.queued_preflight.store.get(original.id)
            self.assertEqual(self.starts(), [], self.native_diagnostics())
            self.assertEqual(actual.queued_turn_snapshot, original.queued_turn_snapshot)
            self.assertEqual(actual.queued_admission_claim_id, original.queued_admission_claim_id)
            self.assertIsNone(actual.queued_start_receipt)
            after = self.goal_bindings.get(before.id, scope=self.actor.tenant)
            self.assertEqual(after.project_continuation.reserved_calls, 1)
            if change == 'revoke':
                self.assertEqual(after, changed, 'Heartbeat must not overwrite an operator revocation')
        finally:
            release.set(); entered.set()
            await asyncio.gather(drain, observer, return_exceptions=True)

    async def test_policy_pause_during_claimed_preparation_cannot_start(self):
        await self.refused_preparation('policy')

    async def test_revoked_binding_during_claimed_preparation_cannot_start(self):
        await self.refused_preparation('revoke')

    async def test_cancelled_assignment_fence_during_claimed_preparation_cannot_start(self):
        await self.refused_preparation('fence')

    async def rejected_initial_owner(self, *, expired):
        await self.queued_goal()
        owner = self.execution.queued_preparation_owner
        operations = []
        observed = []
        async def changed_owner(queued, **kwargs):
            current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
            if not expired:
                def drift(row):
                    original = row.project_continuation.delivery
                    changed_checksum = hashlib.sha256(
                        ('different-custody:' + original.assignment_fingerprint).encode()).hexdigest()
                    delivery = type(original).model_validate({**original.model_dump(mode='python'),
                        'assignment_fingerprint': changed_checksum})
                    return row.model_copy(update={'project_continuation': row.project_continuation.model_copy(
                        update={'delivery': delivery})})
                self.project_goal.mutate(current.id, self.actor.tenant, drift, 'Actual immutable delivery drift')
                current = self.goal_bindings.get(current.id, scope=self.actor.tenant)
            observed.append(current)
            operation = kwargs['operation']
            async def tracked():
                operations.append(True)
                return await operation()
            kwargs['operation'] = tracked
            if expired:
                clock = SimpleNamespace(time=lambda: current.lease_expires_at + 1)
                with patch.object(project_module, 'time', clock), patch.object(binding_module, 'time', clock):
                    return await owner(queued, **kwargs)
            return await owner(queued, **kwargs)
        self.execution.queued_preparation_owner = changed_owner
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(observed), 1)
        self.assertEqual(operations, [], 'An invalid original must not invoke preparation')
        self.assertEqual(self.starts(), [])
        after = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(after, observed[0], 'Invalid original must not renew or rewrite finite custody')
        original = self.execution.queued_preflight.store.for_thread(self.thread)[0]
        self.assertEqual(original.status, 'reconciliation_required')
        self.assertIsNone(original.queued_start_receipt)
        self.assertEqual(after.project_continuation.reserved_calls, 1)

    async def test_expired_initial_claimed_lease_is_not_revived(self):
        await self.rejected_initial_owner(expired=True)

    async def test_changed_immutable_delivery_is_not_renewed_or_prepared(self):
        await self.rejected_initial_owner(expired=False)

    async def test_heartbeat_observes_expiry_inside_delayed_writer(self):
        binding = await self.queued_goal()
        now = [time.time()]
        clock = SimpleNamespace(time=lambda: now[0])
        update = self.goal_bindings.store.update
        def delayed_writer(apply):
            now[0] = binding.lease_expires_at + 1
            return update(apply)
        with patch.object(binding_module, 'time', clock), \
                patch.object(self.goal_bindings.store, 'update', delayed_writer):
            with self.assertRaisesRegex(GoalExecutionBindingConflictError, 'expired'):
                self.goal_bindings.heartbeat_continuation(binding.id, scope=self.actor.tenant,
                    owner_id=self.goal_continuation.owner_id, lease_seconds=self.goal_continuation.lease_seconds,
                    expected_binding=binding)
        self.assertEqual(self.goal_bindings.get(binding.id, scope=self.actor.tenant), binding)
        self.assertEqual(self.starts(), [])


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(ProjectGoalQueuedLeaseOwnershipTests(name)
        for name, method in ProjectGoalQueuedLeaseOwnershipTests.__dict__.items()
        if name.startswith('test_') and callable(method))
