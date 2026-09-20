from __future__ import annotations

from typing import Any

from codex_web.models import BotBinding
from codex_web.services.bot_delivery import BotDeliveryService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.bot_targets import BotTargetService


class GitLabEventPresentationService:
    """Render GitLab agent prompts/notices and deliver Slack notices."""

    def __init__(
        self,
        *,
        delivery: BotDeliveryService,
        targets: BotTargetService,
        telemetry: BotRuntimeTelemetry,
        label_names,
        event_url,
    ) -> None:
        self.delivery = delivery
        self.targets = targets
        self.telemetry = telemetry
        self.label_names = label_names
        self.event_url = event_url

    @staticmethod
    def reference(payload: dict[str, Any]) -> str:
        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        kind = str(
            payload.get("object_kind")
            or payload.get("event_name")
            or "event"
        ).replace("_", " ")
        project_name = (
            project.get("path_with_namespace")
            or project.get("name")
            or "unknown project"
        )
        iid = attrs.get("iid")
        title = (
            attrs.get("title")
            or attrs.get("name")
            or attrs.get("ref")
            or attrs.get("status")
            or ""
        )
        if iid:
            return (
                f"{project_name} {kind} !/#{iid}: {title}"
            ).strip()
        return f"{project_name} {kind}: {title}".strip()

    def format_prompt(
        self,
        payload: dict[str, Any],
        agent: str | None,
    ) -> str:
        attrs = payload.get("object_attributes") or {}
        kind = str(
            payload.get("object_kind")
            or payload.get("event_name")
            or "event"
        )
        action = (
            attrs.get("action")
            or attrs.get("state")
            or attrs.get("status")
            or ""
        )
        labels = self.label_names(payload)
        url = self.event_url(payload)
        lines = [
            (
                "GitLab event received for "
                f"{agent or 'the project'}: {self.reference(payload)}"
            ),
            (
                f"Kind/status: {kind}"
                f"{f' / {action}' if action else ''}"
            ),
        ]
        if labels:
            lines.append("Labels: " + ", ".join(labels))
        if url:
            lines.append(f"URL: {url}")
        if kind == "pipeline":
            lines.append(
                "Pipeline details: "
                + ", ".join(
                    part
                    for part in (
                        (
                            f"ref={attrs.get('ref')}"
                            if attrs.get("ref")
                            else ""
                        ),
                        (
                            "sha="
                            + str(attrs.get("sha") or "")[:12]
                            if attrs.get("sha")
                            else ""
                        ),
                        (
                            f"duration={attrs.get('duration')}"
                            if attrs.get("duration") is not None
                            else ""
                        ),
                    )
                    if part
                )
            )
        lines.extend(
            [
                "",
                (
                    "Handle this event-driven update within your "
                    "directive. Inspect the linked GitLab item/MR/"
                    "pipeline only as needed."
                ),
                (
                    "Before ending the turn, reconcile the affected "
                    "work item through the codex-web /api/work-items "
                    "handoff/ack/progress endpoints as applicable."
                ),
                (
                    "Do not poll GitLab for generic queue state in "
                    "this turn. Keep any Slack/GitLab update concise "
                    "and avoid repeating prior evidence."
                ),
            ]
        )
        return "\n".join(lines)

    def format_notice(
        self,
        payload: dict[str, Any],
        agent: str | None,
        result: dict[str, Any],
    ) -> str:
        attrs = payload.get("object_attributes") or {}
        kind = str(
            payload.get("object_kind")
            or payload.get("event_name")
            or "event"
        ).replace("_", " ")
        action = (
            attrs.get("action")
            or attrs.get("state")
            or attrs.get("status")
            or ""
        )
        labels = self.label_names(payload)
        url = self.event_url(payload)
        route_name = agent or "project"
        dispatch_state = (
            "queued" if result.get("queued") else "started"
        )
        lines = [
            f"GitLab event: {self.reference(payload)}",
            (
                f"Kind/status: {kind}"
                f"{f' / {action}' if action else ''}"
            ),
            f"Routed to: {route_name} ({dispatch_state})",
        ]
        if labels:
            lines.append("Labels: " + ", ".join(labels))
        if url:
            lines.append(f"URL: {url}")
        return "\n".join(lines)

    async def send_notice(
        self,
        binding: BotBinding,
        payload: dict[str, Any],
        agent: str | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if binding.provider != "slack":
            return {
                "sent": False,
                "reason": "unsupported_provider",
            }
        delivery = await self.delivery.send_outbound(
            binding,
            self.format_notice(payload, agent, result),
            username="GitLab",
        )
        self.targets.remember_delivery_target(binding, delivery)
        self.telemetry.append(
            {
                "type": "gitlab_notice_sent",
                "thread_id": binding.thread_id,
                "provider": binding.provider,
                "external_conversation_id": (
                    binding.external_conversation_id
                ),
                "agent": agent,
                "delivery": delivery,
            }
        )
        return delivery
