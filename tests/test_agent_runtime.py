from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeResult,
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
    )

    def __init__(self) -> None:
        self.native_id = "native-1"
        self.calls: list[tuple[str, object]] = []

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
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            provider_native_turn_id="turn-1",
        )

    async def interrupt(self, provider_native_session_id):
        self.calls.append(("interrupt", provider_native_session_id))
        return AgentRuntimeResult(provider_native_session_id=provider_native_session_id)


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

    async def test_session_scope_hides_other_tenant(self) -> None:
        created = await self.service.create(
            provider_id="provider-a",
            runtime_id="runtime-a",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
        )

        with self.assertRaises(KeyError):
            self.service.get(created.id, _actor("org-b", "workspace-b"))

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
        self.assertNotIn(
            AgentProviderCapability.USAGE_EXACT,
            adapter.capabilities,
        )


if __name__ == "__main__":
    unittest.main()
