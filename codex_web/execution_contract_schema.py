from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_web.artifact_evidence import EvidenceRequirement
from codex_web.execution_contracts import ExecutionRoleContract
from codex_web.execution_workspaces import ExecutionWorkspaceReference
from codex_web.models import ArtifactState, HandoffStatus, WorkItemStage, WorkItemState


EXECUTION_CONTRACT_SCHEMA_VERSION = "1.4"


class ExecutionTargetV1(BaseModel):
    """Canonical plus compatibility targets known at work-item dispatch time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_ids: tuple[str, ...] = ()
    workspace: ExecutionWorkspaceReference | None = None
    repository: str | None = None
    branch: str | None = None
    environment: str | None = None


class ExecutionPermissionsV1(BaseModel):
    """Existing execution controls inherited by the work-item contract.

    Milestone 2 records the permission boundary without introducing a second
    authority system. Concrete role/action authority is intentionally left to
    the later authority milestone. A contract can never weaken the thread or
    project sandbox/approval policy it inherits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["thread-project-policy"] = "thread-project-policy"
    sandbox: Literal["inherit"] = "inherit"
    approval_policy: Literal["inherit"] = "inherit"
    can_weaken_controls: Literal[False] = False


class CanonicalWorkItemInputV1(BaseModel):
    """Minimum structured work-item state needed to execute the contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_stage: WorkItemStage
    artifact_state: ArtifactState
    next_action: str | None = None
    blocker: str | None = None
    handoff_from: str | None = None
    handoff_to: str | None = None
    handoff_status: HandoffStatus | None = None
    split_brain_findings: tuple[str, ...] = ()
    retry_attempt: int = 0
    retry_max_attempts: int = 3
    timeout_seconds: float | None = None
    deadline_at: float | None = None
    failure_category: str | None = None
    failure_code: str | None = None
    checkpoint_id: str | None = None
    checkpoint_summary: str | None = None


class ExecutionAccountingV1(BaseModel):
    """Stable attribution hooks for model usage recorded by the executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_ref: str = Field(min_length=1)
    checkpoint_id: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    usage_recording_required: Literal[True] = True


class ExecutionContractV1(BaseModel):
    """Versioned execution contract derived from canonical work-item state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.4"] = EXECUTION_CONTRACT_SCHEMA_VERSION
    work_item_ref: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    agent_id: str | None = None
    target: ExecutionTargetV1
    permissions: ExecutionPermissionsV1 = Field(default_factory=ExecutionPermissionsV1)
    inputs: CanonicalWorkItemInputV1
    accounting: ExecutionAccountingV1
    expected_outputs: tuple[str, ...]
    required_evidence: tuple[EvidenceRequirement, ...] = ()
    success_criteria: tuple[str, ...]
    failure_conditions: tuple[str, ...]

    def compact_public(self) -> dict[str, object]:
        """Return stable machine-readable data without null target noise."""

        payload = self.model_dump(mode="json", exclude_none=True)
        target = payload.get("target")
        if isinstance(target, dict) and not target.get("resource_ids"):
            target.pop("resource_ids", None)
        return payload


def execution_contract_for_work_item(
    state: WorkItemState,
    role: ExecutionRoleContract,
    *,
    split_brain_findings: list[str] | tuple[str, ...] = (),
) -> ExecutionContractV1:
    """Build and validate the canonical v1 contract before work is dispatched."""

    handoff = state.handoff
    pending_recipient = (
        handoff.to_agent
        if handoff is not None and handoff.status == "pending"
        else None
    )
    agent_id = pending_recipient or state.current_owner
    lifecycle = state.execution
    checkpoint = lifecycle.latest_checkpoint
    failure = lifecycle.failure_reason

    return ExecutionContractV1(
        work_item_ref=state.ref,
        role_id=role.id,
        agent_id=agent_id,
        target=ExecutionTargetV1(
            resource_ids=tuple(dict.fromkeys(state.resource_ids)),
            workspace=lifecycle.workspace,
            repository=state.project_path,
        ),
        inputs=CanonicalWorkItemInputV1(
            current_stage=state.current_stage,
            artifact_state=state.artifact_state,
            next_action=state.next_action,
            blocker=state.blocker,
            handoff_from=handoff.from_agent if handoff else None,
            handoff_to=handoff.to_agent if handoff else None,
            handoff_status=handoff.status if handoff else None,
            split_brain_findings=tuple(split_brain_findings),
            retry_attempt=lifecycle.retry.attempt,
            retry_max_attempts=lifecycle.retry.policy.max_attempts,
            timeout_seconds=lifecycle.timeout_seconds,
            deadline_at=lifecycle.deadline_at,
            failure_category=failure.category if failure else None,
            failure_code=failure.code if failure else None,
            checkpoint_id=checkpoint.id if checkpoint else None,
            checkpoint_summary=checkpoint.summary if checkpoint else None,
        ),
        accounting=ExecutionAccountingV1(
            work_item_ref=state.ref,
            checkpoint_id=checkpoint.id if checkpoint else None,
            goal_id=lifecycle.usage.goal_id,
            decision_id=lifecycle.usage.decision_id,
        ),
        expected_outputs=tuple(role.required_artifacts),
        required_evidence=tuple(lifecycle.evidence_requirements),
        success_criteria=(
            "Produce the artifacts required by the resolved execution role.",
            "Record concrete progress, an explicit handoff, or one exact blocker in canonical work-item state.",
            "Preserve the inherited sandbox and approval-policy controls.",
            "Record model usage against the work item when model execution occurs.",
        ),
        failure_conditions=tuple(role.failure_conditions),
    )
