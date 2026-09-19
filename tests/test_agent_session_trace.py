from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.services.agent_session_trace import AgentSessionTraceService


class _Dump:
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self, mode="json"):
        def encode(value):
            if isinstance(value, _Dump):
                return value.model_dump(mode=mode)
            if hasattr(value, "value"):
                return value.value
            if isinstance(value, tuple):
                return [encode(item) for item in value]
            if isinstance(value, list):
                return [encode(item) for item in value]
            return value
        return {key: encode(value) for key, value in self.__dict__.items()}


def _enum(value):
    return SimpleNamespace(value=value)


class AgentSessionTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = SimpleNamespace(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.session = _Dump(
            id="agent-session-1",
            organization_id="org-a",
            workspace_id="ws-a",
            project_id="project-a",
            assignment_id="assignment-a",
            worker_id="worker-a",
            execution_id="exec-a",
            execution_workspace_id="execws-a",
            provider_id="anthropic",
            runtime_id="claude-code",
        )
        assignment = _Dump(
            id="assignment-a",
            organization_id="org-a",
            workspace_id="ws-a",
            work_item_ref="group/app#42",
            execution_id="exec-a",
            execution_workspace_id="execws-a",
            assigned_worker_id="worker-a",
            secret_refs=("secret-1", "secret-2"),
            lease=_Dump(
                worker_id="worker-a",
                fence=3,
                lease_token="super-secret-lease-token",
                acquired_at=1.0,
                expires_at=2.0,
            ),
            status=_enum("running"),
            fence=3,
        )
        worker = _Dump(
            id="worker-a",
            organization_id="org-a",
            workspace_id="ws-a",
            lifecycle=_enum("active"),
            version="worker-v1",
        )
        worker_event = _Dump(
            organization_id="org-a",
            workspace_id="ws-a",
            assignment_id="assignment-a",
            worker_id="worker-a",
            event_type="assignment_started",
        )
        workspace = _Dump(
            id="execws-a",
            organization_id="org-a",
            workspace_id="ws-a",
            status=_enum("active"),
            branch_name="codex/work",
            path="/private/workspace",
        )
        workspace_lease = _Dump(
            execution_workspace_id="execws-a",
            organization_id="org-a",
            workspace_id="ws-a",
            expires_at=2.0,
        )
        workspace_event = _Dump(
            workspace_id="execws-a",
            event_type="workspace_ready",
        )
        intent = _Dump(
            id="intent-a",
            organization_id="org-a",
            workspace_id="ws-a",
            action_id="gitlab.update",
            status=_enum("completed"),
            work_item_ref="group/app#42",
            goal_id=None,
            decision_id=None,
            execution_id="exec-a",
            requested_by="agent",
            created_at=1.0,
            updated_at=2.0,
        )
        receipt = _Dump(
            id="receipt-a",
            intent_id="intent-a",
            outcome="completed",
            provider_external_id="ext-a",
            received_at=2.0,
        )
        intent_verification = _Dump(
            id="intent-verification-a",
            intent_id="intent-a",
            verified=True,
            evidence_satisfied=True,
            verified_at=3.0,
        )
        evidence = _Dump(
            id="evidence-a",
            organization_id="org-a",
            workspace_id="ws-a",
            evidence_type=_enum("runtime_result"),
            result=_enum("pass"),
            summary="runtime passed",
            execution_id="exec-a",
            execution_workspace_id="execws-a",
            work_item_ref="group/app#42",
            provider="anthropic",
            source="agent-runtime:claude-code",
            observed_at=3.0,
            lifecycle=_enum("valid"),
        )
        verification = _Dump(
            id="verification-a",
            organization_id="org-a",
            workspace_id="ws-a",
            method="tests",
            result=_enum("pass"),
            independent=True,
            findings=(),
            work_item_ref="group/app#42",
            execution_id="exec-a",
            verified_at=4.0,
            evidence_ids=("evidence-a",),
            artifact_ids=(),
        )
        usage = _Dump(
            id="usage-a",
            provider_id="anthropic",
            runtime_id="claude-code",
            provider_native_turn_id="turn-a",
            observed_model_ids=("claude-test",),
            runtime_version="2.1.276",
            telemetry_completeness=_enum("partial"),
            terminal_outcome=_enum("succeeded"),
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.01,
            runtime_duration_seconds=1.2,
            tool_call_count=2,
            shell_command_count=1,
            file_edit_count=1,
            git_operation_count=1,
            context_compaction_count=0,
            started_at=1.0,
            completed_at=2.2,
            observed_at=2.2,
            event_fingerprints=("hash-a", "hash-b"),
        )

        self.sessions = SimpleNamespace(get=lambda _sid, _actor: self.session)
        self.telemetry = SimpleNamespace(
            list=lambda _actor, agent_session_id=None: [usage]
        )
        self.workers = SimpleNamespace(
            load=lambda: SimpleNamespace(
                assignments=[assignment],
                workers=[worker],
                events=[worker_event],
            )
        )
        self.workspaces = SimpleNamespace(
            load=lambda: SimpleNamespace(
                workspaces=[workspace],
                leases=[workspace_lease],
                events=[workspace_event],
            )
        )
        self.action_intents = SimpleNamespace(
            load=lambda: SimpleNamespace(
                intents=[intent],
                receipts=[receipt],
                verifications=[intent_verification],
            )
        )
        self.artifact_evidence = SimpleNamespace(
            load=lambda: SimpleNamespace(
                evidence=[evidence],
                verifications=[verification],
            )
        )
        self.service = AgentSessionTraceService(
            self.sessions,
            self.telemetry,
            self.workers,
            self.workspaces,
            self.action_intents,
            self.artifact_evidence,
        )

    def test_trace_links_canonical_execution_records_without_secrets_or_transcripts(self):
        trace = self.service.trace("agent-session-1", actor=self.actor)

        self.assertEqual(trace["assignment"]["id"], "assignment-a")
        self.assertEqual(trace["assignment"]["secret_refs"], [])
        self.assertEqual(trace["assignment"]["secret_ref_count"], 2)
        self.assertEqual(
            trace["assignment"]["lease"]["lease_token"],
            "[redacted]",
        )
        self.assertEqual(trace["worker"]["id"], "worker-a")
        self.assertEqual(trace["execution_workspace"]["id"], "execws-a")
        self.assertEqual(trace["runtime_events"][0]["event_count"], 2)
        self.assertEqual(trace["action_intents"][0]["id"], "intent-a")
        self.assertEqual(
            trace["action_intents"][0]["receipts"][0]["id"],
            "receipt-a",
        )
        self.assertEqual(trace["evidence"][0]["id"], "evidence-a")
        self.assertEqual(trace["verifications"][0]["id"], "verification-a")
        serialized = repr(trace)
        self.assertNotIn("super-secret-lease-token", serialized)
        self.assertNotIn("secret-1", serialized)
        self.assertNotIn("provider transcript", serialized)

    def test_cross_tenant_records_are_not_joined(self):
        foreign = _Dump(
            id="assignment-a",
            organization_id="org-b",
            workspace_id="ws-b",
            work_item_ref="foreign#1",
            execution_id="exec-a",
            execution_workspace_id="execws-foreign",
            assigned_worker_id="worker-foreign",
            secret_refs=("foreign-secret",),
            lease=None,
            status=_enum("running"),
            fence=1,
        )
        self.workers.load = lambda: SimpleNamespace(
            assignments=[foreign],
            workers=[],
            events=[],
        )

        trace = self.service.trace("agent-session-1", actor=self.actor)

        self.assertIsNone(trace["assignment"])
        self.assertIsNone(trace["worker"])
        self.assertNotIn("foreign-secret", repr(trace))


if __name__ == "__main__":
    unittest.main()
