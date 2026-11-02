"""Real retained owner/event/Run/SQLite boundaries; Home/native edges are fixtures."""
from __future__ import annotations

import ast
import asyncio
import contextvars
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.automation_runs import AutomationTriggerAdmissionBridge
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.state_store import storage_operation_budget
from tests import test_home_operator_followup as home_fixture
from tests import test_turn_execution as turn_fixture


class HomeFollowupLaunchOwnerTests(unittest.IsolatedAsyncioTestCase):
    setUp = home_fixture.HomeFollowupTests.setUp
    publish = home_fixture.HomeFollowupTests.publish
    admit = home_fixture.HomeFollowupTests.admit
    launch = home_fixture.HomeFollowupTests.launch

    async def owner_setup(self):
        self.coordinator = KeyedTaskCoordinator(max_concurrency=2, per_scope_concurrency=1)
        self.addAsyncCleanup(self.coordinator.stop)
        self.followup.launch_coordinator = self.coordinator
        self.followup.scheduler = SchedulerService(SchedulerStore(self.sql),
            CanonicalEventIngestionService(self.bus), clock=lambda: 100.)
        tree = ast.parse(Path('codex_web/application.py').read_text())
        function = next(row for row in tree.body if isinstance(row, ast.AsyncFunctionDef)
            and row.name == '_launch_admitted_automation')
        namespace = {'automation_execution_service': self.execution,
            'home_operator_followup': self.followup}
        exec(compile(ast.Module(body=[function], type_ignores=[]),
            '<actual Home admission handoff>', 'exec'), namespace)
        self.handoff = namespace['_launch_admitted_automation']
        self.bridge = AutomationTriggerAdmissionBridge(self.runs, self.triggers, self.bus,
            launch_handler=self.handoff)
        self.bridge.install()

    def event(self, event_id='actual-fixture-terminal-event'):
        return CanonicalEventEnvelope(event_id=event_id, event_type='ci.pipeline',
            occurred_at=95., source='trusted-fixture-child-terminal', tenant_id='local',
            workspace_id='default', payload={'project_id': 'delegated-project'})

    async def join_launches(self):
        while self.coordinator._tasks:
            await asyncio.gather(*tuple(self.coordinator._tasks.values()))
            await asyncio.sleep(0)

    async def test_expired_retained_callback_budget_after_claim_does_not_strand_launch(self):
        await self.owner_setup()
        origin = contextvars.ContextVar('test-original-authority-context', default=None)
        token = origin.set('same-tenant-actor-and-correlation')
        self.addCleanup(origin.reset, token)
        holder, seen = {}, []
        original_claim = self.followup._claim
        def expire_parent_after_real_commit(*args):
            result = original_claim(*args)
            if result[0]:
                seen.append((storage_operation_budget.get(), origin.get()))
                # Deterministic expiration of the actual retained owner's
                # budget after its canonical Run claim, without a timing SLA.
                holder['parent_budget'].deadline = 0.
            return result
        self.followup._claim = expire_parent_after_real_commit
        host = turn_fixture._Host()
        events = []
        host._append_bot_event = events.append
        runtime = TurnExecutionService(host)
        async def terminal_recorder(active, *, succeeded):
            holder['parent_budget'] = storage_operation_budget.get()
            await self.bus.publish(self.event(), idempotency_key='original-terminal-event')
        runtime.retained_turn_outcome_recorder = terminal_recorder
        await runtime._record_retained_outcome(NS(assignment_id='child',
            thread_id='child-thread', turn_id='child-turn'), succeeded=True)
        await self.join_launches()
        runs = self.runs.store.load().runs
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.calls), 1,
            'A joined retained callback failure must not strand the durable Home admission')
        self.assertIsNot(seen[0][0], holder['parent_budget'])
        self.assertEqual(seen[0][1], 'same-tenant-actor-and-correlation')
        self.assertEqual(runs[0].execution_ids, ('home-followup-' + runs[0].id,))
        self.assertNotIn('retained_turn_outcome_unconfirmed', [row['type'] for row in events])
        self.assertEqual(len(self.followup.scheduler.list()), 1)

    async def test_repeated_handoff_coalesces_and_running_run_is_never_resent(self):
        await self.owner_setup()
        run = self.admit().run
        for _ in range(4):
            await self.handoff(run)
        await self.join_launches()
        current = self.runs.store.get(run.id, organization_id='local', workspace_id='default')
        self.assertEqual(current.status, 'running')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.followup.schedule_launch(current))
        await self.followup.recover_pending()
        await self.join_launches()
        self.assertEqual(len(self.calls), 1)

    async def test_stopped_owner_leaves_admitted_run_recoverable_after_startup(self):
        await self.owner_setup()
        await self.coordinator.stop()
        run = self.admit().run
        self.assertFalse(self.followup.schedule_launch(run))
        self.assertEqual(self.runs.store.get(run.id, organization_id='local',
            workspace_id='default').status, 'admitted')
        self.assertFalse(self.calls)
        self.coordinator = KeyedTaskCoordinator()
        self.addAsyncCleanup(self.coordinator.stop)
        self.followup.launch_coordinator = self.coordinator
        await self.followup.recover_pending()
        await self.join_launches()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.runs.store.load().runs), 1)

    async def test_shutdown_joins_claim_writer_and_preserves_unknown_without_resend(self):
        await self.owner_setup()
        entered, release = threading.Event(), threading.Event()
        observed = {}
        original_claim = self.followup._claim
        def hold_after_commit(*args):
            result = original_claim(*args)
            if result[0]:
                observed['budget'] = storage_operation_budget.get()
                entered.set()
                if not release.wait(15):
                    raise AssertionError('test writer barrier was not released')
            return result
        self.followup._claim = hold_after_commit
        run = self.admit().run
        self.followup.schedule_launch(run)
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 15)
        stop = asyncio.create_task(self.coordinator.stop())
        try:
            await asyncio.wait_for(asyncio.to_thread(observed['budget'].cancelled.wait), 15)
            self.assertFalse(stop.done(), 'Shutdown must join the actual canonical writer')
        finally:
            release.set()
        await stop
        current = self.runs.store.get(run.id, organization_id='local', workspace_id='default')
        self.assertEqual(current.status, 'running')
        self.assertEqual(current.result_code, 'home_followup_launch_unconfirmed')
        self.assertIn('native outcome is not overridden', current.result_reason)
        self.assertFalse(self.calls)
        self.coordinator = KeyedTaskCoordinator()
        self.addAsyncCleanup(self.coordinator.stop)
        self.followup.launch_coordinator = self.coordinator
        await self.followup.recover_pending()
        await self.join_launches()
        self.assertFalse(self.calls)

    async def test_joined_launch_exception_is_visible_without_reset_or_replay(self):
        await self.owner_setup()
        def fail_schedule(*args, **kwargs):
            raise ValueError('fixture post-claim scheduling refusal')
        self.followup._schedule = fail_schedule
        run = self.admit().run
        self.followup.schedule_launch(run)
        await self.join_launches()
        current = self.runs.store.get(run.id, organization_id='local', workspace_id='default')
        self.assertEqual(current.status, 'running')
        self.assertEqual(current.result_code, 'home_followup_launch_unconfirmed')
        self.assertIn('ValueError', current.result_reason)
        self.assertIn('native outcome is not overridden', current.result_reason)
        self.assertFalse(self.calls)
        self.assertFalse(self.followup.schedule_launch(current))

    async def test_foreign_or_changed_admission_cannot_use_detached_owner(self):
        await self.owner_setup()
        run = self.admit().run
        stale = run.model_copy(update={'project_id': 'foreign-project'})
        self.followup.schedule_launch(stale)
        await self.join_launches()
        self.assertFalse(self.calls)
        current = self.runs.store.get(run.id, organization_id='local', workspace_id='default')
        self.assertEqual(current.status, 'admitted')
        self.assertEqual(current, run)
