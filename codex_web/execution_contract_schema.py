from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_web.execution_contracts import ExecutionRoleContract
from codex_web.models import ArtifactState, HandoffStatus, WorkItemStage, WorkItemState


EXECUTION_CONTRACT_SCHEMA_VERSION = "1.0"


class ExecutionTargetV1(BaseModel):
    """Repository/environment target known at work-item dispatch time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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


class ExecutionContractV1(BaseModel):
    """Versioned execution contract derived from canonical work-item state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = EXECUTION_CONTRACT_SCHEMA_VERSION
    work_item_ref: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    agent_id: str | None = None
    target: ExecutionTargetV1
    permissions: ExecutionPermissionsV1 = Field(default_factory=ExecutionPermissionsV1)
    inputs: CanonicalWorkItemInputV1
    expected_outputs: tuple[str, ...]
    success_criteria: tuple[str, ...]
    failure_conditions: tuple[str, ...]

    def compact_public(self) -> dict[str, object]:
        """Return stable machine-readable data without null target noise."""

        return self.model_dump(mode="json", exclude_none=True)


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

    return ExecutionContractV1(
        work_item_ref=state.ref,
        role_id=role.id,
        agent_id=agent_id,
        target=ExecutionTargetV1(repository=state.project_path),
        inputs=CanonicalWorkItemInputV1(
            current_stage=state.current_stage,
            artifact_state=state.artifact_state,
            next_action=state.next_action,
            blocker=state.blocker,
            handoff_from=handoff.from_agent if handoff else None,
            handoff_to=handoff.to_agent if handoff else None,
            handoff_status=handoff.status if handoff else None,
            split_brain_findings=tuple(split_brain_findings),
        ),
        expected_outputs=tuple(role.required_artifacts),
        success_criteria=(
            "Produce the artifacts required by the resolved execution role.",
            "Record concrete progress, an explicit handoff, or one exact blocker in canonical work-item state.",
            "Preserve the inherited sandbox and approval-policy controls.",
        ),
        failure_conditions=tuple(role.failure_conditions),
    )
