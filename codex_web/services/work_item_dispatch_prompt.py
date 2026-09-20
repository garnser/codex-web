from __future__ import annotations

import urllib.parse
from collections.abc import Callable

from codex_web.models import WorkItemState


class WorkItemDispatchPromptPolicy:
    """Render canonical owner/handoff wake-up prompts."""

    def __init__(
        self,
        *,
        coerce_owner: Callable[[str | None], str | None],
        coordination_channel: str,
    ) -> None:
        self.coerce_owner = coerce_owner
        self.coordination_channel = coordination_channel

    def render(self, state: WorkItemState) -> str:
        findings_suffix = ""
        if state.blocking_findings:
            findings_suffix = (
                " Supporting findings: "
                + "; ".join(state.blocking_findings[:3])
                + "."
            )
        if state.handoff and state.handoff.status == "pending":
            return (
                f"{state.handoff.to_agent}: structured handoff pending for "
                f"{state.ref}. Acknowledge receipt and intent to process in "
                f"{self.coordination_channel} now. Expected action: "
                f"{state.handoff.expected_action or state.next_action or 'process the handoff'}."
                f"{findings_suffix} If you cannot accept it, emit one exact "
                "blocker immediately. Use "
                f"`/api/work-items/{urllib.parse.quote(state.ref, safe='')}/ack` "
                "before you stop."
            )
        if (
            state.handoff
            and state.handoff.status == "accepted"
            and self.coerce_owner(state.current_owner)
            == self.coerce_owner(state.handoff.to_agent)
        ):
            return (
                f"{state.current_owner or state.next_owner or 'owner'}: accepted "
                f"handoff is live for {state.ref}. Current stage: "
                f"{state.current_stage}. Next action: "
                f"{state.next_action or 'continue the owned lane now'}."
                f"{findings_suffix} Do not leave the lane parked after "
                "acknowledgement. Record concrete progress, an exact blocker, "
                "or an exact handoff in codex-web before you stop."
            )
        if state.current_stage in {
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }:
            return (
                f"{state.current_owner or state.next_owner or 'owner'}: "
                f"release/validation lane for {state.ref}. Current stage: "
                f"{state.current_stage}. Next action: "
                f"{state.next_action or 'acknowledge and process the release-side lane'}."
                f"{findings_suffix} Close the lane or emit one exact blocker in "
                f"{self.coordination_channel}. Reconcile the structured work-item "
                "progress before ending the turn."
            )
        return (
            f"{state.current_owner or state.next_owner or 'owner'}: owned-work "
            f"SLA triggered for {state.ref}. Current stage: {state.current_stage}. "
            f"Next action: {state.next_action or 'state the exact next action and continue the item'}."
            f"{findings_suffix} No passive waiting is allowed. Record the resulting "
            "progress or blocker in codex-web before you stop."
        )
