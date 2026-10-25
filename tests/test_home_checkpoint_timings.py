"""Closed stage diagnostics on the actual dormant Home preservation owners."""
import asyncio
import collections
import unittest
from unittest.mock import patch

from codex_web.observability import JsonFormatter, correlated
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services import home_checkpoint_telemetry as telemetry
from tests import test_home_unowned_upgrade as canonical


class HomeCheckpointTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = canonical.UnownedHomeUpgradeTests(
            methodName='test_actual_restart_projection_preserves_unknown_without_navigation_clear')
        await self.f.asyncSetUp()
        self.f.lose()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    def events(self, captured):
        rows = [record.structured for record in captured.records]
        allowed = {'checkpoint_current', 'preservation', 'initial_snapshot',
            'before_artifact_snapshot', 'final_snapshot', 'custody',
            'custody_authorization', 'goals', 'goal_inspection', 'status',
            'profile', 'session', 'held', 'artifact_write'}
        for record, row in zip(captured.records, rows):
            self.assertEqual(set(row), {'event', 'correlation_id', 'stage', 'outcome',
                'duration_seconds', 'tenant_id', 'workspace_id'})
            self.assertEqual(row['event'], 'home.checkpoint.stage')
            self.assertEqual(row['correlation_id'], 'home-private-stage-test')
            self.assertEqual(row['tenant_id'], self.f.actor.organization_id)
            self.assertEqual(row['workspace_id'], self.f.actor.workspace_id)
            self.assertIn(row['stage'], allowed)
            self.assertIn(row['outcome'], {'completed', 'refused'})
            self.assertGreaterEqual(row['duration_seconds'], 0)
            self.assertIsNone(record.exc_info)
            serialized = JsonFormatter().format(record)
            self.assertNotIn('PRIVATE-CONTENT', serialized)
            self.assertNotIn('dormant_preservation', serialized)
            self.assertNotIn('Traceback', serialized)
        return rows

    async def test_real_three_snapshot_hook_has_closed_component_timings(self):
        before = self.f.preserved()
        # A real canonical Profile contains private text; diagnostics never include it.
        self.f.profiles.store.update(lambda state: state.model_copy(update={
            'revisions': [row.model_copy(update={'description':'PRIVATE-CONTENT'})
                          for row in state.revisions]}))
        with correlated(correlation_id='home-private-stage-test'):
            with self.assertLogs(telemetry._logger, level='INFO') as captured:
                self.assertTrue(self.f.drain().maintenance_mode)
        rows = self.events(captured)
        counts = collections.Counter(row['stage'] for row in rows)
        self.assertEqual(counts['status'], 6)
        self.assertEqual(counts['profile'], 10)
        self.assertEqual(counts['goal_inspection'], 3)
        self.assertEqual(counts['held'], 3)
        for stage in ('initial_snapshot','before_artifact_snapshot','final_snapshot',
                      'artifact_write','preservation','checkpoint_current'):
            self.assertEqual(counts[stage], 1)
        self.assertTrue(all(row['outcome']=='completed' for row in rows))
        self.assertEqual(self.f.preserved(), before)
        self.assertEqual(self.f.effects, 0)
        self.assertIsNone(telemetry._scope.get())

    async def test_final_goal_timeout_is_attributed_without_hiding_committed_artifact(self):
        original = self.f.goal_bindings.list_all
        attempts = 0
        def read(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise TimeoutError('PRIVATE-CONTENT credentials and provider response')
            return original(*args, **kwargs)
        before = self.f.preserved()
        with correlated(correlation_id='home-private-stage-test'):
            with self.assertLogs(telemetry._logger, level='INFO') as captured:
                with patch.object(self.f.goal_bindings, 'list_all', side_effect=read):
                    with self.assertRaises(ExecutionPrestartRefusal) as failed:
                        self.f.drain()
        self.assertEqual(failed.exception.detail['phase'], 'final_snapshot')
        self.assertEqual(failed.exception.detail['exception_type'], 'TimeoutError')
        self.assertIs(failed.exception.detail['retryable'], False)
        rows = self.events(captured)
        refused = {row['stage'] for row in rows if row['outcome']=='refused'}
        self.assertEqual(refused, {'goal_inspection','goals','final_snapshot',
                                  'preservation','checkpoint_current'})
        self.assertIn(('artifact_write','completed'),
                      {(row['stage'],row['outcome']) for row in rows})
        self.assertIsNotNone(self.f.planned._dormant_artifact(self.f.plan.id,self.f.actor))
        self.assertFalse(self.f.upgrades.get(self.f.plan.id,actor=self.f.operator).maintenance_mode)
        self.assertEqual(self.f.preserved(),before)
        self.assertEqual(self.f.effects,0)
        self.assertIsNone(telemetry._scope.get())

    async def test_telemetry_failure_changes_neither_result_nor_scope_lifetime(self):
        before = self.f.preserved()
        with patch('codex_web.observability.log_event', side_effect=asyncio.CancelledError) as emit:
            self.assertTrue(self.f.drain().maintenance_mode)
            self.assertGreater(emit.call_count, 0)
            emit.reset_mock()
            self.f.planned.goal_inspection.operator.status(self.f.actor)
            emit.assert_not_called()
        self.assertEqual(self.f.preserved(),before)
        self.assertEqual(self.f.effects,0)
        self.assertIsNone(telemetry._scope.get())

    async def test_logging_failure_never_replaces_original_timeout(self):
        refusal = TimeoutError('PRIVATE-CONTENT original timeout')
        with patch('codex_web.observability.log_event', side_effect=asyncio.CancelledError):
            with patch.object(self.f.goal_bindings, 'list_all', side_effect=refusal):
                with self.assertRaises(ExecutionPrestartRefusal) as failed:
                    self.f.drain()
        self.assertIs(failed.exception.__cause__, refusal)
        self.assertEqual(failed.exception.detail['phase'], 'initial_snapshot')
        self.assertIsNone(self.f.planned._dormant_artifact(self.f.plan.id,self.f.actor))
        self.assertFalse(self.f.upgrades.get(self.f.plan.id,actor=self.f.operator).maintenance_mode)
        self.assertEqual(self.f.effects,0)
        self.assertIsNone(telemetry._scope.get())
