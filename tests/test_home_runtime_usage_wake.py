"""Real EventHub/adapter/telemetry ordering; no application or native inputs."""
from __future__ import annotations

import ast
import asyncio
import contextvars
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from codex_web.agent_runtime import AgentRuntimeSessionRequest
from codex_web.events import EventHub
from codex_web.identity import TenantScope
from codex_web.observability import correlation_fields
from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.native_recovery import NativeRecoveryService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget
from tests import test_home_operator_followup as home_fixture


class HomeRuntimeUsageWakeTests(unittest.IsolatedAsyncioTestCase):
    setUp = home_fixture.HomeFollowupTests.setUp
    publish = home_fixture.HomeFollowupTests.publish
    admit = home_fixture.HomeFollowupTests.admit
    launch = home_fixture.HomeFollowupTests.launch
    terminal_fixture = home_fixture.HomeFollowupTests.terminal_fixture

    async def prepare(self, *, checkpoint_succeeded=True):
        self.run = self.admit().run
        await self.launch(self.run)
        _, self.active = self.terminal_fixture(self.run)
        self.hub = EventHub()
        self.adapter = CodexAgentRuntimeAdapter(NS(host=NS(hub=self.hub)))
        registry = AgentRuntimeRegistry()
        registry.register(self.adapter)
        self.sessions = AgentSessionStore(self.sql)
        self.session = AgentSessionService(self.sessions, registry).adopt(
            provider_id='openai', runtime_id='codex', runtime_type=self.adapter.runtime_type,
            provider_native_session_id=self.active.thread_id,
            request=AgentRuntimeSessionRequest(project_id='home', execution_id=self.active.execution_id,
                assignment_id=self.active.assignment_id, execution_workspace_id=self.active.execution_workspace_id),
            actor=self.actor, capability_snapshot=self.adapter.capabilities, capability_revision=1)
        self.usage_store = AgentRuntimeUsageStore(self.sql)
        self.followup.sessions = self.sessions
        self.followup.runtime_usage = self.usage_store
        self.followup.outcomes.runtime_usage = self.usage_store
        self.telemetry = AgentRuntimeTelemetryService(self.usage_store, self.sessions, registry, clock=lambda: 102.)
        self.addCleanup(self.telemetry.close)
        self.telemetry.subscribe(self.adapter)
        self.cycle_contexts = []
        async def cycle():
            self.cycle_contexts.append(correlation_fields())
            await self.followup.recover_pending()
        self.other_cycles = []
        async def unrelated():
            self.other_cycles.append('ordinary-cycle')
        self.recovery = NativeRecoveryService(policy=NS(autonomy_enabled=lambda: True,
            native_recovery_cooldown=lambda: 600., native_recovery_cycle_timeout=lambda: 30.),
            cycles=(cycle, unrelated), append_event=lambda _: None)
        self.recovery.usage_cycle = cycle  # exact explicit production-selected cycle
        self.recovery.set_shutting_down(False)  # exact RuntimeSupervisor.start seam
        self.addAsyncCleanup(self.recovery.stop)
        tree = ast.parse(Path('codex_web/application.py').read_text())
        function = next(row for row in tree.body if isinstance(row, ast.FunctionDef)
            and row.name == '_notify_automation_runtime_usage')
        namespace = {'asyncio': asyncio, 'TenantScope': TenantScope,
            'identity_service': NS(bootstrap_service_actor=self.service_actor),
            'automation_outcome_reconciliation_service': self.followup.outcomes,
            'app': NS(state=NS(native_recovery_service=self.recovery))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual usage composition>', 'exec'), namespace)
        self.notifier_threads = []
        def persisted(record):
            # The actual telemetry owner must commit BEFORE its notifier; this
            # assertion does not infer ordering from provider completed_at.
            self.assertEqual(self.usage_store.get(record.id), record)
            self.notifier_threads.append(threading.get_ident())
            namespace['_notify_automation_runtime_usage'](record)
        self.notifier_namespace = namespace
        self.telemetry.observation_notifier = persisted
        self.followup.scheduler = SchedulerService(SchedulerStore(self.sql),
            CanonicalEventIngestionService(self.bus), clock=lambda: 101.)
        self.addCleanup(self.bus.subscribe(self.followup.on_schedule, event_types=('schedule.due',)))
        await self.followup.record_terminal(self.active, succeeded=checkpoint_succeeded)
        # Execute the real one-shot scheduler event while usage is absent.
        await self.followup.scheduler.run_due()
        self.assertEqual(self.current().result_code, 'home_followup_budget_pending')
        self.assertIsNotNone(self.current().home_terminal)
        self.assertEqual(self.usage_store.list(), [])
        self.assertTrue(self.recovery.schedule(reason='prior-ordinary-recovery'))
        await self.join_recovery()
        self.assertFalse(self.recovery.schedule(reason='ordinary-cooldown'))
        self.assertEqual(self.other_cycles, ['ordinary-cycle'])

    def service_actor(self, **kwargs):
        self.assertEqual(kwargs['scope'], TenantScope(organization_id='local', workspace_id='default'))
        self.assertEqual(kwargs['service_scopes'], ('automation:execute',))
        return self.actor

    def current(self):
        return self.runs.store.get(self.run.id, organization_id='local', workspace_id='default')

    async def join_recovery(self):
        # Let an already-submitted loop callback run, then join ONLY actual
        # coordinator owners; never call recover_pending as a test workaround.
        await asyncio.sleep(0)
        while self.recovery.tasks:
            await asyncio.gather(*tuple(self.recovery.tasks))
            await asyncio.sleep(0)

    async def emit(self, *, method='turn/completed', usage=True):
        params = {'threadId': self.active.thread_id, 'turnId': self.active.turn_id}
        message = {'method': method, 'params': params}
        if usage:
            message['usage'] = {'input_tokens': 20, 'output_tokens': 10}
        await self.hub.publish({'type': 'codex.event', 'message': message,
            'correlation_id': 'same-usage-correlation', 'workspace_id': 'default',
            'execution_id': self.active.execution_id})
        await self.join_recovery()

    async def test_late_worker_usage_settles_exact_home_run_after_single_wake_and_during_cooldown(self):
        await self.prepare()
        witness, custody = self.current().home_terminal, self.current().home_custody
        await self.emit()
        self.assertEqual(self.current().status, 'succeeded')
        self.assertEqual(self.current().home_terminal, witness)
        self.assertEqual(self.current().home_custody, custody)
        self.assertEqual(self.current().execution_ids, ('home-followup-' + self.run.id,))
        self.assertTrue(all(value != threading.get_ident() for value in self.notifier_threads))
        self.assertEqual(self.cycle_contexts[-1]['correlation_id'], 'same-usage-correlation')
        self.assertEqual(self.other_cycles, ['ordinary-cycle'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.runs.store.load().runs), 1)

    async def test_generic_identity_reconciliation_failure_does_not_drop_home_terminal_wake(self):
        await self.prepare()
        def unavailable(**kwargs):
            raise ValueError('unrelated generic reconciler identity refusal')
        self.notifier_namespace['identity_service'] = NS(bootstrap_service_actor=unavailable)
        await self.emit()
        self.assertEqual(self.current().status, 'succeeded')
        self.assertEqual(len(self.calls), 1)

    async def test_duplicate_worker_usage_preserves_terminal_and_does_not_send_again(self):
        await self.prepare()
        await self.emit()
        done = self.current()
        self.assertEqual(done.status, 'succeeded')
        await self.emit()
        self.assertEqual(self.current(), done)
        self.assertEqual(len(self.usage_store.list()), 1)
        self.assertEqual(len(self.calls), 1)

    async def test_nonterminal_usage_keeps_pending_until_real_terminal_usage(self):
        await self.prepare()
        cycles = len(self.cycle_contexts)
        await self.emit(method='turn/started', usage=False)
        self.assertEqual(len(self.cycle_contexts), cycles)
        self.assertEqual(self.current().status, 'running')
        self.assertEqual(self.current().result_code, 'home_followup_budget_pending')
        await self.emit()
        self.assertEqual(self.current().status, 'succeeded')
        self.assertEqual(len(self.calls), 1)


class RuntimeUsageBridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_burst_is_one_pending_wake_preserving_context_without_parent_budget(self):
        seen = []
        original = contextvars.ContextVar('test-actor-tenant-context', default=None)
        async def cycle():
            seen.append((original.get(), storage_operation_budget.get()))
        service = NativeRecoveryService(policy=NS(autonomy_enabled=lambda: True,
            native_recovery_cooldown=lambda: 600.), cycles=(cycle,), append_event=lambda _: None)
        service.usage_cycle = cycle
        service.set_shutting_down(False)
        self.addAsyncCleanup(service.stop)
        accepted = []
        def publish():
            original.set(('same-actor', 'same-tenant'))
            storage_operation_budget.set(StorageOperationBudget(0))
            accepted.extend(service.notify_runtime_usage() for _ in range(100))
        thread = threading.Thread(target=publish)
        thread.start()
        thread.join()  # deterministic burst before the owner loop can drain
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(service.tasks))
        self.assertEqual(sum(accepted), 1)
        self.assertEqual(seen, [(('same-actor', 'same-tenant'), None)])
        self.assertEqual(service.coordinator.status()['scheduled'], 1)

    async def test_usage_wake_does_not_postpone_ordinary_recovery_cooldown(self):
        calls = []
        async def home():
            calls.append('home')
        async def other():
            calls.append('other')
        service = NativeRecoveryService(policy=NS(autonomy_enabled=lambda: True,
            native_recovery_cooldown=lambda: 600.), cycles=(home, other),
            usage_cycle=home, append_event=lambda _: None)
        service.set_shutting_down(False)
        self.addAsyncCleanup(service.stop)
        clock = [1000.]
        with patch('codex_web.services.native_recovery.time.time', side_effect=lambda: clock[0]):
            self.assertTrue(service.schedule(reason='ordinary-first'))
            await asyncio.gather(*tuple(service.tasks))
            clock[0] = 1601.
            await asyncio.to_thread(service.notify_runtime_usage)
            await asyncio.sleep(0)
            await asyncio.gather(*tuple(service.tasks))
            self.assertEqual(calls.count('other'), 1)
            self.assertEqual(service.last_scheduled_at, 1000.)
            self.assertTrue(service.schedule(reason='ordinary-after-cooldown'))
            await asyncio.gather(*tuple(service.tasks))
        self.assertEqual(calls.count('other'), 2)

    async def test_shutdown_cancels_pending_wake_and_rejects_late_worker(self):
        calls = []
        async def cycle():
            calls.append('unexpected')
        service = NativeRecoveryService(policy=NS(autonomy_enabled=lambda: True,
            native_recovery_cooldown=lambda: 0.), cycles=(cycle,), append_event=lambda _: None)
        self.assertFalse(await asyncio.to_thread(service.notify_runtime_usage))
        service.usage_cycle = cycle
        service.set_shutting_down(False)
        thread = threading.Thread(target=service.notify_runtime_usage)
        thread.start()
        thread.join()
        await service.stop()
        await asyncio.sleep(0)
        self.assertFalse(await asyncio.to_thread(service.notify_runtime_usage))
        self.assertEqual(calls, [])
        self.assertEqual(service.tasks, set())

    async def test_shutdown_joins_running_usage_recovery(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        async def cycle():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        service = NativeRecoveryService(policy=NS(autonomy_enabled=lambda: True,
            native_recovery_cooldown=lambda: 0.), cycles=(cycle,), append_event=lambda _: None)
        service.usage_cycle = cycle
        service.set_shutting_down(False)
        await asyncio.to_thread(service.notify_runtime_usage)
        await entered.wait()
        await service.stop()
        self.assertTrue(finished.is_set())
        self.assertEqual(service.tasks, set())


class HistoricalHomeUsageCustodyTests(unittest.IsolatedAsyncioTestCase):
    """Real Home custody rollover plus the real worker telemetry/accounting path."""
    setUp = home_fixture.HomeFollowupTests.setUp
    admit = home_fixture.HomeFollowupTests.admit
    launch = home_fixture.HomeFollowupTests.launch
    current = HomeRuntimeUsageWakeTests.current
    join_recovery = HomeRuntimeUsageWakeTests.join_recovery
    emit = HomeRuntimeUsageWakeTests.emit
    service_actor = HomeRuntimeUsageWakeTests.service_actor

    def publish(self, publisher=None, identifier='home-ci'):
        return home_fixture.HomeFollowupTests.publish(self, publisher or self.actor.identity_id, identifier)

    def terminal_fixture(self, run):
        a = self.host_fixture.assignment
        self.followup.workers = self.host_fixture.f.workers
        self.followup.bootstrap_bindings = self.host_fixture.bindings
        active = NS(source='queued:home-operator-automation:' + run.id,
            turn_id='actual-native-turn', thread_id='operator-thread', assignment_id=a.id,
            fence=a.fence, execution_id=a.execution_id, worker_id=a.assigned_worker_id,
            execution_workspace_id=a.execution_workspace_id)
        return a, active

    async def prepare_historical(self, *, checkpoint_succeeded=True):
        from tests import test_home_operator_host as host_fixture
        from codex_web.services.automation_definitions import install_automation_definitions, AutomationEventTriggerService
        from codex_web.services.automation_runs import AutomationRunService
        from codex_web.services.canonical_events import CanonicalEventBus
        from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
        from codex_web.storage.automation_runs import AutomationRunStore
        from codex_web.storage.canonical_events import CanonicalEventStore
        e = self.host_fixture = host_fixture.HomeOperatorHostTests()
        e.addCleanup, e.addAsyncCleanup = self.addCleanup, self.addAsyncCleanup
        await e.asyncSetUp()
        self.sql, self.registry, self.actor = e.f.sqlite, e.registry, e.actor
        self.definitions = install_automation_definitions(self.registry)
        self.runs = AutomationRunService(AutomationRunStore(self.sql), self.definitions, clock=lambda: 100.)
        self.events = CanonicalEventStore(self.sql)
        self.bus = CanonicalEventBus(self.events)
        self.triggers = AutomationEventTriggerService(self.registry, self.bus)
        self.selected = {'status':'configured', 'binding':{'revision':2,
            'selected_by':self.actor.identity_id, 'agent_profile_id':e.assignment.agent_profile.profile_id,
            'agent_profile_revision':e.assignment.agent_profile.profile_revision},
            'thread':{'id':'operator-thread', 'execution_profile_id':'orchestration-only'}}
        self.payload['owner_identity_id'] = self.actor.identity_id
        self.payload['target'].update(id=e.assignment.agent_profile.profile_id,
            agent_profile_revision=e.assignment.agent_profile.profile_revision, home_thread_id='operator-thread')
        self.publish()
        self.followup.runs, self.followup.events, self.followup.event_triggers = self.runs, self.events, self.triggers
        self.followup.identity = e.f.identity
        self.followup.workers, self.followup.bootstrap_bindings = e.f.workers, e.bindings
        self.execution.runs, self.execution.events, self.execution.identity = self.runs, self.events, e.f.identity
        self.followup.outcomes = AutomationOutcomeReconciliationService(self.runs, None)
        # Reuse the actual EventHub→adapter→telemetry→application notifier seam,
        # real one-shot wake, and existing Home recovery coordinator.
        await HomeRuntimeUsageWakeTests.prepare(self, checkpoint_succeeded=checkpoint_succeeded)
        await self.emit(method='turn/started', usage=False)
        usage = self.usage_store.list()[0]
        self.assertEqual((usage.agent_session_id, usage.assignment_id, usage.execution_id),
            (self.session.id, self.active.assignment_id, self.active.execution_id))
        self.old_usage = usage

    def reassociate(self):
        from codex_web.agent_runtime import AgentRuntimeResult
        from codex_web.execution_workers import AssignmentCancelRequest, AssignmentClaimRequest, AssignmentStartRequest
        from codex_web.services.agent_session_custody import AgentSessionCustodyService
        e = self.host_fixture
        self.old_assignment = old = e.f.workers.store.assignment(self.active.assignment_id)
        e.f.workers.cancel_bootstrap(old.id, AssignmentCancelRequest(expected_fence=old.fence,
            reason='Completed Home turn custody rollover'), actor=e.actor)
        binding = e.f.service.prepare_bootstrap(bootstrap_id='historical-successor', execution_id='historical-successor-execution',
            project_id='home', sandbox=old.sandbox, approval_policy=old.approval_policy,
            execution_profile_id=old.execution_profile_id, agent_profile=old.agent_profile)
        claimed = e.f.workers.claim(e.f.worker.id, AssignmentClaimRequest(), actor=e.worker_actor,
            assignment_id=binding.assignment_id)
        successor = e.f.workers.start(e.f.worker.id, binding.assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=e.worker_actor)
        e.bindings.rebind(bootstrap_id='historical-successor', thread_id='operator-thread',
            execution_id=successor.execution_id, assignment_id=successor.id,
            execution_workspace_id=successor.execution_workspace_id, actor=e.actor)
        workspace = e.f.workspaces.store.workspace(successor.execution_workspace_id)
        worker_session = NS(workspace_path=Path(workspace.path),
            validate_current=lambda:e.f.workers.store.assignment(successor.id))
        request = AgentRuntimeSessionRequest(project_id='home', assignment_id=successor.id,
            execution_id=successor.execution_id, execution_workspace_id=workspace.id,
            resource_ids=successor.resource_ids, worker_id=e.f.worker.id, workspace_cwd=workspace.path)
        custody = AgentSessionCustodyService(e.sessions, e.f.workers, e.bindings, e.f.workspaces, e.f.execution_profiles)
        adapter = NS(provider_id=self.session.provider_id, runtime_id=self.session.runtime_id,
            runtime_type=self.session.runtime_type)
        before = self.usage_store.get(self.old_usage.id)
        current = custody.complete(custody.capture('operator-thread', adapter, worker_session, request, successor.fence),
            'operator-thread', adapter, worker_session, request, successor.fence,
            AgentRuntimeResult(provider_native_session_id='operator-thread'))
        self.assertEqual(current.id, self.session.id)
        self.assertEqual((current.assignment_id, current.execution_id), (successor.id, successor.execution_id))
        self.assertEqual(self.usage_store.get(self.old_usage.id), before)
        self.assertNotEqual(current.assignment_id, old.id)
        return current

    async def test_real_custody_reassociation_then_late_worker_usage_settles_original_without_input(self):
        await self.prepare_historical()
        witness, custody = self.current().home_terminal, self.current().home_custody
        current = self.reassociate()
        await self.emit()
        self.assertEqual(self.current().status, 'succeeded')
        self.assertEqual((self.current().home_terminal, self.current().home_custody), (witness, custody))
        record = self.usage_store.get(self.old_usage.id)
        self.assertEqual((record.agent_session_id, record.assignment_id, record.execution_id,
            record.execution_workspace_id), (current.id, self.old_assignment.id,
            self.old_assignment.execution_id, self.old_assignment.execution_workspace_id))
        self.assertEqual(self.sessions.get(current.id, organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id), current)
        self.assertEqual(len(self.calls), 1)

    async def test_wrong_historical_usage_attribution_remains_pending(self):
        await self.prepare_historical()
        self.reassociate()
        changes = ({'organization_id':'foreign'}, {'workspace_id':'foreign'}, {'project_id':'foreign'},
            {'agent_session_id':'another-session'}, {'assignment_id':'another-assignment'},
            {'execution_id':'another-execution'}, {'execution_workspace_id':'another-workspace'},
            {'provider_id':'another-provider'}, {'runtime_id':'another-runtime'},
            {'provider_native_session_id':'another-native-session'}, {'provider_native_turn_id':'another-turn'})
        for change in changes:
            with self.subTest(change=change):
                # Deliberately malformed historical attribution fixture; the
                # service must refuse it, not repair or borrow current custody.
                self.sql.record_apply(self.usage_store.namespace, upserts={self.old_usage.id:
                    self.old_usage.model_copy(update=change).model_dump(mode='json')})
                await self.emit()
                self.assertEqual(self.current().status, 'running')
                self.assertIsNone(self.current().completed_at)
        self.sql.record_apply(self.usage_store.namespace, upserts={self.old_usage.id:
            self.old_usage.model_dump(mode='json')})
        await self.emit()
        self.assertEqual(self.current().status, 'succeeded')
        self.assertEqual(len(self.calls), 1)

    async def test_known_failed_checkpoint_is_not_overridden_by_successful_historical_usage(self):
        await self.prepare_historical(checkpoint_succeeded=False)
        witness = self.current().home_terminal
        self.reassociate()
        await self.emit()
        self.assertFalse(witness.checkpoint_succeeded)
        self.assertEqual(self.current().home_terminal, witness)
        self.assertEqual(self.current().status, 'failed')
        self.assertEqual(self.current().result_code, 'home_followup_failed')
        self.assertEqual(self.usage_store.get(self.old_usage.id).terminal_outcome, 'succeeded')
        self.assertEqual(len(self.calls), 1)
