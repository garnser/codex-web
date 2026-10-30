"""Real Home/Upgrade owners; diagnostic failures never grant Home continuation."""
import unittest
from unittest.mock import patch

from codex_web.storage.state_store import storage_operation_budget
from tests import test_home_unowned_upgrade as fixture
from tests import test_home_preservation_failure_diagnostics as drain_api


class NonblockingHomeUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixture.UnownedHomeUpgradeTests(methodName='runTest')
        await self.f.asyncSetUp()
        owner = getattr(self.f.planned, 'checkpoint_for_service_drain', self.f.planned.checkpoint_current)
        callback = lambda plan_id, actor: owner(plan_id, worker_runtime=self.f.runtime, operator_actor=actor)
        self.f.upgrades.home_checkpoint_before_drain = callback
        self.f.lose()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    async def drain(self):
        return await drain_api.HomePreservationFailureDiagnosticTests.request_drain(self.f)

    def diagnostic(self, response, outcome):
        self.assertEqual(response.status_code, 200, response.text)
        current = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
        self.assertTrue(current.maintenance_mode)
        self.assertFalse(current.rollback_available)
        self.assertEqual(current.status, 'draining')
        self.assertEqual(current.home_checkpoint_disposition.outcome, outcome)
        self.assertFalse(current.home_checkpoint_disposition.continuation_authorized)
        self.assertEqual(self.f.effects, 0)
        return current.home_checkpoint_disposition

    async def test_settled_artifact_budget_refusal_does_not_gate_global_drain_or_retry(self):
        class PrivateArtifactDeadline(TimeoutError):
            pass
        before = self.f.preserved()
        with patch.object(self.f.artifacts, 'create_artifact', side_effect=PrivateArtifactDeadline('private unavailable writer')) as writer:
            first = self.diagnostic(await self.drain(), 'deferred_unknown')
            self.assertEqual(writer.call_count, 1)
            self.diagnostic(await self.drain(), 'deferred_unknown')
            self.assertEqual(writer.call_count, 1)
        self.assertEqual(first.exception_type, 'TimeoutError')
        self.assertTrue(first.preservation_artifact_id.startswith('artifact-'))
        self.assertEqual(self.f.preserved(), before)
        self.assertIsNone(self.f.planned._dormant_artifact(self.f.plan.id, self.f.actor))

    async def test_committed_artifact_with_final_expiry_stays_unknown_without_resume(self):
        before = self.f.preserved()
        original = self.f.artifacts.create_artifact
        created = []
        def commit_then_expire(*args, **kwargs):
            artifact = original(*args, **kwargs)
            created.append(artifact.id)
            storage_operation_budget.get().deadline -= 6
            return artifact
        with patch.object(self.f.artifacts, 'create_artifact', side_effect=commit_then_expire):
            diagnostic = self.diagnostic(await self.drain(), 'deferred_unknown')
        self.assertEqual(created, [diagnostic.preservation_artifact_id])
        self.assertIsNone(diagnostic.recorded_artifact_id)
        self.assertEqual(self.f.artifacts.get_artifact(created[0], actor=self.f.actor).id, created[0])
        # Preserve the actual diagnostic when simulating the existing completed plan.
        drained = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
        self.f.plan = drained
        self.f.completed()
        with patch.object(self.f.planned, '_dormant_artifact', side_effect=AssertionError('must not promote unknown Artifact')):
            self.assertIsNone(self.f.planned.resume_completed(self.f.plan.id, operator_actor=self.f.operator))
        self.assertEqual(self.f.preserved(), before)

    async def test_known_home_guard_failure_is_warning_not_home_readiness(self):
        self.f.active_store.put('operator-thread', self.f.interrupted)
        before = self.f.preserved()
        diagnostic = self.diagnostic(await self.drain(), 'blocked')
        self.assertTrue(diagnostic.reason_code.startswith('home_upgrade_'))
        self.assertEqual(self.f.preserved(), before)
        self.assertIsNotNone(self.f.active_store.load().get('operator-thread'))

    async def test_selected_identity_unavailable_does_not_revoke_upgrade_operator(self):
        def disable_selected(state):
            state.humans = [row.model_copy(update={'disabled_at': 1.0})
                if row.id == self.f.actor.identity_id else row for row in state.humans]
            return state
        self.f.f.identity.store.update(disable_selected)
        diagnostic = self.diagnostic(await self.drain(), 'blocked')
        self.assertEqual(diagnostic.reason_code, 'home_upgrade_current_authority_unavailable')

    async def test_current_profile_revision_change_is_not_a_global_gate(self):
        from codex_web.agent_profiles import AgentProfileUpdate
        self.f.profiles.update('home-operator', AgentProfileUpdate(name='Updated selected profile',
            reason='Private current revision change'), actor=self.f.actor)
        diagnostic = self.diagnostic(await self.drain(), 'blocked')
        self.assertEqual(diagnostic.reason_code, 'home_upgrade_selected_profile_changed')

    async def test_global_operator_revocation_and_unexpected_errors_still_refuse(self):
        from codex_web.services.identity import IdentityError
        original = self.f.f.identity.actor_for_identity
        def operator_unavailable(identity_id, **kwargs):
            if identity_id == self.f.operator.identity_id:
                raise IdentityError('private revoked Upgrade operator')
            return original(identity_id, **kwargs)
        with patch.object(self.f.f.identity, 'actor_for_identity', side_effect=operator_unavailable):
            response = await self.drain()
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(self.f.upgrades.get(self.f.plan.id, actor=self.f.operator).maintenance_mode)
        # A different plan avoids reusing the first plan's unknown reservation.
        self.f.plan = self.f.plan.model_copy(update={'id': 'upgrade-programming-error'})
        self.f.put_plan(self.f.plan)
        with patch.object(self.f.f.sqlite, 'home_intent_recovery_rows', side_effect=TypeError('private defect')):
            response = await self.drain()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, 'Internal Server Error')

    async def test_final_upgrade_write_unknown_reuses_pending_without_second_home_writer(self):
        update = self.f.upgrades._update
        def fail_final(plan_id, actor, updater, **kwargs):
            # The reservation is durable; fail only the mandatory drain mutation.
            if updater.__name__ == '<lambda>':
                raise TimeoutError('private final Upgrade write outcome unknown')
            return update(plan_id, actor, updater, **kwargs)
        with patch.object(self.f.planned, 'checkpoint_for_service_drain', wraps=self.f.planned.checkpoint_for_service_drain) as owner:
            # Rebind the callback to make the actual owner attempt observable.
            self.f.upgrades.home_checkpoint_before_drain = lambda p, a: self.f.planned.checkpoint_for_service_drain(
                p, worker_runtime=self.f.runtime, operator_actor=a)
            with patch.object(self.f.upgrades, '_update', side_effect=fail_final):
                response = await self.drain()
            self.assertEqual(response.status_code, 500)
            self.assertEqual(owner.call_count, 1)
            pending = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
            self.assertFalse(pending.maintenance_mode)
            self.assertEqual(pending.home_checkpoint_disposition.outcome, 'pending_unknown')
            observed = pending.home_checkpoint_disposition
            self.assertEqual(observed.selection, self.f.projects.get('home', self.f.actor.tenant).home_operator)
            self.assertIsNotNone(observed.checkpoint_artifact_id)
            self.assertEqual(self.f.planned._dormant_artifact(self.f.plan.id, self.f.actor).id,
                observed.preservation_artifact_id)
            self.diagnostic(await self.drain(), 'deferred_unknown')
            self.assertEqual(owner.call_count, 1)

    async def test_success_records_actual_artifact_and_repeat_drain_never_rewrites(self):
        original = self.f.artifacts.create_artifact
        with patch.object(self.f.artifacts, 'create_artifact', wraps=original) as writer:
            first = self.diagnostic(await self.drain(), 'recorded')
            self.assertEqual(writer.call_count, 1)
            self.diagnostic(await self.drain(), 'recorded')
            self.assertEqual(writer.call_count, 1)
        self.assertEqual(first.recorded_artifact_id, first.preservation_artifact_id)
        self.assertEqual(self.f.artifacts.get_artifact(first.recorded_artifact_id, actor=self.f.actor).source,
            'home-upgrade-dormant-preservation')

    async def test_real_global_admission_still_blocks_preflight(self):
        from types import SimpleNamespace
        from codex_web.recovery import RecoveryHealth
        self.f.upgrades.recovery = SimpleNamespace(health=lambda **kwargs: RecoveryHealth(policy_configured=False))
        self.diagnostic(await self.drain(), 'recorded')
        with self.f.upgrades.admission.admit(self.f.actor.organization_id, self.f.actor.workspace_id,
                lambda: True, kind='native') as admitted:
            self.assertTrue(admitted)
            current = self.f.upgrades.preflight(self.f.plan.id, actor=self.f.operator)
            self.assertEqual(current.preflight.active_native_admissions, 1)
            self.assertTrue(any(code.startswith('active_operations_not_drained:') for code in current.preflight.blockers))
        current = self.f.upgrades.preflight(self.f.plan.id, actor=self.f.operator)
        self.assertEqual(current.preflight.active_native_admissions, 0)
        self.assertFalse(any(code.startswith('active_operations_not_drained:') for code in current.preflight.blockers))

    async def test_unknown_reservation_skips_home_writer_and_global_drain_can_proceed(self):
        class PrivateStorageDeadline(TimeoutError):
            pass
        update = self.f.upgrades._update
        calls = []
        def commit_reservation_then_unknown(*args, **kwargs):
            result = update(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise PrivateStorageDeadline('private reservation committed but response lost')
            return result
        with patch.object(self.f.upgrades, '_update', side_effect=commit_reservation_then_unknown), \
                patch.object(self.f.artifacts, 'create_artifact', side_effect=AssertionError('no Home writer after unknown reservation')):
            diagnostic = self.diagnostic(await self.drain(), 'deferred_unknown')
        self.assertEqual(diagnostic.reason_code, 'home_checkpoint_reservation_unknown')
        self.assertEqual(len(calls), 2)
        self.assertEqual(diagnostic.exception_type, 'TimeoutError')

    async def test_missing_selected_profile_does_not_gate_global_drain(self):
        def remove_profile(state):
            state.revisions = [row for row in state.revisions if row.profile_id != 'home-operator']
            return state
        self.f.profiles.store.update(remove_profile)
        diagnostic = self.diagnostic(await self.drain(), 'blocked')
        self.assertEqual(diagnostic.reason_code, 'home_upgrade_current_authority_unavailable')

    async def test_forward_format_preserves_old_outcomes_but_invalidates_binary_rollback(self):
        self.assertTrue(self.f.plan.rollback_available)
        old = self.f.plan.model_copy(update={'id': 'upgrade-historical-unknown', 'status': 'failed'})
        self.f.put_plan(old)
        with patch.object(self.f.artifacts, 'create_artifact', side_effect=TimeoutError('private current refusal')):
            self.diagnostic(await self.drain(), 'deferred_unknown')
        self.assertEqual(self.f.upgrades.get(old.id, actor=self.f.operator).model_dump(mode='json'),
            old.model_copy(update={'rollback_available': False}).model_dump(mode='json'))
        from codex_web.upgrades import UpgradePlanCreate
        payload = UpgradePlanCreate(release_id=self.f.plan.release_id, current_app_version='1.0',
            target_app_version='2.0', observed_control_plane_versions=('1.0',), steps=(),
            compatibility=self.f.plan.compatibility.model_copy(update={'rollback_supported_to_app_version': '1.0'}))
        subsequent = self.f.upgrades.create(payload, actor=self.f.operator)
        self.assertFalse(subsequent.rollback_available)

    async def test_unknown_identity_pin_skips_artifact_writer_but_keeps_observed_ids(self):
        update = self.f.upgrades._update
        def commit_pin_then_unknown(plan_id, actor, updater, **kwargs):
            result = update(plan_id, actor, updater, **kwargs)
            if updater.__name__ == 'pin':
                raise TimeoutError('private diagnostic pin committed but response unknown')
            return result
        with patch.object(self.f.upgrades, '_update', side_effect=commit_pin_then_unknown), \
                patch.object(self.f.artifacts, 'create_artifact', side_effect=AssertionError('no writer after unknown pin')):
            diagnostic = self.diagnostic(await self.drain(), 'deferred_unknown')
        self.assertIsNotNone(diagnostic.selection)
        self.assertIsNotNone(diagnostic.preservation_artifact_id)
        self.assertIsNone(diagnostic.recorded_artifact_id)
        self.assertIsNone(self.f.planned._dormant_artifact(self.f.plan.id, self.f.actor))

    async def test_selection_changed_after_observed_pin_blocks_only_home_writer(self):
        pin = self.f.upgrades._pin_home_checkpoint_observation
        original = self.f.projects.get('home', self.f.actor.tenant).home_operator
        def pin_then_change(*args, **kwargs):
            result = pin(*args, **kwargs)
            self.f.projects.repository.update_home_operator(organization_id=self.f.actor.organization_id,
                workspace_id=self.f.actor.workspace_id, expected_revision=original.revision,
                binding=original.model_copy(update={'revision': original.revision + 1}))
            return result
        with patch.object(self.f.upgrades, '_pin_home_checkpoint_observation', side_effect=pin_then_change), \
                patch.object(self.f.artifacts, 'create_artifact', side_effect=AssertionError('changed selection must not write')):
            diagnostic = self.diagnostic(await self.drain(), 'blocked')
        self.assertEqual(diagnostic.selection, original)
        self.assertEqual(diagnostic.reason_code, 'home_upgrade_selected_navigation_changed')
        self.assertEqual(self.f.projects.get('home', self.f.actor.tenant).home_operator.revision, original.revision + 1)

    async def test_selection_cleared_between_pin_and_owner_never_writes_artifact(self):
        pin = self.f.upgrades._pin_home_checkpoint_observation
        original = self.f.projects.get('home', self.f.actor.tenant).home_operator
        def pin_then_clear(*args, **kwargs):
            result = pin(*args, **kwargs)
            self.f.projects.repository.update_home_operator(organization_id=self.f.actor.organization_id,
                workspace_id=self.f.actor.workspace_id, expected_revision=original.revision,
                binding=original.model_copy(update={'thread_id': None, 'revision': original.revision + 1}))
            return result
        with patch.object(self.f.upgrades, '_pin_home_checkpoint_observation', side_effect=pin_then_clear), \
                patch.object(self.f.artifacts, 'create_artifact', side_effect=AssertionError('cleared selection must not write')):
            diagnostic = self.diagnostic(await self.drain(), 'blocked')
        self.assertEqual(diagnostic.selection, original)
        self.assertEqual(diagnostic.reason_code, 'home_upgrade_selected_navigation_changed')
        self.assertIsNone(self.f.projects.get('home', self.f.actor.tenant).home_operator.thread_id)
