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
        }
    ),
    "ready_for_validation": frozenset(
        {
            "ready_for_validation",
            "validation_running",
            "implementation_active",
            "failed_with_action_owner",
        }
    ),
    "validation_running": frozenset(
        {
            "validation_running",
            "ready_to_close",
            "implementation_active",
            "failed_with_action_owner",
        }
    ),
    # The blocked stage intentionally permits resuming the exact lane that was
    # blocked. WorkItemState does not yet persist a previous_stage field, so a
    # deterministic recovery may return to any non-terminal active stage.
    "failed_with_action_owner": frozenset(
        {
            "failed_with_action_owner",
            "implementation_active",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
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
    # Reopening a closed GitLab item is an external reconciliation event, not a
    # manual progress transition. Manual callers must not silently resurrect a
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
    """Single authority for mutating the canonical work-item stage.

    Manual/API transitions must satisfy ``WorkItemTransitionPolicy``. External
    projections such as GitLab close/reopen reconciliation intentionally use the
    same mutation primitive while bypassing the manual transition matrix. This
    keeps the mutation point singular without pretending upstream state changes
    are operator/API requests.
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
        state.current_stage = target_stage
        return state
