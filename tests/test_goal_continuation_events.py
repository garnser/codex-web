from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from codex_web.agent_runtime import AgentRuntimeEvent, AgentSessionStatus
from codex_web.events import EventHub
from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
from codex_web.identity import TenantScope
from codex_web.services.goal_continuation import GoalContinuationDispatchResult
from codex_web.services.goal_continuation_events import GoalContinuationEventService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter


class _Bindings:
    def __init__(self, binding):
        self.binding = binding
        self.releases = []

    def list_all(self, *, scope):
        return (self.binding,)

    def release_continuation(self, binding_id, **kwargs):
        self.releases.append((binding_id, kwargs))
        self.binding = SimpleNamespace(
            **{
                **self.binding.__dict__,
                "lease_owner_id": None,
                "status": kwargs["status"],
                "recovery_attempts": self.binding.recovery_attempts
                + (1 if kwargs.get("increment_recovery") else 0),
            }
        )
        return self.binding


class _Sessions:
    def __init__(self, session_id="session-a"):
        self.session_id = session_id
        self.status_updates = []

    def find_by_native_id(self, native_id, actor, **kwargs):
        if native_id != "native-session-a":
            return None
        return SimpleNamespace(id=self.session_id)

    def mark_status(
        self,
        session_id,
        status,
        *,
        actor,
        failure_reason=None,
    ):
        self.status_updates.append(
            (session_id, status, failure_reason)
        )
        return SimpleNamespace(id=session_id, status=status)


class _Continuation:
    def __init__(self, bindings):
        self.owner_id = "worker-a"
        self.max_recovery_attempts = 4
        self.retry_base_seconds = 5.0
        self.bindings = bindings
        self.resolved = []
        self.dispatched = []

    def recovery_attempt_limit_for(self, binding, *, scope):
        return self.max_recovery_attempts

    def resolve_turn(
        self,
        binding_id,
        *,
        scope,
        reason,
        terminal_status=None,
        now=None,
    ):
        self.resolved.append(
            (binding_id, scope, reason, terminal_status, now)
        )
        self.bindings.binding = SimpleNamespace(
            **{
                **self.bindings.binding.__dict__,
                "lease_owner_id": None,
                "status": terminal_status or GoalExecutionBindingStatus.IDLE,
            }
        )
        return self.bindings.binding

    async def dispatch_once(self, binding_id, *, scope, now=None):
        self.dispatched.append((binding_id, scope, now))
        return GoalContinuationDispatchResult(
            binding_id=binding_id,
            outcome="started",
            turn_id="turn-next",
        )


