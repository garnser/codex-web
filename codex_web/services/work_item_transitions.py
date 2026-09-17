from __future__ import annotations

from typing import Final

from fastapi import HTTPException

from codex_web.models import WorkItemStage, WorkItemState, WorkItemTerminalOutcome


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
        terminal_outcome: WorkItemTerminalOutcome | None = None,
    ) -> WorkItemStage:
        # Cancellation and terminal failure are explicit terminal decisions and
        # may stop work from any active lane. Successful completion still has to
        # pass through ready_to_close and the ordinary transition matrix.
        if (
            target_stage == "closed"
            and current_stage != "closed"
            and terminal_outcome in {"cancelled", "failed"}
        ):
            return target_stage

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
                "terminal_outcome": terminal_outcome,
                "allowed_targets": sorted(self.allowed_targets(current_stage)),
            },
        )


class WorkItemTransitionService:
    """Single authority for mutating canonical work-item lifecycle state.

    ``closed`` remains the compatibility lifecycle stage. ``terminal_outcome``
    carries the semantically distinct terminal result: completed, cancelled, or
    failed. The resumable ``failed_with_action_owner`` stage is therefore not a
    terminal failure.
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
        terminal_outcome: WorkItemTerminalOutcome | None = None,
    ) -> WorkItemState:
        if terminal_outcome is not None and target_stage != "closed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "terminal_outcome_requires_closed_stage",
                    "message": "A terminal outcome can only be recorded while closing a work item.",
                    "from_stage": state.current_stage,
                    "to_stage": target_stage,
                    "source": source,
                    "terminal_outcome": terminal_outcome,
                },
            )

        if state.current_stage == "closed" and terminal_outcome is not None:
            existing = state.terminal_outcome
            if existing is not None and existing != terminal_outcome:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "terminal_outcome_conflict",
                        "message": "A terminal work-item outcome cannot be rewritten in place.",
                        "existing_terminal_outcome": existing,
                        "requested_terminal_outcome": terminal_outcome,
                        "source": source,
                    },
                )

        if not external_projection:
            self.policy.validate(
                state.current_stage,
                target_stage,
                source=source,
                terminal_outcome=terminal_outcome,
            )

        previous_stage = state.current_stage
        state.current_stage = target_stage

        if target_stage == "closed":
            if terminal_outcome is not None:
                state.terminal_outcome = terminal_outcome
            elif not external_projection and previous_stage != "closed":
                # The only ordinary manual close path is ready_to_close -> closed,
                # which is successful completion unless explicitly classified
                # otherwise.
                state.terminal_outcome = "completed"
        elif previous_stage == "closed":
            # Only an external projection can reopen a closed lane. Its previous
            # terminal classification must not leak into the active lifecycle.
            state.terminal_outcome = None

        return state
