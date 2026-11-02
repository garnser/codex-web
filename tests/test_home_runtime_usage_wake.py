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

    async def prepare(self):
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
        await self.followup.record_terminal(self.active, succeeded=True)
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
