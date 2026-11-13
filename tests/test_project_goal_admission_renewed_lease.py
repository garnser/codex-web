"""Current finite custody after canonical same-owner metadata-time renewal."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_workers import AssignmentCancelRequest, AssignmentRenewRequest, WorkerHeartbeatRequest
from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
from codex_web.services import execution_workers as worker_module
from codex_web.services import goal_execution_bindings as binding_module
from codex_web.services import project_goal_continuation as project_module
from tests import test_project_goal_worker_admission as fixtures


class ProjectGoalAdmissionRenewedLeaseTests(fixtures.ProjectGoalWorkerAdmissionTests):
    async def context_drift(self, kind):
        origin = time.time()
        now = [origin]
        clock = SimpleNamespace(time=lambda: now[0])
        original_profile = self.profiles.resolve_for_execution
        changed = []
        with patch.object(project_module, 'time', clock), \
                patch.object(worker_module, 'time', clock), \
                patch.object(binding_module, 'time', clock):
            target = self.f.workers.store.assignment(self.target.id)
            self.f.workers.renew(target.assigned_worker_id, target.id,
                AssignmentRenewRequest(lease_token=target.lease.lease_token, fence=target.fence,
                    lease_seconds=300 if kind == 'binding_expired' else 120), actor=self.worker_actor)
            claimed = self.goal_bindings.claim_continuation(self.goal_binding.id,
                scope=self.actor.tenant, owner_id=self.goal_continuation.owner_id,
                lease_seconds=300 if kind == 'worker_expired' else 120)
            self.assertIsNotNone(claimed)

            def resolve_then_change(*args, **kwargs):
                result = original_profile(*args, **kwargs)
                self.assertEqual(changed, [])
                changed.append(kind)
                if kind in {'worker_expired', 'binding_expired'}:
                    now[0] = origin + 121
                elif kind == 'binding_paused':
                    self.goal_bindings.update(claimed.id, GoalExecutionBindingUpdate(
                        status='blocked', stop_reason='operator paused: metadata interleaving',
                        reason='Actual operator stop during admission'), scope=self.actor.tenant,
                        actor_id=self.actor.identity_id, application_only=True)
                elif kind == 'cancelled':
                    cancelled = self.f.workers.cancel_bootstrap(target.id, AssignmentCancelRequest(
                        expected_fence=target.fence, reason='Actual canonical cancellation during metadata'),
                        actor=self.actor)
                    self.assertEqual(cancelled.status, 'cancelled')
                    self.assertEqual(cancelled.fence, target.fence+1)
                    self.assertIsNone(cancelled.lease)
                else:
                    def drift(state):
                        current = state.assignments[0]
                        if kind == 'fence':
                            current = current.model_copy(update={'fence': current.fence+1})
                        elif kind == 'token':
                            current = current.model_copy(update={'lease': current.lease.model_copy(
                                update={'lease_token': 'different-current-token-for-negative'})})
                        elif kind == 'resources':
                            current = current.model_copy(update={'resource_ids': ('changed-current-resource',)})
                        state.assignments = [current]
                        return state
                    self.f.workers.store.transition_assignment(target.assigned_worker_id, target.id, drift)
                return result

            with patch.object(self.profiles, 'resolve_for_execution', resolve_then_change):
                with self.assertRaises(ValueError):
                    self.project_goal.context(claimed)
            self.assertEqual(changed, [kind])
            self.assertIsNone(self.goal_bindings.get(claimed.id, scope=self.actor.tenant).last_execution_id)
            self.assertEqual(self.queue_store.get(self.thread), [])
            self.assertEqual(self.starts(), [])

    async def test_current_worker_lease_expiry_still_refuses(self):
        await self.context_drift('worker_expired')

    async def test_current_binding_lease_expiry_still_refuses(self):
        await self.context_drift('binding_expired')

    async def test_operator_pause_during_metadata_still_refuses(self):
        await self.context_drift('binding_paused')

    async def test_current_fence_drift_still_refuses(self):
        await self.context_drift('fence')

    async def test_current_lease_token_drift_still_refuses(self):
        await self.context_drift('token')

    async def test_current_resource_drift_still_refuses(self):
        await self.context_drift('resources')

    async def test_canonical_cancellation_during_metadata_still_refuses(self):
        await self.context_drift('cancelled')

    async def test_same_owner_leases_renewed_during_metadata_remain_admissible(self):
        origin = time.time()
        now = [origin]
        clock = SimpleNamespace(time=lambda: now[0])
        worker = self.f.workers
        scope = self.actor.tenant
        owner = self.goal_continuation.owner_id
        original_profile = self.profiles.resolve_for_execution
        renewed = []

        with patch.object(project_module, 'time', clock), \
                patch.object(worker_module, 'time', clock), \
                patch.object(binding_module, 'time', clock):
            target = worker.store.assignment(self.target.id)
            initial = worker.renew(target.assigned_worker_id, target.id,
                AssignmentRenewRequest(lease_token=target.lease.lease_token,
                    fence=target.fence, lease_seconds=120), actor=self.worker_actor)
            claimed = self.goal_bindings.claim_continuation(self.goal_binding.id,
                scope=scope, owner_id=owner, lease_seconds=120)
            self.assertIsNotNone(claimed)

            def resolve_after_real_renewal(*args, **kwargs):
                # This real canonical metadata boundary follows context()'s
                # initial binding/assignment observations. Renew BEFORE expiry,
                # then finish metadata after only those old samples expire.
                self.assertEqual(renewed, [])
                now[0] = origin + 30
                worker.heartbeat(initial.assigned_worker_id,
                    WorkerHeartbeatRequest(version=self.f.worker.version), actor=self.worker_actor)
                current = worker.store.assignment(initial.id)
                actual = worker.renew(current.assigned_worker_id, current.id,
                    AssignmentRenewRequest(lease_token=current.lease.lease_token,
                        fence=current.fence, lease_seconds=120), actor=self.worker_actor)
                binding = self.goal_bindings.heartbeat_continuation(claimed.id,
                    scope=scope, owner_id=owner, lease_seconds=120)
                renewed.append((binding, actual))
                now[0] = origin + 121
                self.assertLess(initial.lease.expires_at, now[0])
                self.assertLess(claimed.lease_expires_at, now[0])
                self.assertGreater(actual.lease.expires_at, now[0])
                self.assertGreater(binding.lease_expires_at, now[0])
                return original_profile(*args, **kwargs)

            with patch.object(self.profiles, 'resolve_for_execution', resolve_after_real_renewal):
                try:
                    current_binding, _, _, assignment = self.project_goal.context(claimed)
                except ValueError as exc:
                    self.fail('Canonical unchanged custody was renewed while live, '
                        'but final Project Goal admission rejected its old sample: '+str(exc))
            self.assertEqual(len(renewed), 1)
            binding, actual = renewed[0]
            self.assertEqual(current_binding.lease_expires_at, binding.lease_expires_at)
            self.assertEqual(assignment.lease.expires_at, actual.lease.expires_at)
            self.assertEqual(assignment.lease.lease_token, initial.lease.lease_token)
            self.assertEqual(assignment.lease.acquired_at, initial.lease.acquired_at)
            self.assertEqual(assignment.fence, initial.fence)
            self.assertEqual(self.project_goal.fingerprint(assignment), self.project_goal.fingerprint(initial))
            self.assertIsNone(self.goal_bindings.get(claimed.id, scope=scope).last_execution_id)
            self.assertEqual(self.queue_store.get(self.thread), [])
            self.assertEqual(self.starts(), [])


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(ProjectGoalAdmissionRenewedLeaseTests(name)
        for name in ProjectGoalAdmissionRenewedLeaseTests.__dict__ if name.startswith('test_'))
