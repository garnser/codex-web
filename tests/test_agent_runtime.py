from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeObjectiveRequest,
    AgentRuntimeResult,
    AgentRuntimeUnsupportedCapability,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
    AgentSessionStatus,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_runtime import (
    AgentRuntimeRegistry,
    AgentSessionService,
)
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor(
    organization_id: str = "org-a",
    workspace_id: str = "workspace-a",
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id=organization_id,
        workspace_id=workspace_id,
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class _Runtime:
    provider_id = "provider-a"
    runtime_id = "runtime-a"
    runtime_type = "test-runtime"
    capabilities = (
        AgentProviderCapability.AGENT_EXECUTION,
        AgentProviderCapability.PERSISTENT_SESSIONS,
        AgentProviderCapability.INTERRUPT_CANCEL,
        AgentProviderCapability.NATIVE_CONTEXT_COMPACTION,
        AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
    )

    def __init__(self) -> None:
        self.native_id = "native-1"
        self.calls: list[tuple[str, object]] = []
        self.fail_turn = False

    async def health(self):
        return AgentRuntimeHealth.HEALTHY

    async def create_session(self, request):
        self.calls.append(("create", request))
        return AgentRuntimeResult(provider_native_session_id=self.native_id)

    async def resume_session(self, provider_native_session_id, request):
        self.calls.append(("resume", provider_native_session_id))
        self.native_id = "native-2"
        return AgentRuntimeResult(provider_native_session_id=self.native_id)

    async def read_session(self, provider_native_session_id):
        return AgentRuntimeResult(provider_native_session_id=provider_native_session_id)

    async def close_session(self, provider_native_session_id):
        self.calls.append(("close", provider_native_session_id))
        return AgentRuntimeResult(provider_native_session_id=provider_native_session_id)

    async def start_turn(self, provider_native_session_id, request):
        self.calls.append(("turn", provider_native_session_id))
        if self.fail_turn:
            raise RuntimeError("provider crashed")
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            provider_native_turn_id="turn-1",
        )

    async def interrupt(self, provider_native_session_id):
        self.calls.append(("interrupt", provider_native_session_id))
        return AgentRuntimeResult(provider_native_session_id=provider_native_session_id)

    async def compact_session(self, provider_native_session_id):
        self.calls.append(("compact", provider_native_session_id))
        return AgentRuntimeResult(provider_native_session_id=provider_native_session_id)

    async def read_objective(self, provider_native_session_id):
        self.calls.append(("objective-read", provider_native_session_id))
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload={"goal": {"objective": "bounded validation", "status": "active"}},
        )

    async def set_objective(self, provider_native_session_id, request):
        self.calls.append(("objective-set", request))
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload={"goal": {"objective": request.objective, "status": request.status}},
        )

    async def clear_objective(self, provider_native_session_id):
        self.calls.append(("objective-clear", provider_native_session_id))
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload={"goal": None},
        )


class AgentSessionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.store = AgentSessionStore(sqlite)
        self.registry = AgentRuntimeRegistry()
        self.runtime = _Runtime()
        self.registry.register(self.runtime)
        self.service = AgentSessionService(self.store, self.registry)
        self.actor = _actor()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_canonical_identity_survives_provider_native_resume_id_change(self) -> None:
        request = AgentRuntimeSessionRequest(
            project_id="project-a",
            execution_id="execution-a",
            assignment_id="assignment-a",
            execution_workspace_id="workspace-execution-a",
            worker_id="worker-a",
        )
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=request,
            actor=self.actor,
            capability_revision=7,
        )
        canonical_id = created.id
        self.assertEqual(created.provider_native_session_id, "native-1")
        self.assertEqual(created.capability_revision, 7)

        resumed = await self.service.resume(
            canonical_id,
            request=request,
            actor=self.actor,
        )

        self.assertEqual(resumed.id, canonical_id)
        self.assertEqual(resumed.provider_native_session_id, "native-2")
        self.assertEqual(resumed.recovery_attempts, 1)
        self.assertEqual(resumed.status, AgentSessionStatus.READY)

    async def test_legacy_adoption_is_idempotent_and_does_not_duplicate(self) -> None:
        request = AgentRuntimeSessionRequest(project_id="project-a")
        first = self.service.adopt(
            provider_id="provider-a",
            runtime_id="runtime-a",
            runtime_type="test-runtime",
            provider_native_session_id="legacy-native-1",
            request=request,
            actor=self.actor,
            capability_snapshot=self.runtime.capabilities,
        )
        second = self.service.adopt(
            provider_id="provider-a",
            runtime_id="runtime-a",
            runtime_type="test-runtime",
            provider_native_session_id="legacy-native-1",
            request=request,
            actor=self.actor,
            capability_snapshot=self.runtime.capabilities,
        )

        self.assertEqual(second.id, first.id)
        self.assertEqual(len(self.service.list(self.actor)), 1)

    async def test_provider_crash_marks_canonical_session_failed(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )
        self.runtime.fail_turn = True

        with self.assertRaisesRegex(RuntimeError, "provider crashed"):
            await self.service.start_turn(
                created.id,
                AgentRuntimeTurnRequest(message="fail"),
                actor=self.actor,
            )

        failed = self.service.get(created.id, self.actor)
        self.assertEqual(failed.status, AgentSessionStatus.FAILED)
        self.assertEqual(
            failed.failure_reason,
            "The execution process failed.",
        )
        self.assertIsNotNone(failed.failure)
        self.assertEqual(
            failed.failure.reason_code.value,
            "process_failure",
        )
        self.assertEqual(
            failed.failure.source_native_code,
            "RuntimeError",
        )
        self.assertNotIn(
            "provider crashed",
            failed.failure.model_dump_json(),
        )

    async def test_unsupported_capability_fails_deterministically(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )
        self.runtime.capabilities = (AgentProviderCapability.AGENT_EXECUTION,)

        with self.assertRaises(AgentRuntimeUnsupportedCapability) as caught:
            await self.service.interrupt(created.id, actor=self.actor)

        self.assertEqual(
            caught.exception.capability,
            AgentProviderCapability.INTERRUPT_CANCEL,
        )

    async def test_session_scope_hides_other_tenant(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )

        with self.assertRaises(KeyError):
            self.service.get(created.id, _actor("org-b", "workspace-b"))

    async def test_compaction_is_server_side_capability_checked(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )

        result = await self.service.compact(created.id, actor=self.actor)

        self.assertEqual(result.provider_native_session_id, "native-1")
        self.assertIn(("compact", "native-1"), self.runtime.calls)

        self.runtime.capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        with self.assertRaises(AgentRuntimeUnsupportedCapability):
            await self.service.compact(created.id, actor=self.actor)

    async def test_native_objective_operations_are_capability_gated(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )
        request = AgentRuntimeObjectiveRequest(
            objective="Continue only the canonical release validation scope",
            status="active",
            token_budget=8000,
        )

        set_result = await self.service.set_objective(
            created.id,
            request,
            actor=self.actor,
        )
        read_result = await self.service.read_objective(created.id, actor=self.actor)
        clear_result = await self.service.clear_objective(created.id, actor=self.actor)

        self.assertEqual(
            set_result.payload["goal"]["objective"],
            "Continue only the canonical release validation scope",
        )
        self.assertEqual(read_result.payload["goal"]["status"], "active")
        self.assertIsNone(clear_result.payload["goal"])

        self.runtime.capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        with self.assertRaises(AgentRuntimeUnsupportedCapability) as caught:
            await self.service.read_objective(created.id, actor=self.actor)
        self.assertEqual(
            caught.exception.capability,
            AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
        )

    async def test_turn_interrupt_and_close_use_provider_native_id(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )
        result = await self.service.start_turn(
            created.id,
            AgentRuntimeTurnRequest(message="Implement the change"),
            actor=self.actor,
        )
        self.assertEqual(result.provider_native_turn_id, "turn-1")

        await self.service.interrupt(created.id, actor=self.actor)
        closed = await self.service.close(created.id, actor=self.actor)

        self.assertEqual(closed.status, AgentSessionStatus.CLOSED)
        self.assertIn(("turn", "native-1"), self.runtime.calls)
        self.assertIn(("interrupt", "native-1"), self.runtime.calls)
        self.assertIn(("close", "native-1"), self.runtime.calls)


class CodexAgentRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_rpc_is_hidden_behind_canonical_runtime_methods(self) -> None:
        transport = type("Transport", (), {})()
        transport.request = AsyncMock(
            side_effect=[
                {"thread": {"id": "codex-thread-1"}},
                {"turn": {"id": "codex-turn-1"}},
                {"ok": True},
            ]
        )
        adapter = CodexAgentRuntimeAdapter(transport)
        session = await adapter.create_session(
            AgentRuntimeSessionRequest(
                project_id="project-a",
                workspace_cwd="/workspace/project-a",
                sandbox="workspace-write",
                approval_policy="on-request",
                model="gpt-test",
            )
        )
        turn = await adapter.start_turn(
            "codex-thread-1",
            AgentRuntimeTurnRequest(message="hello", reasoning_effort="medium"),
        )
        await adapter.interrupt("codex-thread-1")

        self.assertEqual(session.provider_native_session_id, "codex-thread-1")
        self.assertEqual(turn.provider_native_turn_id, "codex-turn-1")
        self.assertEqual(
            transport.request.await_args_list[0].args,
            (
                "thread/start",
                {
                    "sessionStartSource": "startup",
                    "cwd": "/workspace/project-a",
                    "sandbox": "workspace-write",
                    "approvalPolicy": "on-request",
                    "model": "gpt-test",
                },
            ),
        )
        self.assertEqual(
            transport.request.await_args_list[1].args[0],
            "turn/start",
        )
        self.assertEqual(
            transport.request.await_args_list[2].args,
            ("turn/interrupt", {"threadId": "codex-thread-1"}),
        )

    async def test_codex_native_objectives_use_supported_thread_goal_protocol(self) -> None:
        transport = SimpleNamespace(
            request=AsyncMock(
                side_effect=[
                    {"goal": {"threadId": "thread-1", "objective": "bounded", "status": "active"}},
                    {"goal": {"threadId": "thread-1", "objective": "bounded", "status": "active"}},
                    {"goal": None},
                ]
            )
        )
        adapter = CodexAgentRuntimeAdapter(transport)

        read = await adapter.read_objective("thread-1")
        set_result = await adapter.set_objective(
            "thread-1",
            AgentRuntimeObjectiveRequest(
                objective="bounded",
                status="active",
                token_budget=5000,
            ),
        )
        cleared = await adapter.clear_objective("thread-1")

        self.assertEqual(read.payload["goal"]["objective"], "bounded")
        self.assertEqual(set_result.payload["goal"]["status"], "active")
        self.assertIsNone(cleared.payload["goal"])
        self.assertEqual(
            [call.args[0] for call in transport.request.await_args_list],
            ["thread/goal/get", "thread/goal/set", "thread/goal/clear"],
        )
        self.assertEqual(
            transport.request.await_args_list[1].args[1]["tokenBudget"],
            5000,
        )

    async def test_compaction_approval_recovery_and_events_use_adapter_surface(self) -> None:
        class Hub:
            def __init__(self):
                self.listeners = set()

            def subscribe(self, listener):
                self.listeners.add(listener)

            def unsubscribe(self, listener):
                self.listeners.discard(listener)

            def emit(self, event):
                for listener in list(self.listeners):
                    listener(event)

        hub = Hub()
        transport = SimpleNamespace(
            host=SimpleNamespace(hub=hub),
            request=AsyncMock(return_value={"ok": True}),
            respond_to_server_request=AsyncMock(),
            ensure_started=AsyncMock(),
        )
        adapter = CodexAgentRuntimeAdapter(transport)
        events = []
        unsubscribe = adapter.subscribe_events(events.append)

        hub.emit(
            {
                "type": "codex.event",
                "message": {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                    },
                },
            }
        )
        compacted = await adapter.compact_session("thread-1")
        await adapter.respond_approval("approval-1", {"decision": "accept"})
        health = await adapter.recover()
        unsubscribe()

        self.assertEqual(events[0].event_type, "turn/completed")
        self.assertEqual(events[0].provider_native_session_id, "thread-1")
        self.assertEqual(events[0].provider_native_turn_id, "turn-1")
        self.assertEqual(compacted.provider_native_session_id, "thread-1")
        transport.respond_to_server_request.assert_awaited_once_with(
            "approval-1",
            {"decision": "accept"},
        )
        transport.ensure_started.assert_awaited_once()
        self.assertEqual(health, AgentRuntimeHealth.HEALTHY)
        self.assertEqual(hub.listeners, set())

    async def test_codex_capacity_snapshot_uses_structured_rate_limit_rpc(self) -> None:
        transport = SimpleNamespace(
            request=AsyncMock(
                return_value={
                    "ordinaryUsageAllowed": False,
                    "rateLimits": {
                        "rateLimitReachedType": "primary",
                        "primary": {
                            "usedPercent": 100,
                            "resetsAt": 1_900_000_120,
                        },
                    },
                }
            )
        )
        adapter = CodexAgentRuntimeAdapter(transport)

        snapshot = await adapter.capacity_snapshot()

        self.assertFalse(snapshot["ordinaryUsageAllowed"])
        self.assertEqual(
            snapshot["rateLimits"]["primary"]["usedPercent"],
            100,
        )
        transport.request.assert_awaited_once_with(
            "account/rateLimits/read",
            {},
        )

    async def test_codex_capabilities_are_explicit(self) -> None:
        adapter = CodexAgentRuntimeAdapter(type("Transport", (), {})())
        self.assertIn(
            AgentProviderCapability.AGENT_EXECUTION,
            adapter.capabilities,
        )
        self.assertIn(
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION,
            adapter.capabilities,
        )
        self.assertIn(
            AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
            adapter.capabilities,
        )
        self.assertNotIn(
            AgentProviderCapability.USAGE_EXACT,
            adapter.capabilities,
        )


if __name__ == "__main__":
    unittest.main()