def _binding(**changes):
    values = {
        "id": "binding-a",
        "agent_session_id": "session-a",
        "last_turn_id": "turn-a",
        "status": GoalExecutionBindingStatus.ACTIVE,
        "lease_owner_id": "worker-a",
        "recovery_attempts": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _event(event_type, *, turn_id="turn-a", session_id="native-session-a"):
    return AgentRuntimeEvent(
        event_type=event_type,
        provider_native_session_id=session_id,
        provider_native_turn_id=turn_id,
    )


class GoalContinuationEventServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )

    async def test_completed_turn_releases_and_dispatches_next_bounded_turn(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        sessions = _Sessions()
        service = GoalContinuationEventService(
            bindings,
            sessions,
            continuation,
        )

        result = await service.handle_event(
            _event("turn.completed"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "continued")
        self.assertEqual(result.binding_id, "binding-a")
        self.assertEqual(result.continuation.turn_id, "turn-next")
        self.assertEqual(
            continuation.resolved[-1][3],
            GoalExecutionBindingStatus.IDLE,
        )
        self.assertEqual(
            continuation.dispatched,
            [("binding-a", self.scope, 100.0)],
        )
        self.assertEqual(
            sessions.status_updates[-1][1],
            AgentSessionStatus.READY,
        )

    async def test_failed_turn_releases_with_retry_backoff(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn/failed"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "retry_scheduled")
        release = bindings.releases[-1][1]
        self.assertEqual(release["status"], GoalExecutionBindingStatus.FAILED)
        self.assertEqual(release["retry_after_seconds"], 5.0)
        self.assertTrue(release["increment_recovery"])

    async def test_failed_turn_blocks_when_retry_budget_is_exhausted(self):
        bindings = _Bindings(_binding(recovery_attempts=3))
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.failed"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "blocked")
        release = bindings.releases[-1][1]
        self.assertEqual(release["status"], GoalExecutionBindingStatus.BLOCKED)
        self.assertIsNone(release["retry_after_seconds"])

    async def test_interrupted_turn_releases_without_automatic_redispatch(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.interrupted"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "interrupted")
        self.assertEqual(continuation.dispatched, [])
        self.assertEqual(
            continuation.resolved[-1][3],
            GoalExecutionBindingStatus.IDLE,
        )

    async def test_duplicate_or_foreign_owner_event_is_ignored(self):
        bindings = _Bindings(_binding(lease_owner_id=None))
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.completed"),
            scope=self.scope,
        )

        self.assertEqual(result.outcome, "ignored")
        self.assertEqual(result.reason, "continuation_lease_not_owned")
        self.assertEqual(continuation.resolved, [])
        self.assertEqual(continuation.dispatched, [])

    async def test_application_observer_filters_streams_before_registry_access(self):
        # Load only the composition function: importing application would open
        # deployment state and start unrelated integration dependencies.
        tree = ast.parse((Path(__file__).parents[1] / "codex_web/application.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_subscribe_goal_continuation_events")
        store = Mock()
        store.list.return_value = []
        namespace = {"agent_session_store": store, "asyncio": asyncio,
                     "goal_continuation_event_service": GoalContinuationEventService,
                     "TenantScope": TenantScope}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "observer", "exec"), namespace)
        adapter = Mock()
        namespace[function.name](adapter, provider_id="openai", runtime_id="codex")
        listener = adapter.subscribe_events.call_args.args[0]
        for event_type in ("item.started", "item/agentMessage/delta", "thread/status/changed"):
            listener(_event(event_type))
        store.list.assert_not_called()
        loop = asyncio.get_running_loop()
        create_task = loop.create_task
        owned = []
        def track(coroutine, *args, **kwargs):
            task = create_task(coroutine, *args, **kwargs)
            owned.append(task)
            return task
        with patch.object(loop, "create_task", side_effect=track):
            for event_type in ("turn.completed", "turn/failed", "turn.interrupted"):
                listener(_event(event_type))
            await asyncio.gather(*owned)
        self.assertEqual(store.list.call_count, 3)

    async def test_application_terminal_observer_receives_real_event_hub_publication(self):
        await self._check_real_hub_publication(import_time=False)

    async def test_import_time_subscription_uses_existing_execution_owner_loop(self):
        await self._check_real_hub_publication(import_time=True)

    async def _check_real_hub_publication(self, *, import_time):
        # Exercise the production subscription boundary without importing App or
        # replacing EventHub's worker-thread delivery with direct callbacks.
        tree = ast.parse((Path(__file__).parents[1] / "codex_web/application.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_subscribe_goal_continuation_events")
        loop = asyncio.get_running_loop()
        calls = []
        handled_tasks = set()

        async def handle_event(event, *, scope):
            handled_tasks.add(asyncio.current_task())
            calls.append((event, scope, asyncio.get_running_loop()))

        service = SimpleNamespace(
            is_terminal_event=GoalContinuationEventService.is_terminal_event,
            handle_event=handle_event,
        )
        store = Mock()
        session = SimpleNamespace(
            provider_native_session_id="native-session-a", provider_id="openai",
            runtime_id="codex", organization_id="org-a", workspace_id="workspace-a",
        )
        lookup_threads = []
        def lookup():
            lookup_threads.append(threading.get_ident())
            return [session]
        store.list.side_effect = lookup
        namespace = {"agent_session_store": store, "asyncio": asyncio,
                     "goal_continuation_event_service": service, "TenantScope": TenantScope,
                     "turn_execution_service": SimpleNamespace(_event_loop=loop)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "observer", "exec"), namespace)
        hub = EventHub()
        adapter = CodexAgentRuntimeAdapter(SimpleNamespace(host=SimpleNamespace(hub=hub)))
        subscribe = namespace[function.name]
        if import_time:
            unsubscribe = await asyncio.to_thread(subscribe, adapter,
                provider_id="openai", runtime_id="codex")
        else:
            unsubscribe = subscribe(adapter, provider_id="openai", runtime_id="codex")

        async def publish(method, native_id="native-session-a"):
            await hub.publish({"type": "codex.event", "message": {
                "method": method, "params": {"threadId": native_id,
                    "turn": {"id": "turn-a", "threadId": native_id}},
            }})
            # Publication has joined its worker listener. Explicit loop barriers
            # drain thread-safe scheduling and the resulting handler task;
            # absence on OLD fails an assertion rather than waiting forever.
            for _ in range(2):
                barrier = loop.create_future()
                loop.call_soon(barrier.set_result, None)
                await barrier
            if handled_tasks:
                await asyncio.gather(*handled_tasks)

        try:
            await publish("item/started")
            store.list.assert_not_called()
            self.assertEqual(calls, [])
            await publish("turn/completed", "unmatched-native-session")
            self.assertEqual(store.list.call_count, 1)
            self.assertEqual(calls, [])
            await publish("turn/completed")
            self.assertEqual(store.list.call_count, 2)
            self.assertEqual(len(calls), 1,
                "matching terminal publication must reach Goal handling from the real EventHub")
            event, scope, handled_loop = calls[0]
            self.assertEqual(event.provider_native_session_id, "native-session-a")
            self.assertEqual(event.provider_native_turn_id, "turn-a")
            self.assertEqual(scope, TenantScope(organization_id="org-a", workspace_id="workspace-a"))
            self.assertIs(handled_loop, loop)
            self.assertTrue(all(thread != threading.get_ident() for thread in lookup_threads))
            unsubscribe()
            await publish("turn/completed")
            self.assertEqual(len(calls), 1)
            self.assertEqual(store.list.call_count, 2)
        finally:
            unsubscribe()
            if handled_tasks:
                await asyncio.gather(*handled_tasks)

    async def test_direct_loop_adapter_lookup_is_off_loop_and_owned(self):
        tree = ast.parse((Path(__file__).parents[1] / "codex_web/application.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_subscribe_goal_continuation_events")
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        calls, lookup_threads = [], []
        async def handle_event(event, *, scope):
            calls.append((event, scope, asyncio.get_running_loop()))
        store = Mock()
        def lookup():
            lookup_threads.append(threading.get_ident())
            return [SimpleNamespace(provider_native_session_id="native-session-a",
                provider_id="openai", runtime_id="codex", organization_id="org-a", workspace_id="workspace-a")]
        store.list.side_effect = lookup
        namespace = {"agent_session_store": store, "asyncio": asyncio,
            "goal_continuation_event_service": SimpleNamespace(
                is_terminal_event=GoalContinuationEventService.is_terminal_event, handle_event=handle_event),
            "TenantScope": TenantScope}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "observer", "exec"), namespace)
        adapter = Mock()
        unsubscribe = namespace[function.name](adapter, provider_id="openai", runtime_id="codex")
        listener = adapter.subscribe_events.call_args.args[0]
        owned = []
        create_task = loop.create_task
        def track(coroutine, *args, **kwargs):
            task = create_task(coroutine, *args, **kwargs)
            owned.append(task)
            return task
        try:
            with patch.object(loop, "create_task", side_effect=track):
                listener(_event("turn.completed"))
                await asyncio.gather(*owned)
            self.assertEqual(len(calls), 1)
            self.assertIs(calls[0][2], loop)
            self.assertEqual(len(lookup_threads), 1)
            self.assertNotEqual(lookup_threads[0], loop_thread)
        finally:
            unsubscribe()
            await asyncio.gather(*owned)

    async def test_import_time_subscription_refuses_closed_owner_loop(self):
        tree = ast.parse((Path(__file__).parents[1] / "codex_web/application.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_subscribe_goal_continuation_events")
        store, service, closed_loop = Mock(), Mock(), Mock()
        closed_loop.is_closed.return_value = True
        service.is_terminal_event = GoalContinuationEventService.is_terminal_event
        namespace = {"agent_session_store": store, "asyncio": asyncio,
            "goal_continuation_event_service": service, "TenantScope": TenantScope,
            "turn_execution_service": SimpleNamespace(_event_loop=closed_loop)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "observer", "exec"), namespace)
        hub = EventHub()
        adapter = CodexAgentRuntimeAdapter(SimpleNamespace(host=SimpleNamespace(hub=hub)))
        unsubscribe = await asyncio.to_thread(namespace[function.name], adapter,
            provider_id="openai", runtime_id="codex")
        try:
            await hub.publish({"type": "codex.event", "message": {"method": "turn/completed",
                "params": {"threadId": "native-session-a", "turn": {"id": "turn-a"}}}})
            store.list.assert_not_called()
            service.handle_event.assert_not_called()
            closed_loop.call_soon_threadsafe.assert_not_called()
        finally:
            unsubscribe()

    async def test_goal_startup_seeds_existing_execution_loop_owner(self):
        tree = ast.parse((Path(__file__).parents[1] / "codex_web/application.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "_recover_goal_continuations_on_startup")
        owner = SimpleNamespace(_event_loop=None)
        store = Mock()
        store.load.return_value = SimpleNamespace(bindings=[])
        namespace = {"asyncio": asyncio, "turn_execution_service": owner,
            "goal_execution_binding_store": store}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "startup", "exec"), namespace)
        await namespace[function.name]()
        self.assertIs(owner._event_loop, asyncio.get_running_loop())

    async def test_wrong_turn_and_non_terminal_events_are_ignored(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        wrong_turn = await service.handle_event(
            _event("turn.completed", turn_id="turn-other"),
            scope=self.scope,
        )
        non_terminal = await service.handle_event(
            _event("item.started"),
            scope=self.scope,
        )

        self.assertEqual(wrong_turn.outcome, "ignored")
        self.assertEqual(non_terminal.outcome, "ignored")
        self.assertEqual(continuation.resolved, [])


if __name__ == "__main__":
    unittest.main()
