"""Expected Home selection drift is diagnostic; global drain remains canonical."""
import unittest
from unittest.mock import patch

from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.upgrades import UpgradeConflictError
from tests import test_home_unowned_upgrade as fixture
from tests import test_home_preservation_failure_diagnostics as drain_api


class HomeSelectionDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixture.UnownedHomeUpgradeTests(methodName='runTest')
        await self.f.asyncSetUp()
        self.f.lose()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    async def drain(self):
        return await drain_api.HomePreservationFailureDiagnosticTests.request_drain(self.f)

    def drifting_status(self, after):
        navigation = self.f.planned.goal_inspection.operator
        original = navigation.status
        calls = []
        def status(actor):
            value = original(actor)
            calls.append(1)
            if len(calls) == after:
                row = self.f.index.get('operator-thread')
                # A real canonical metadata change between the inspection's
                # two ordinary reads, not a replaced denial/checkpoint owner.
                self.f.index.upsert(row.model_copy(update={'name': 'Changed private Home name'}))
            return value
        return navigation, status, calls

    def assert_blocked_drain(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        current = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
        self.assertTrue(current.maintenance_mode)
        self.assertEqual(current.status, 'draining')
        self.assertEqual(current.admission_epoch, 1)
        self.assertEqual(current.home_checkpoint_disposition.outcome, 'blocked')
        self.assertEqual(current.home_checkpoint_disposition.reason_code, 'home_upgrade_selection_changed')
        self.assertFalse(current.home_checkpoint_disposition.continuation_authorized)
        self.assertIsNone(current.home_checkpoint_disposition.recorded_artifact_id)
        self.assertEqual(self.f.effects, 0)
        return current

    async def test_second_snapshot_selection_drift_is_blocked_before_writer_but_global_drain_proceeds(self):
        before = self.f.preserved()
        navigation, status, calls = self.drifting_status(after=3)
        authority = self.f.upgrades._require_service_drain_authority
        with patch.object(navigation, 'status', side_effect=status), \
                patch.object(self.f.artifacts, 'create_artifact', wraps=self.f.artifacts.create_artifact) as writer, \
                patch.object(self.f.upgrades, '_require_service_drain_authority', wraps=authority) as mandatory:
            current = self.assert_blocked_drain(await self.drain())
            self.assertEqual(len(calls), 4)
            writer.assert_not_called()
            self.assertGreaterEqual(mandatory.call_count, 2)
            # Same-plan continuation consumes the settled diagnostic; it does
            # not run another optional inspection or preservation writer.
            self.assertEqual((await self.drain()).status_code, 200)
            self.assertEqual(len(calls), 4)
            writer.assert_not_called()
        self.assertIsNotNone(current.home_checkpoint_disposition.preservation_artifact_id)
        self.assertEqual(self.f.preserved(), before)
        self.assertIsNone(self.f.planned._dormant_artifact(self.f.plan.id, self.f.actor))

    async def test_direct_home_checkpoint_still_refuses_selection_drift(self):
        navigation, status, _ = self.drifting_status(after=1)
        with patch.object(navigation, 'status', side_effect=status), \
                patch.object(self.f.artifacts, 'create_artifact', wraps=self.f.artifacts.create_artifact) as writer:
            with self.assertRaises(ExecutionPrestartRefusal) as caught:
                self.f.planned.checkpoint_current(self.f.plan.id, worker_runtime=self.f.runtime,
                    operator_actor=self.f.operator)
            self.assertEqual(caught.exception.detail['code'], 'home_upgrade_selection_changed')
            writer.assert_not_called()
        self.assertFalse(self.f.upgrades.get(self.f.plan.id, actor=self.f.operator).maintenance_mode)
        self.assertEqual(self.f.effects, 0)

    async def test_selection_refusal_does_not_bypass_final_global_authority(self):
        navigation, status, _ = self.drifting_status(after=3)
        original = self.f.upgrades._require_service_drain_authority
        calls = []
        def authority(actor):
            calls.append(1)
            if len(calls) == 2:
                raise UpgradeConflictError('Private mandatory authority changed')
            return original(actor)
        with patch.object(navigation, 'status', side_effect=status), \
                patch.object(self.f.upgrades, '_require_service_drain_authority', side_effect=authority):
            response = await self.drain()
        self.assertEqual(response.status_code, 409)
        current = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
        self.assertFalse(current.maintenance_mode)
        self.assertEqual(current.admission_epoch, 0)
        self.assertEqual(self.f.effects, 0)

    async def test_unrelated_exception_remains_unknown_and_never_repeats_home_writer(self):
        with patch.object(self.f.planned.goal_inspection, 'inspect', side_effect=TypeError('Private programming defect')) as inspect:
            response = await self.drain()
            self.assertEqual(response.status_code, 500)
            inspect.assert_called_once()
        current = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
        self.assertFalse(current.maintenance_mode)
        self.assertEqual(current.home_checkpoint_disposition.outcome, 'pending_unknown')
        with patch.object(self.f.planned.goal_inspection, 'inspect', side_effect=AssertionError('Must not repeat UNKNOWN optional owner')):
            response = await self.drain()
        self.assertEqual(response.status_code, 200)
        current = self.f.upgrades.get(self.f.plan.id, actor=self.f.operator)
        self.assertTrue(current.maintenance_mode)
        self.assertEqual(current.home_checkpoint_disposition.outcome, 'deferred_unknown')
        self.assertEqual(current.home_checkpoint_disposition.reason_code, 'home_checkpoint_original_outcome_unknown')
        self.assertFalse(current.home_checkpoint_disposition.continuation_authorized)

    async def test_selection_drift_after_commit_keeps_actual_artifact_without_continuation_or_retry(self):
        navigation, status, calls = self.drifting_status(after=5)
        writer = self.f.artifacts.create_artifact
        created = []
        def commit(*args, **kwargs):
            artifact = writer(*args, **kwargs)
            created.append(artifact.id)
            return artifact
        with patch.object(navigation, 'status', side_effect=status), \
                patch.object(self.f.artifacts, 'create_artifact', side_effect=commit):
            current = self.assert_blocked_drain(await self.drain())
            self.assertEqual(len(calls), 6)
            self.assertEqual(created, [current.home_checkpoint_disposition.preservation_artifact_id])
            self.assertEqual((await self.drain()).status_code, 200)
            self.assertEqual(len(created), 1)
        actual = self.f.artifacts.get_artifact(created[0], actor=self.f.actor)
        self.assertEqual(actual.source, 'home-upgrade-dormant-preservation')
        self.assertFalse(current.home_checkpoint_disposition.continuation_authorized)
        self.assertEqual(self.f.effects, 0)
