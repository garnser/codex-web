from __future__ import annotations

from typing import Final

from fastapi import HTTPException

from codex_web.models import WorkItemStage, WorkItemState


WORK_ITEM_STAGES: Final[tuple[WorkItemStage, ...]] = (
    "implementation_active",
    "ready_for_validation",
    "validation_running",
    "failed_with_action_owner",
    "ready_to_close",
    "closed",
)

# These are canonical operator/API transitions. GitLab reconciliation is an
# external-state projection path and is deliberately handled separately by the
# state machine so a reopened/closed upstream issue can be reconciled without
# weakening the manual transition rules below.
WORK_ITEM_STAGE_TRANSITIONS: Final[dict[WorkItemStage, frozenset[WorkItemStage]]] = {
    "implementation_active": frozenset(
        {
            "implementation_active",
            "ready_for_validation",
            "failed_with_action_owner",
            "closed",
        }
    ),
    "ready_for_validation": frozenset(
        {
            "ready_for_validation",
            "validation_running",
            "implementation_active",
            "failed_with_action_owner",
            "closed",
        }
    ),
    "validation_running": frozenset(
        {
            "validation_running",
            "ready_to_close",
            "implementation_active",
            "failed_with_action_owner",
            "closed",
        }
    ),
    # The blocked stage intentionally permits resuming the exact lane that was
    # blocked. Closing from this lane is the explicit terminal-failure path.
    "failed_with_action_owner": frozenset(
        {
            "failed_with_action_owner",
            "implementation_active",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
            "closed",
        }
    ),
    "ready_to_close": frozenset(
        {
            "ready_to_close",
            "closed",
            "implementation_active",
            "ready_for_validation",
            "validation_running",
            "failed_with_action_owner",
        }
    ),
    # Reopening a closed external item is an external reconciliation event, not
    # a manual progress transition. Manual callers must not silently resurrect a
    # terminal lane.
    "closed": frozenset({"closed"}),
}


class WorkItemTransitionPolicy:
    """Deterministically validate canonical manual work-item stage changes."""

    def allowed_targets(self, current_stage: WorkItemStage) -> frozenset[WorkItemStage]:
        return WORK_ITEM_STAGE_TRANSITIONS[current_stage]

    def validate(
        self,
        current_stage: WorkItemStage,
        target_stage: WorkItemStage,
        *,
        source: str,
    ) -> WorkItemStage:
        if target_stage in self.allowed_targets(current_stage):
            return target_stage

        raise HTTPException(
            status_code=409,
            detail={
                "code": "invalid_stage_transition",
                "message": f"Work item cannot transition from {current_stage} to {target_stage} via {source}.",
                "from_stage": current_stage,
                "to_stage": target_stage,
                "source": source,
                "allowed_targets": sorted(self.allowed_targets(current_stage)),
            },
        )


class WorkItemTransitionService:
    """Single authority for mutating canonical work-item lifecycle state.

    ``closed`` remains the persisted compatibility lifecycle stage while
    ``terminal_outcome`` records the semantic terminal result. Manual closure is
    deterministic from the lane being closed:

    - ``ready_to_close`` -> ``completed``
    - ``failed_with_action_owner`` -> ``failed``
    - any other active lane -> ``cancelled``

    The resumable ``failed_with_action_owner`` stage is therefore distinct from
    terminal failure. External task-source projections use the same stage
    mutator but do not guess a provider-neutral terminal outcome.
    """

    def __init__(self, policy: WorkItemTransitionPolicy | None = None) -> None:
        self.policy = policy or WorkItemTransitionPolicy()

    def transition(
        self,
        state: WorkItemState,
        target_stage: WorkItemStage,
        *,
        source: str,
        external_projection: bool = False,
    ) -> WorkItemState:
        if not external_projection:
            self.policy.validate(state.current_stage, target_stage, source=source)

        previous_stage = state.current_stage
        state.current_stage = target_stage

        if target_stage == "closed" and previous_stage != "closed":
            if external_projection:
                # Provider-specific close reasons are intentionally not guessed
                # here. A task-source adapter can classify them later when the
                # source exposes enough semantics.
                state.terminal_outcome = None
            elif previous_stage == "ready_to_close":
                state.terminal_outcome = "completed"
            elif previous_stage == "failed_with_action_owner":
                state.terminal_outcome = "failed"
            else:
                state.terminal_outcome = "cancelled"
        elif previous_stage == "closed" and target_stage != "closed":
            # Only an external projection can reopen a closed item. Clear the
            # stale terminal result when work becomes active again.
            state.terminal_outcome = None

        return state
