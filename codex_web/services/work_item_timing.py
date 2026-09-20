from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from codex_web.models import WorkItemState


class WorkItemTimingPolicy:
    """Own work-item handoff and SLA timing policy outside the legacy runtime."""

    def __init__(
        self,
        coerce_owner: Callable[[str | None], str | None],
    ) -> None:
        self.coerce_owner = coerce_owner

    @staticmethod
    def handoff_timeout_seconds() -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_WORK_ITEM_HANDOFF_TIMEOUT_SECONDS"
                )
                or "900"
            )
        except ValueError:
            return 900.0
        return max(60.0, seconds)

    @staticmethod
    def progress_sla_seconds() -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS"
                )
                or "3600"
            )
        except ValueError:
            return 3600.0
        return max(300.0, seconds)

    @staticmethod
    def release_validation_sla_seconds() -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_RELEASE_VALIDATION_SLA_SECONDS"
                )
                or "1800"
            )
        except ValueError:
            return 1800.0
        return max(300.0, seconds)

    @staticmethod
    def accepted_handoff_owner_idle_seconds() -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS"
                )
                or "300"
            )
        except ValueError:
            return 300.0
        return max(60.0, seconds)

    def sla_threshold_seconds(self, state: WorkItemState) -> float:
        accepted_by_current_owner = (
            state.handoff
            and state.handoff.status == "accepted"
            and self.coerce_owner(state.current_owner)
            == self.coerce_owner(state.handoff.to_agent)
        )
        release_stage = state.current_stage in {
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }
        if accepted_by_current_owner:
            baseline = (
                self.release_validation_sla_seconds()
                if release_stage
                else self.progress_sla_seconds()
            )
            return min(
                baseline,
                self.accepted_handoff_owner_idle_seconds(),
            )
        if release_stage:
            return self.release_validation_sla_seconds()
        return self.progress_sla_seconds()


def install_work_item_timing_policy(
    app: Any,
    host: Any,
    *,
    coerce_owner: Callable[[str | None], str | None] | None = None,
) -> WorkItemTimingPolicy:
    existing = getattr(app.state, "work_item_timing_policy", None)
    if isinstance(existing, WorkItemTimingPolicy):
        policy = existing
    else:
        policy = WorkItemTimingPolicy(
            coerce_owner or host._coerce_owner,
        )
        app.state.work_item_timing_policy = policy

    # Output-only compatibility aliases while legacy_core is deleted.
    host._work_item_handoff_timeout_seconds = (
        policy.handoff_timeout_seconds
    )
    host._work_item_progress_sla_seconds = (
        policy.progress_sla_seconds
    )
    host._release_validation_sla_seconds = (
        policy.release_validation_sla_seconds
    )
    host._accepted_handoff_owner_idle_seconds = (
        policy.accepted_handoff_owner_idle_seconds
    )
    host._work_item_sla_threshold_seconds = (
        policy.sla_threshold_seconds
    )
    return policy
