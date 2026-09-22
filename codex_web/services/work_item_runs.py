from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import HTTPException

from codex_web.execution_workers import AssignmentStatus, ExecutionAssignment
from codex_web.storage.execution_workers import ExecutionWorkerStore


class WorkItemRunProjectionService:
    """Bounded read model over canonical execution state.

    Runs are projected from execution assignments and their canonical related
    records. This service deliberately owns no independent lifecycle state.
    """

    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100
    MAX_ACTIVE = 50

    _STATUS = {
        AssignmentStatus.PENDING: "queued",
        AssignmentStatus.CLAIMED: "claimed",
        AssignmentStatus.RUNNING: "running",
        AssignmentStatus.SUCCEEDED: "completed",
        AssignmentStatus.FAILED: "failed",
        AssignmentStatus.CANCELLED: "cancelled",
        AssignmentStatus.LOST: "lost",
    }
    _ACTIVE = {
        AssignmentStatus.PENDING,
        AssignmentStatus.CLAIMED,
        AssignmentStatus.RUNNING,
    }

    def __init__(
        self,
        execution_workers: ExecutionWorkerStore,
        *,
        runtime_usage: Any | None = None,
        artifact_evidence: Any | None = None,
        action_intents: Any | None = None,
        approvals: Any | None = None,
        attention: Any | None = None,
        work_item_execution: Any | None = None,
    ) -> None:
        self.execution_workers = execution_workers
        self.runtime_usage = runtime_usage
        self.artifact_evidence = artifact_evidence
        self.action_intents = action_intents
        self.approvals = approvals
        self.attention = attention
        self.work_item_execution = work_item_execution

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)

    @staticmethod
    def _encode_cursor(item: ExecutionAssignment) -> str:
        raw = json.dumps(
            [float(item.created_at), item.id],
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: str | None) -> tuple[float, str] | None:
        if not value:
            return None
        try:
            padded = value + ("=" * (-len(value) % 4))
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            created_at, assignment_id = json.loads(raw.decode("utf-8"))
            return float(created_at), str(assignment_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_run_cursor"},
            ) from exc

    @classmethod
    def _sort_key(cls, item: ExecutionAssignment) -> tuple[float, str]:
        return float(item.created_at), item.id

    @staticmethod
    def _safe_load(store: Any | None, attr: str | None = None) -> Any:
        if store is None:
            return None
        try:
            value = store.load()
            return getattr(value, attr) if attr else value
        except Exception:
            return None

    def _scoped_assignments(
        self,
        ref: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[Any, list[ExecutionAssignment]]:
        state = self.execution_workers.load()
        items = [
            item
            for item in state.assignments
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and item.work_item_ref == ref
        ]
        items.sort(key=self._sort_key, reverse=True)
        return state, items

    @classmethod
    def _lease_view(cls, item: ExecutionAssignment) -> dict[str, Any] | None:
        lease = item.lease
        if lease is None:
            return None
        # Never project lease_token. It is a bearer credential.
        return {
            "workerId": lease.worker_id,
            "fence": lease.fence,
            "acquiredAt": lease.acquired_at,
            "expiresAt": lease.expires_at,
            "renewedAt": lease.renewed_at,
        }

    @classmethod
    def _agent_view(cls, item: ExecutionAssignment) -> dict[str, Any] | None:
        profile = item.agent_profile
        if profile is None:
            return None
        return {
            "profileId": profile.profile_id,
            "profileRevision": profile.profile_revision,
            "profileRecordId": profile.profile_record_id,
            "roleId": profile.role_id,
            "roleDefinition": (
                profile.role_definition_ref.model_dump(mode="json")
                if profile.role_definition_ref is not None
                else None
            ),
            "instructionsDefinition": (
                profile.instructions_ref.model_dump(mode="json")
                if profile.instructions_ref is not None
                else None
            ),
            "skillDefinitions": [
                ref.model_dump(mode="json")
                for ref in profile.skill_refs
            ],
            "authorityRoleId": profile.authority_role_id,
            "authorityDefinition": (
                profile.authority_definition_ref.model_dump(mode="json")
                if profile.authority_definition_ref is not None
                else None
            ),
            "selectedProviderId": profile.selected_provider_id,
            "selectedProviderRevision": profile.selected_provider_revision,
            "selectedRuntimeId": profile.selected_runtime_id,
            "selectedRuntimeCapabilityRevision": (
                profile.selected_runtime_capability_revision
            ),
            "selectedWorkerId": profile.selected_worker_id,
            "modelProviderId": profile.model_provider_id,
            "modelId": profile.model_id,
        }

    @classmethod
    def _runtime_view(cls, item: ExecutionAssignment) -> dict[str, Any] | None:
        binding = item.runtime_binding
        if binding is None:
            return None
        return {
            "providerId": binding.provider_id,
            "runtimeId": binding.runtime_id,
            "capabilityRevision": binding.capability_revision,
        }

    @classmethod
    def _repository_scope_view(
        cls,
        item: ExecutionAssignment,
    ) -> dict[str, Any] | None:
        scope = item.repository_scope
        if scope is not None:
            return {
                "organizationId": scope.organization_id,
                "workspaceId": scope.workspace_id,
                "projectId": scope.project_id,
                "writeMode": cls._value(scope.write_mode),
                "writableRepositoryIds": list(scope.writable_repository_ids),
                "readOnlyRepositoryIds": list(scope.read_only_repository_ids),
                "source": cls._value(scope.source),
                "sourceRef": scope.source_ref,
                "selectionEvidence": [
                    evidence.model_dump(mode="json")
                    for evidence in scope.selection_evidence
                ],
            }

        target = item.repository_target
        if target is None:
            return None
        return {
            "organizationId": target.organization_id,
            "workspaceId": target.workspace_id,
            "projectId": target.project_id,
            "writeMode": "single",
            "writableRepositoryIds": (
                [target.mutable_repository_id]
                if target.mutable_repository_id is not None
                else []
            ),
            "readOnlyRepositoryIds": list(target.read_only_repository_ids),
            "source": cls._value(target.source),
            "sourceRef": target.source_ref,
            "selectionEvidence": [
                evidence.model_dump(mode="json")
                for evidence in target.selection_evidence
            ],
        }

    @classmethod
    def _attempts(cls, worker_state: Any, item: ExecutionAssignment) -> list[dict[str, Any]]:
        relevant = sorted(
            (
                event
                for event in worker_state.events
                if event.assignment_id == item.id
            ),
            key=lambda event: (event.occurred_at, event.id),
        )
        attempts: dict[int, dict[str, Any]] = {}
        current_fence = 0
        for event in relevant:
            details = dict(event.details or {})
            try:
                fence = int(details.get("fence") or current_fence or 0)
            except (TypeError, ValueError):
                fence = current_fence
            if event.event_type == "assignment_claimed" and fence > 0:
                current_fence = fence
            if fence <= 0:
                continue
            attempt = attempts.setdefault(
                fence,
                {
                    "attempt": fence,
                    "workerId": event.worker_id,
                    "claimedAt": None,
                    "startedAt": None,
                    "completedAt": None,
                    "outcome": None,
                    "leaseExpiresAt": None,
                },
            )
            if event.worker_id:
                attempt["workerId"] = event.worker_id
            if event.event_type == "assignment_claimed":
                attempt["claimedAt"] = event.occurred_at
                attempt["leaseExpiresAt"] = details.get("expires_at")
            elif event.event_type == "assignment_started":
                attempt["startedAt"] = event.occurred_at
            elif event.event_type == "assignment_completed":
                attempt["completedAt"] = event.occurred_at
                attempt["outcome"] = details.get("status")
            elif event.event_type == "assignment_lost":
                attempt["completedAt"] = event.occurred_at
                attempt["outcome"] = "lost"
            elif event.event_type == "assignment_retried":
                attempt["retryRequestedAt"] = event.occurred_at

        if not attempts and item.fence > 0:
            attempts[item.fence] = {
                "attempt": item.fence,
                "workerId": item.assigned_worker_id,
                "claimedAt": None,
                "startedAt": item.started_at,
                "completedAt": item.completed_at,
                "outcome": cls._STATUS.get(item.status, cls._value(item.status)),
                "leaseExpiresAt": item.lease.expires_at if item.lease else None,
            }
        return [attempts[key] for key in sorted(attempts)]

    def _usage_records(self, item: ExecutionAssignment) -> list[Any]:
        if self.runtime_usage is None:
            return []
        try:
            values = self.runtime_usage.list()
        except Exception:
            return []
        return [
            value
            for value in values
            if value.organization_id == item.organization_id
            and value.workspace_id == item.workspace_id
            and (
                value.execution_id == item.execution_id
                or value.assignment_id == item.id
            )
        ]

    @classmethod
    def _usage_summary(cls, records: list[Any]) -> dict[str, Any]:
        if not records:
            return {
                "records": 0,
                "inputTokens": None,
                "outputTokens": None,
                "reasoningTokens": None,
                "totalTokens": None,
                "costUsd": None,
                "toolCalls": 0,
                "shellCommands": 0,
                "fileEdits": 0,
                "gitOperations": 0,
                "models": [],
                "providers": [],
                "runtimes": [],
            }

        def optional_sum(name: str) -> int | float | None:
            values = [getattr(record, name, None) for record in records]
            known = [value for value in values if value is not None]
            return sum(known) if known else None

        return {
            "records": len(records),
            "inputTokens": optional_sum("input_tokens"),
            "outputTokens": optional_sum("output_tokens"),
            "reasoningTokens": optional_sum("reasoning_output_tokens"),
            "totalTokens": optional_sum("total_tokens"),
            "costUsd": optional_sum("cost_usd"),
            "toolCalls": sum(record.tool_call_count for record in records),
            "shellCommands": sum(record.shell_command_count for record in records),
            "fileEdits": sum(record.file_edit_count for record in records),
            "gitOperations": sum(record.git_operation_count for record in records),
            "models": sorted(
                {
                    model
                    for record in records
                    for model in record.observed_model_ids
                    if model
                }
            ),
            "providers": sorted({record.provider_id for record in records}),
            "runtimes": sorted({record.runtime_id for record in records}),
        }

    def _summary(self, worker_state: Any, item: ExecutionAssignment) -> dict[str, Any]:
        usage = self._usage_summary(self._usage_records(item))
        attempts = self._attempts(worker_state, item)
        failure = item.failure.model_dump(mode="json") if item.failure is not None else None
        return {
            "id": item.execution_id,
            "executionId": item.execution_id,
            "assignmentId": item.id,
            "workItemRef": item.work_item_ref,
            "projectId": item.project_id,
            "subject": (
                item.subject.model_dump(mode="json")
                if item.subject is not None
                else None
            ),
            "trigger": {
                "source": "execution_assignment",
                "createdBy": item.created_by,
            },
            "status": self._STATUS.get(item.status, self._value(item.status)),
            "assignmentStatus": self._value(item.status),
            "createdAt": item.created_at,
            "updatedAt": item.updated_at,
            "startedAt": item.started_at,
            "completedAt": item.completed_at,
            "deadlineAt": item.deadline_at,
            "agent": self._agent_view(item),
            "runtime": self._runtime_view(item),
            "worker": {
                "workerId": item.assigned_worker_id,
                "fence": item.fence,
                "lease": self._lease_view(item),
            },
            "executionContractVersion": item.execution_contract_version,
            "executionProfileId": item.execution_profile_id,
            "executionProfileDefinition": (
                item.execution_profile_definition.model_dump(mode="json")
                if item.execution_profile_definition is not None
                else None
            ),
            "resourceIds": list(item.resource_ids),
            "baseRevision": item.base_revision,
            "repositoryTarget": (
                item.repository_target.model_dump(mode="json")
                if item.repository_target is not None
                else None
            ),
            "repositoryScope": self._repository_scope_view(item),
            "usage": usage,
            "activity": {
                "toolCalls": usage["toolCalls"],
                "shellCommands": usage["shellCommands"],
                "fileEdits": usage["fileEdits"],
                "gitOperations": usage["gitOperations"],
            },
            "retryLineage": {
                "attemptCount": max(len(attempts), item.fence, 1),
                "retryCount": max(max(len(attempts), item.fence, 1) - 1, 0),
                "attempts": attempts,
            },
            "failure": failure,
            "failureCode": item.failure_code,
            "failureMessage": item.failure_message,
            "artifactIds": list(item.artifact_ids),
            "evidenceIds": list(item.evidence_ids),
        }

    def list_runs(
        self,
        ref: str,
        *,
        organization_id: str,
        workspace_id: str,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), self.MAX_LIMIT))
        worker_state, assignments = self._scoped_assignments(
            ref,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        active_all = [item for item in assignments if item.status in self._ACTIVE]
        active = active_all[: self.MAX_ACTIVE]
        history = [item for item in assignments if item.status not in self._ACTIVE]

        position = self._decode_cursor(cursor)
        if position is not None:
            history = [
                item
                for item in history
                if self._sort_key(item) < position
            ]

        page = history[: limit + 1]
        has_more = len(page) > limit
        page = page[:limit]
        next_cursor = (
            self._encode_cursor(page[-1])
            if has_more and page
            else None
        )
        return {
            "ref": ref,
            "active": [self._summary(worker_state, item) for item in active],
            "activeTruncated": len(active_all) > len(active),
            "items": [self._summary(worker_state, item) for item in page],
            "limit": limit,
            "nextCursor": next_cursor,
            "hasMore": has_more,
        }

    def _artifacts(self, item: ExecutionAssignment) -> tuple[list[Any], list[Any], list[Any]]:
        state = self._safe_load(self.artifact_evidence)
        if state is None:
            return [], [], []
        artifacts = [
            value
            for value in state.artifacts
            if value.organization_id == item.organization_id
            and value.workspace_id == item.workspace_id
            and (
                value.execution_id == item.execution_id
                or value.id in item.artifact_ids
            )
        ]
        evidence = [
            value
            for value in state.evidence
            if value.organization_id == item.organization_id
            and value.workspace_id == item.workspace_id
            and (
                value.execution_id == item.execution_id
                or value.id in item.evidence_ids
            )
        ]
        artifact_ids = {value.id for value in artifacts}
        evidence_ids = {value.id for value in evidence}
        verifications = [
            value
            for value in state.verifications
            if value.organization_id == item.organization_id
            and value.workspace_id == item.workspace_id
            and (
                value.execution_id == item.execution_id
                or bool(artifact_ids.intersection(value.artifact_ids))
                or bool(evidence_ids.intersection(value.evidence_ids))
            )
        ]
        return artifacts, evidence, verifications

    def _actions(self, item: ExecutionAssignment) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        state = self._safe_load(self.action_intents)
        if state is None:
            return [], [], []
        intents = [
            value
            for value in state.intents
            if value.organization_id == item.organization_id
            and value.workspace_id == item.workspace_id
            and value.execution_id == item.execution_id
        ]
        intent_ids = {value.id for value in intents}
        receipts = [
            receipt.id
            for receipt in state.receipts
            if receipt.intent_id in intent_ids
        ]
        verifications = [
            verification.id
            for verification in state.verifications
            if verification.intent_id in intent_ids
        ]
        return (
            [
                {
                    "id": value.id,
                    "status": self._value(value.status),
                    "providerType": value.provider_type,
                    "providerInstance": value.provider_instance,
                    "actionId": value.action_id,
                    "resourceIds": list(value.resource_ids),
                    "attempt": value.attempt,
                    "correlationId": value.correlation_id,
                    "causationId": value.causation_id,
                    "createdAt": value.created_at,
                    "completedAt": value.completed_at,
                    "failure": (
                        value.failure.model_dump(mode="json")
                        if value.failure is not None
                        else None
                    ),
                }
                for value in intents
            ],
            receipts,
            verifications,
        )

    def _approvals(self, item: ExecutionAssignment) -> list[dict[str, Any]]:
        if self.approvals is None:
            return []
        try:
            values = self.approvals.list(
                organization_id=item.organization_id,
                workspace_id=item.workspace_id,
            )
        except Exception:
            return []
        object_ids = {item.execution_id, item.id}
        return [
            {
                "id": value.id,
                "status": self._value(value.status),
                "operation": value.target.operation,
                "objectType": value.target.object_type,
                "objectId": value.target.object_id,
                "reason": value.reason,
                "createdAt": value.created_at,
                "updatedAt": value.updated_at,
            }
            for value in values
            if value.target.object_id in object_ids
        ]

    def _attention(self, item: ExecutionAssignment) -> list[dict[str, Any]]:
        if self.attention is None:
            return []
        try:
            values = self.attention.list()
        except Exception:
            return []
        object_ids = {item.execution_id, item.id}
        return [
            {
                "id": value.id,
                "type": value.type,
                "severity": self._value(value.severity),
                "status": self._value(value.status),
                "reason": value.reason,
                "source": value.source.model_dump(mode="json"),
                "deepLink": value.deep_link,
                "createdAt": value.created_at,
                "updatedAt": value.updated_at,
            }
            for value in values
            if value.organization_id == item.organization_id
            and value.workspace_id == item.workspace_id
            and value.source.object_id in object_ids
        ]

    def _context_checkpoint(
        self,
        ref: str,
        execution_id: str,
    ) -> dict[str, Any] | None:
        if self.work_item_execution is None:
            return None
        try:
            return self.work_item_execution.run_context(ref, execution_id)
        except Exception:
            return None

    @staticmethod
    def _artifact_view(value: Any) -> dict[str, Any]:
        return {
            "id": value.id,
            "type": getattr(value.artifact_type, "value", value.artifact_type),
            "name": value.name,
            "lifecycle": getattr(value.lifecycle, "value", value.lifecycle),
            "externalUrl": value.external_url,
            "revision": value.revision,
            "resourceIds": list(value.resource_ids),
            "producedAt": value.produced_at,
        }

    @staticmethod
    def _evidence_view(value: Any) -> dict[str, Any]:
        return {
            "id": value.id,
            "type": getattr(value.evidence_type, "value", value.evidence_type),
            "result": getattr(value.result, "value", value.result),
            "summary": value.summary,
            "artifactIds": list(value.artifact_ids),
            "deepLink": value.deep_link,
            "observedAt": value.observed_at,
        }

    @staticmethod
    def _verification_view(value: Any) -> dict[str, Any]:
        return {
            "id": value.id,
            "result": getattr(value.result, "value", value.result),
            "method": value.method,
            "artifactIds": list(value.artifact_ids),
            "evidenceIds": list(value.evidence_ids),
            "findings": list(value.findings),
            "deepLink": value.deep_link,
            "verifiedAt": value.verified_at,
        }

    @classmethod
    def _repository_activity(
        cls,
        item: ExecutionAssignment,
        artifacts: list[Any],
        evidence: list[Any],
        verifications: list[Any],
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        scope = cls._repository_scope_view(item) or {}
        repository_ids = set(scope.get("writableRepositoryIds", ())) | set(
            scope.get("readOnlyRepositoryIds", ())
        )

        def repositories(values: Any) -> list[str]:
            return sorted(repository_ids.intersection(values or ()))

        artifact_repositories = {
            value.id: repositories(value.resource_ids)
            for value in artifacts
        }
        evidence_repositories = {
            value.id: sorted(
                {
                    repository_id
                    for artifact_id in value.artifact_ids
                    for repository_id in artifact_repositories.get(artifact_id, ())
                }
            )
            for value in evidence
        }

        rows: list[dict[str, Any]] = []
        for value in artifacts:
            rows.append(
                {
                    "kind": "artifact",
                    "id": value.id,
                    "occurredAt": value.produced_at,
                    "repositoryIds": artifact_repositories[value.id],
                    "label": value.name,
                    "detail": cls._value(value.artifact_type),
                    "status": cls._value(value.lifecycle),
                }
            )
        for value in evidence:
            rows.append(
                {
                    "kind": "evidence",
                    "id": value.id,
                    "occurredAt": value.observed_at,
                    "repositoryIds": evidence_repositories[value.id],
                    "label": value.summary or cls._value(value.evidence_type),
                    "detail": cls._value(value.evidence_type),
                    "status": cls._value(value.result),
                }
            )
        for value in verifications:
            verification_repositories = sorted(
                {
                    repository_id
                    for artifact_id in value.artifact_ids
                    for repository_id in artifact_repositories.get(artifact_id, ())
                }
                | {
                    repository_id
                    for evidence_id in value.evidence_ids
                    for repository_id in evidence_repositories.get(evidence_id, ())
                }
            )
            rows.append(
                {
                    "kind": "verification",
                    "id": value.id,
                    "occurredAt": value.verified_at,
                    "repositoryIds": verification_repositories,
                    "label": value.method,
                    "detail": value.method,
                    "status": cls._value(value.result),
                }
            )
        for value in actions:
            rows.append(
                {
                    "kind": "action",
                    "id": value["id"],
                    "occurredAt": value.get("completedAt") or value.get("createdAt"),
                    "repositoryIds": repositories(value.get("resourceIds")),
                    "label": value.get("actionId"),
                    "detail": value.get("providerType"),
                    "status": value.get("status"),
                }
            )

        return sorted(
            rows,
            key=lambda value: (
                float(value.get("occurredAt") or 0.0),
                str(value.get("kind") or ""),
                str(value.get("id") or ""),
            ),
        )

    def get_run(
        self,
        ref: str,
        execution_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        worker_state, assignments = self._scoped_assignments(
            ref,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        item = next(
            (
                value
                for value in assignments
                if value.execution_id == execution_id
            ),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Run not found")

        result = self._summary(worker_state, item)
        usage_records = self._usage_records(item)
        artifacts, evidence, verifications = self._artifacts(item)
        actions, action_receipts, action_verifications = self._actions(item)
        correlation_ids = sorted(
            {
                action["correlationId"]
                for action in actions
                if action.get("correlationId")
            }
        )
        causation_ids = sorted(
            {
                action["causationId"]
                for action in actions
                if action.get("causationId")
            }
        )
        result.update(
            {
                "correlationIds": correlation_ids,
                "causationIds": causation_ids,
                "usageRecords": [
                    {
                        "id": value.id,
                        "providerId": value.provider_id,
                        "runtimeId": value.runtime_id,
                        "runtimeType": value.runtime_type,
                        "runtimeVersion": value.runtime_version,
                        "models": list(value.observed_model_ids),
                        "inputTokens": value.input_tokens,
                        "outputTokens": value.output_tokens,
                        "reasoningTokens": value.reasoning_output_tokens,
                        "totalTokens": value.total_tokens,
                        "costUsd": value.cost_usd,
                        "runtimeDurationSeconds": value.runtime_duration_seconds,
                        "toolCalls": value.tool_call_count,
                        "shellCommands": value.shell_command_count,
                        "fileEdits": value.file_edit_count,
                        "gitOperations": value.git_operation_count,
                        "telemetryCompleteness": self._value(
                            value.telemetry_completeness
                        ),
                        "terminalOutcome": self._value(value.terminal_outcome),
                        "startedAt": value.started_at,
                        "completedAt": value.completed_at,
                        "evidenceIds": list(value.evidence_ids),
                    }
                    for value in usage_records
                ],
                "artifacts": [self._artifact_view(value) for value in artifacts],
                "evidence": [self._evidence_view(value) for value in evidence],
                "verifications": [
                    self._verification_view(value)
                    for value in verifications
                ],
                "actions": actions,
                "repositoryActivity": self._repository_activity(
                    item,
                    artifacts,
                    evidence,
                    verifications,
                    actions,
                ),
                "actionReceiptIds": action_receipts,
                "actionVerificationIds": action_verifications,
                "approvals": self._approvals(item),
                "attention": self._attention(item),
                "contextCheckpoint": self._context_checkpoint(
                    ref,
                    item.execution_id,
                ),
            }
        )
        return {"ref": ref, "run": result}
