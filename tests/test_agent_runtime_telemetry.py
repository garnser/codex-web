from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeHealth,
    AgentRuntimeSessionRequest,
    AgentSessionStatus,
)
from codex_web.agent_runtime_usage import (
    RuntimeTelemetryCompleteness,
    RuntimeTerminalOutcome,
)
from codex_web.artifact_evidence import EvidenceType
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
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
    provider_id = "anthropic"
    runtime_id = "claude-code"
    runtime_type = "claude-agent-sdk"
    capabilities = (
        AgentProviderCapability.AGENT_EXECUTION,
        AgentProviderCapability.USAGE_PARTIAL,
    )

    async def health(self):
        return AgentRuntimeHealth.HEALTHY


class AgentRuntimeTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.sessions = AgentSessionStore(sqlite)
        self.usage = AgentRuntimeUsageStore(sqlite)
        self.evidence_store = ArtifactEvidenceStore(sqlite)
        self.evidence = ArtifactEvidenceService(self.evidence_store)
        self.registry = AgentRuntimeRegistry()
        self.runtime = _Runtime()
        self.registry.register(self.runtime, capability_revision=4)
        self.session_service = AgentSessionService(self.sessions, self.registry)
        self.actor = _actor()
        self.session = self.session_service.adopt(
            provider_id="anthropic",
            runtime_id="claude-code",
            runtime_type="claude-agent-sdk",
            provider_native_session_id="native-session-1",
            request=AgentRuntimeSessionRequest(
                project_id="project-a",
                execution_id="execution-a",
                assignment_id="assignment-a",
                execution_workspace_id="execws-a",
                model="claude-test",
            ),
            actor=self.actor,
            capability_snapshot=self.runtime.capabilities,
            capability_revision=4,
        )
        self.clock = [1000.0]
        self.service = AgentRuntimeTelemetryService(
            self.usage,
            self.sessions,
            self.registry,
            artifact_evidence=self.evidence,
            attribution_resolver=lambda _session: {
                "goal_id": "goal-a",
                "work_item_ref": "group/app#42",
                "decision_id": "decision-a",
            },
            clock=lambda: self.clock[0],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_claude_terminal_usage_is_partial_attributed_and_emits_compact_evidence(self) -> None:
        record = self.service.observe_event(
            "anthropic",
            "claude-code",
            AgentRuntimeEvent(
                event_type="result/success",
                provider_native_session_id="native-session-1",
                provider_native_turn_id="turn-1",
                payload={
                    "type": "result",
                    "subtype": "success",
                    "session_id": "native-session-1",
                    "turn_id": "turn-1",
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "cache_read_input_tokens": 40,
                    },
                    "total_cost_usd": 0.012,
                    "duration_ms": 2500,
                    "modelUsage": {"claude-test": {"inputTokens": 120}},
                    "claude_code_version": "2.1.276",
                    "request_id": "req-1",
                    "result": "provider transcript must not be stored",
                },
            ),
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.agent_session_id, self.session.id)
        self.assertEqual(record.project_id, "project-a")
        self.assertEqual(record.goal_id, "goal-a")
        self.assertEqual(record.work_item_ref, "group/app#42")
        self.assertEqual(record.decision_id, "decision-a")
        self.assertEqual(record.execution_id, "execution-a")
        self.assertEqual(record.capability_revision, 4)
        self.assertEqual(record.input_tokens, 120)
        self.assertEqual(record.output_tokens, 30)
        self.assertEqual(record.cached_input_tokens, 40)
        self.assertEqual(record.total_tokens, 150)
        self.assertEqual(record.cost_usd, 0.012)
        self.assertEqual(record.runtime_duration_seconds, 2.5)
        self.assertEqual(record.telemetry_completeness, RuntimeTelemetryCompleteness.PARTIAL)
        self.assertEqual(record.terminal_outcome, RuntimeTerminalOutcome.SUCCEEDED)
        persisted_session = self.sessions.get(self.session.id)
        self.assertEqual(persisted_session.status, AgentSessionStatus.READY)
        self.assertEqual(record.observed_model_ids, ("claude-test",))
        self.assertEqual(record.runtime_version, "2.1.276")
        self.assertEqual(record.provider_request_ids, ("req-1",))
        self.assertEqual(len(record.evidence_ids), 1)

        evidence = self.evidence_store.load().evidence
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].evidence_type, EvidenceType.RUNTIME_RESULT)
        self.assertNotIn("provider transcript", repr(record.model_dump()))
        self.assertNotIn("provider transcript", repr(evidence[0].model_dump()))

    def test_observation_notifier_runs_after_usage_persistence(self) -> None:
        observed = []
        self.service.observation_notifier = lambda record: observed.append(
            (
                record.id,
                next(item.id for item in self.usage.list() if item.id == record.id),
            )
        )

        record = self.service.observe_event(
            "anthropic",
            "claude-code",
            AgentRuntimeEvent(
                event_type="result/success",
                provider_native_session_id="native-session-1",
                provider_native_turn_id="turn-notify",
                payload={
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                    },
                    "total_cost_usd": 0.01,
                },
            ),
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(observed, [(record.id, record.id)])

    def test_duplicate_tool_event_is_idempotent_and_does_not_double_count(self) -> None:
        event = AgentRuntimeEvent(
            event_type="tool/requested",
            provider_native_session_id="native-session-1",
            payload={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "git status"},
                        }
                    ]
                },
            },
        )
        first = self.service.observe_event("anthropic", "claude-code", event)
        second = self.service.observe_event("anthropic", "claude-code", event)

        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.tool_call_count, 1)
        self.assertEqual(second.shell_command_count, 1)
        self.assertEqual(second.git_operation_count, 1)
        self.assertEqual(len(second.event_fingerprints), 1)

    def test_codex_token_event_normalizes_last_turn_without_inventing_cost(self) -> None:
        codex = SimpleNamespace(
            provider_id="openai",
            runtime_id="codex",
            runtime_type="codex-app-server",
            capabilities=(
                AgentProviderCapability.AGENT_EXECUTION,
                AgentProviderCapability.USAGE_PARTIAL,
            ),
        )
        self.registry.register(codex, capability_revision=9)
        codex_session = self.session_service.adopt(
            provider_id="openai",
            runtime_id="codex",
            runtime_type="codex-app-server",
            provider_native_session_id="codex-thread-1",
            request=AgentRuntimeSessionRequest(
                project_id="project-a",
                execution_id="execution-b",
            ),
            actor=self.actor,
            capability_snapshot=codex.capabilities,
            capability_revision=9,
        )

        record = self.service.observe_event(
            "openai",
            "codex",
            AgentRuntimeEvent(
                event_type="thread/tokenUsage/updated",
                provider_native_session_id="codex-thread-1",
                provider_native_turn_id="codex-turn-1",
                payload={
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "codex-thread-1",
                        "turnId": "codex-turn-1",
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 50,
                                "cachedInputTokens": 10,
                                "outputTokens": 20,
                                "reasoningOutputTokens": 5,
                                "totalTokens": 75,
                            },
                            "total": {
                                "inputTokens": 500,
                                "cachedInputTokens": 100,
                                "outputTokens": 200,
                                "reasoningOutputTokens": 50,
                                "totalTokens": 750,
                            },
                        },
                    },
                },
            ),
        )

        assert record is not None
        self.assertEqual(record.agent_session_id, codex_session.id)
        self.assertEqual(record.input_tokens, 50)
        self.assertEqual(record.output_tokens, 20)
        self.assertEqual(record.reasoning_output_tokens, 5)
        self.assertEqual(record.total_tokens, 75)
        self.assertIsNone(record.cost_usd)
        self.assertEqual(record.telemetry_completeness, RuntimeTelemetryCompleteness.PARTIAL)

    def test_exact_capability_marks_exact_but_unknown_metrics_remain_null(self) -> None:
        exact = SimpleNamespace(
            provider_id="provider-exact",
            runtime_id="runtime-exact",
            runtime_type="test-runtime",
            capabilities=(
                AgentProviderCapability.AGENT_EXECUTION,
                AgentProviderCapability.USAGE_EXACT,
            ),
        )
        self.registry.register(exact)
        self.session_service.adopt(
            provider_id="provider-exact",
            runtime_id="runtime-exact",
            runtime_type="test-runtime",
            provider_native_session_id="exact-native",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=self.actor,
            capability_snapshot=exact.capabilities,
        )

        record = self.service.observe_event(
            "provider-exact",
            "runtime-exact",
            AgentRuntimeEvent(
                event_type="runtime/status",
                provider_native_session_id="exact-native",
                payload={"status": "ready"},
            ),
        )

        assert record is not None
        self.assertEqual(record.telemetry_completeness, RuntimeTelemetryCompleteness.EXACT)
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.cost_usd)

    def test_ambiguous_provider_native_session_id_is_never_used_as_canonical_identity(self) -> None:
        self.session_service.adopt(
            provider_id="anthropic",
            runtime_id="claude-code",
            runtime_type="claude-agent-sdk",
            provider_native_session_id="native-session-1",
            request=AgentRuntimeSessionRequest(project_id="project-b"),
            actor=_actor("org-b", "workspace-b"),
            capability_snapshot=self.runtime.capabilities,
        )

        result = self.service.observe_event(
            "anthropic",
            "claude-code",
            AgentRuntimeEvent(
                event_type="result/success",
                provider_native_session_id="native-session-1",
                payload={"usage": {"input_tokens": 1, "output_tokens": 1}},
            ),
        )

        self.assertIsNone(result)
        self.assertEqual(self.usage.list(), [])


if __name__ == "__main__":
    unittest.main()
