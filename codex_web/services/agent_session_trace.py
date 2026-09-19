from __future__ import annotations

from typing import Any

from codex_web.identity import AuthenticationActor
from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore


class AgentSessionTraceService:
    """Build a compact canonical execution trace without provider transcripts."""

    def __init__(
        self,
        sessions: AgentSessionService,
        telemetry: AgentRuntimeTelemetryService,
        workers: ExecutionWorkerStore,
        workspaces: ExecutionWorkspaceStateStore,
        action_intents: ActionIntentStore,
        artifact_evidence: ArtifactEvidenceStore,
    ) -> None:
        self.sessions = sessions
        self.telemetry = telemetry
        self.workers = workers
        self.workspaces = workspaces
        self.action_intents = action_intents
        self.artifact_evidence = artifact_evidence

    @staticmethod
    def _same_scope(item: Any, actor: AuthenticationActor) -> bool:
        return (
            getattr(item, "organization_id", None) == actor.organization_id
            and getattr(item, "workspace_id", None) == actor.workspace_id
        )

    @staticmethod
    def _assignment_projection(item: Any) -> dict[str, Any]:
        payload = item.model_dump(mode="json")
        secret_count = len(payload.get("secret_refs") or [])
        payload["secret_refs"] = []
        payload["secret_ref_count"] = secret_count
        lease = payload.get("lease")
        if isinstance(lease, dict) and lease.get("lease_token"):
            lease["lease_token"] = "[redacted]"
        return payload

    @staticmethod
    def _intent_projection(item: Any, state: Any) -> dict[str, Any]:
        receipts = [
            receipt
            for receipt in state.receipts
            if receipt.intent_id == item.id
        ]
        verifications = [
            verification
            for verification in state.verifications
            if verification.intent_id == item.id
        ]
        return {
            "id": item.id,
            "action_id": item.action_id,
            "status": item.status.value,
            "work_item_ref": item.work_item_ref,
            "goal_id": item.goal_id,
            "decision_id": item.decision_id,
            "execution_id": item.execution_id,
            "requested_by": item.requested_by,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "receipts": [
                {
                    "id": receipt.id,
                    "outcome": getattr(receipt.outcome, "value", receipt.outcome),
                    "provider_external_id": receipt.provider_external_id,
                    "received_at": receipt.received_at,
                }
                for receipt in receipts
            ],
            "verifications": [
                {
                    "id": verification.id,
                    "outcome": getattr(
                        verification.outcome,
                        "value",
                        verification.outcome,
                    ),
                    "verified_at": verification.verified_at,
                }
                for verification in verifications
            ],
        }

    @staticmethod
    def _evidence_projection(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "evidence_type": item.evidence_type.value,
            "result": item.result.value,
            "summary": item.summary,
            "execution_id": item.execution_id,
            "execution_workspace_id": item.execution_workspace_id,
            "work_item_ref": item.work_item_ref,
            "provider": item.provider,
            "source": item.source,
            "observed_at": item.observed_at,
            "lifecycle": item.lifecycle.value,
        }

    @staticmethod
    def _verification_projection(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "verification_type": item.verification_type.value,
            "result": item.result.value,
            "summary": item.summary,
            "work_item_ref": item.work_item_ref,
            "verified_at": item.verified_at,
            "lifecycle": item.lifecycle.value,
            "evidence_ids": list(item.evidence_ids),
        }

    def trace(
        self,
        session_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        session = self.sessions.get(session_id, actor)
        worker_state = self.workers.load()

        assignment = next(
            (
                item
                for item in worker_state.assignments
                if session.assignment_id
                and item.id == session.assignment_id
                and self._same_scope(item, actor)
            ),
            None,
        )
        worker_id = (
            assignment.assigned_worker_id
            if assignment is not None
            else session.worker_id
        )
        worker = next(
            (
                item
                for item in worker_state.workers
                if worker_id
                and item.id == worker_id
                and self._same_scope(item, actor)
            ),
            None,
        )
        worker_events = [
            event
            for event in worker_state.events
            if self._same_scope(event, actor)
            and (
                (session.assignment_id and event.assignment_id == session.assignment_id)
                or (worker_id and event.worker_id == worker_id)
            )
        ][-100:]

        workspace_state = self.workspaces.load()
        execution_workspace_id = (
            assignment.execution_workspace_id
            if assignment is not None
            else session.execution_workspace_id
        )
        workspace = next(
            (
                item
                for item in workspace_state.workspaces
                if execution_workspace_id
                and item.id == execution_workspace_id
                and self._same_scope(item, actor)
            ),
            None,
        )
        workspace_lease = next(
            (
                item
                for item in workspace_state.leases
                if execution_workspace_id
                and item.execution_workspace_id == execution_workspace_id
                and self._same_scope(item, actor)
            ),
            None,
        )
        workspace_events = [
            event
            for event in workspace_state.events
            if execution_workspace_id
            and event.workspace_id == execution_workspace_id
        ][-100:]

        work_item_ref = (
            assignment.work_item_ref
            if assignment is not None
            else None
        )
        execution_id = (
            assignment.execution_id
            if assignment is not None
            else session.execution_id
        )

        runtime_usage = self.telemetry.list(
            actor,
            agent_session_id=session.id,
        )

        action_state = self.action_intents.load()
        intents = [
            item
            for item in action_state.intents
            if self._same_scope(item, actor)
            and (
                (execution_id and item.execution_id == execution_id)
                or (work_item_ref and item.work_item_ref == work_item_ref)
            )
        ]

        evidence_state = self.artifact_evidence.load()
        evidence = [
            item
            for item in evidence_state.evidence
            if self._same_scope(item, actor)
            and (
                (execution_id and item.execution_id == execution_id)
                or (work_item_ref and item.work_item_ref == work_item_ref)
                or (
                    execution_workspace_id
                    and item.execution_workspace_id == execution_workspace_id
                )
            )
        ]
        evidence_ids = {item.id for item in evidence}
        verifications = [
            item
            for item in evidence_state.verifications
            if self._same_scope(item, actor)
            and (
                (work_item_ref and item.work_item_ref == work_item_ref)
                or bool(set(item.evidence_ids) & evidence_ids)
            )
        ]

        return {
            "agent_session": session.model_dump(mode="json"),
            "assignment": (
                self._assignment_projection(assignment)
                if assignment is not None
                else None
            ),
            "worker": (
                worker.model_dump(mode="json")
                if worker is not None
                else None
            ),
            "worker_events": [
                event.model_dump(mode="json")
                for event in worker_events
            ],
            "execution_workspace": (
                workspace.model_dump(mode="json")
                if workspace is not None
                else None
            ),
            "execution_workspace_lease": (
                workspace_lease.model_dump(mode="json")
                if workspace_lease is not None
                else None
            ),
            "execution_workspace_events": [
                event.model_dump(mode="json")
                for event in workspace_events
            ],
            "runtime_events": [
                {
                    "id": item.id,
                    "provider_id": item.provider_id,
                    "runtime_id": item.runtime_id,
                    "provider_native_turn_id": item.provider_native_turn_id,
                    "observed_model_ids": list(item.observed_model_ids),
                    "runtime_version": item.runtime_version,
                    "telemetry_completeness": item.telemetry_completeness.value,
                    "terminal_outcome": item.terminal_outcome.value,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "cost_usd": item.cost_usd,
                    "runtime_duration_seconds": item.runtime_duration_seconds,
                    "tool_call_count": item.tool_call_count,
                    "shell_command_count": item.shell_command_count,
                    "file_edit_count": item.file_edit_count,
                    "git_operation_count": item.git_operation_count,
                    "context_compaction_count": item.context_compaction_count,
                    "started_at": item.started_at,
                    "completed_at": item.completed_at,
                    "observed_at": item.observed_at,
                    "event_count": len(item.event_fingerprints),
                }
                for item in runtime_usage
            ],
            "action_intents": [
                self._intent_projection(item, action_state)
                for item in intents
            ],
            "evidence": [
                self._evidence_projection(item)
                for item in evidence
            ],
            "verifications": [
                self._verification_projection(item)
                for item in verifications
            ],
        }
