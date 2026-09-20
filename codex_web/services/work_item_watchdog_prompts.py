from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.models import WorkItemState


class WorkItemWatchdogPromptPolicy:
    """Own watchdog prompt rendering and human-readable age formatting."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        project_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        if project_lookup is None and host is not None:
            project_lookup = host._project
        if project_lookup is None:
            raise TypeError(
                "WorkItemWatchdogPromptPolicy requires a project lookup"
            )
        self.project_lookup = project_lookup

    def human_duration(self, seconds: float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        minutes, secs = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
        hours, mins = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h" if mins == 0 else f"{hours}h {mins}m"
        days, hrs = divmod(hours, 24)
        return f"{days}d" if hrs == 0 else f"{days}d {hrs}h"

    def format_orchestrator_prompt(
        self,
        project_id: str,
        items: list[tuple[str, WorkItemState, float]],
    ) -> str:
        project = self.project_lookup(project_id)
        lines = [
            "Orchestrator: autonomous follow-up sweep.",
            f"Project: {project.name} ({project.path})",
            "Keep the work-item loop closed until each listed item is assigned, acknowledged, advanced, or closed.",
            "",
            "Required in this turn:",
            "1. Push the named owner or next owner if follow-up is needed.",
            "2. Record the resulting ownership/progress decision through codex-web `/api/work-items` before you stop.",
            "3. Do not leave any listed item without one exact next action.",
            "",
            "Items needing orchestration:",
        ]
        for index, (reason, state, age) in enumerate(items[:8], start=1):
            owner = state.current_owner or state.next_owner or "unassigned"
            action = state.next_action or "set one exact next action"
            lines.append(
                f"{index}. {state.ref} | trigger={reason} | owner={owner} | "
                f"stage={state.current_stage} | age={self.human_duration(age)}"
            )
            lines.append(f"   next_action={action}")
            if state.handoff and state.handoff.status == "pending":
                lines.append(
                    "   pending_handoff="
                    + f"{state.handoff.from_agent}->{state.handoff.to_agent} for "
                    + self.human_duration(time.time() - state.handoff.requested_at)
                )
            if state.blocker:
                lines.append(f"   blocker={state.blocker}")
            if state.blocking_findings:
                lines.append("   blocking_findings=" + " | ".join(state.blocking_findings[:3]))
        if len(items) > 8:
            lines.append(f"... plus {len(items) - 8} more stale items.")
        lines.append("")
        lines.append(
            "If a listed item is already moving, reconcile the structured state anyway "
            "and explicitly state who owns the next step."
        )
        return "\n".join(lines)

    def format_split_brain_prompt(
        self,
        project_id: str,
        items: list[tuple[WorkItemState, list[str]]],
    ) -> str:
        project = self.project_lookup(project_id)
        lines = [
            "Orchestrator: continuous split-brain monitor triggered.",
            f"Project: {project.name} ({project.path})",
            "Reconcile each item to one canonical owner, one canonical stage, and one canonical next action in this turn.",
            "",
            "Items with live owner/stage/handoff drift:",
        ]
        for index, (state, findings) in enumerate(items[:8], start=1):
            lines.append(
                f"{index}. {state.ref} | owner={state.current_owner or 'unassigned'} | "
                f"stage={state.current_stage} | next_owner={state.next_owner or 'none'}"
            )
            for finding in findings:
                lines.append(f"   - {finding}")
            if state.next_action:
                lines.append(f"   next_action={state.next_action}")
            if state.blocking_findings:
                lines.append("   blocking_findings=" + " | ".join(state.blocking_findings[:3]))
        if len(items) > 8:
            lines.append(f"... plus {len(items) - 8} more split-brain items.")
        lines.append("")
        lines.append("Record the reconciliation through codex-web `/api/work-items` before you stop.")
        return "\n".join(lines)


def install_work_item_watchdog_prompt_policy(
    app: Any,
    host: Any,
    *,
    project_lookup: Callable[[str], Any] | None = None,
) -> WorkItemWatchdogPromptPolicy:
    existing = getattr(app.state, "work_item_watchdog_prompt_policy", None)
    if isinstance(existing, WorkItemWatchdogPromptPolicy):
        policy = existing
    else:
        policy = WorkItemWatchdogPromptPolicy(
            host,
            project_lookup=project_lookup,
        )
        app.state.work_item_watchdog_prompt_policy = policy

    host._human_duration = policy.human_duration
    host._format_orchestrator_watchdog_prompt = policy.format_orchestrator_prompt
    host._format_split_brain_watchdog_prompt = policy.format_split_brain_prompt
    return policy
