from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from codex_web.models import BotBinding, BotInboundMessage, BotThreadDetail


SLACK_ICONS: tuple[str, ...] = (
    ":large_blue_circle:", ":large_green_circle:", ":large_orange_circle:",
    ":large_purple_circle:", ":large_yellow_circle:", ":red_circle:",
    ":black_circle:", ":white_circle:", ":brown_circle:",
    ":large_red_square:", ":large_blue_square:", ":large_green_square:",
    ":large_yellow_square:", ":large_orange_square:", ":large_purple_square:",
    ":large_brown_square:", ":black_large_square:", ":white_large_square:",
    ":small_blue_diamond:", ":small_orange_diamond:", ":large_blue_diamond:",
    ":large_orange_diamond:", ":small_red_triangle:", ":small_red_triangle_down:",
    ":eight_pointed_black_star:", ":six_pointed_star:", ":star:", ":sparkles:",
    ":zap:", ":fire:", ":snowflake:", ":sunny:", ":crescent_moon:", ":cloud:",
    ":umbrella:", ":coffee:", ":rocket:", ":satellite:", ":gear:", ":mag:",
    ":lock:", ":key:", ":bell:", ":bookmark:", ":pushpin:", ":paperclip:",
    ":scissors:", ":hammer:", ":wrench:", ":pick:", ":shield:", ":link:",
    ":package:", ":battery:", ":bulb:", ":hourglass:", ":watch:", ":compass:",
    ":anchor:",
)


class BotPresentationService:
    """Own bot route labels, prompt formatting and Slack approval presentation."""

    def __init__(
        self,
        *,
        load_slack_icons: Callable[[], dict[str, str]],
        save_slack_icons: Callable[[dict[str, str]], None],
    ) -> None:
        self.load_slack_icons = load_slack_icons
        self.save_slack_icons = save_slack_icons

    @staticmethod
    def binding_prefix(binding: BotBinding) -> str:
        return (binding.route_prefix or binding.thread_name or binding.thread_id).strip()

    def binding_report_name(self, binding: BotBinding) -> str:
        prefix = self.binding_prefix(binding)
        if " - " in prefix:
            first, rest = prefix.split(" - ", 1)
            if first.strip() and "agent" in rest.lower():
                return first.strip()
        return prefix

    def binding_prefix_candidates(self, binding: BotBinding) -> list[str]:
        candidates = [
            self.binding_prefix(binding),
            self.binding_report_name(binding),
            binding.thread_name or "",
        ]
        seen: set[str] = set()
        result: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip()
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                result.append(normalized)
        return result

    @staticmethod
    def strip_prefix(
        text: str,
        prefix: str,
        *,
        allow_bare_word: bool = False,
    ) -> str | None:
        normalized = text.strip()
        prefix = prefix.strip()
        candidates = [
            f"{prefix}:",
            f"{prefix} -",
            f"[{prefix}]",
            f"@{prefix}",
        ]
        lower = normalized.lower()
        for candidate in candidates:
            if lower.startswith(candidate.lower()):
                return normalized[len(candidate) :].strip()
        if allow_bare_word:
            match = re.match(
                rf"^{re.escape(prefix)}(?:\s+)(.+)$",
                normalized,
                re.IGNORECASE | re.DOTALL,
            )
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def strip_slack_mentions(text: str) -> str:
        return re.sub(r"^(?:<@[A-Z0-9]+>\s*)+", "", text.strip()).strip()

    @staticmethod
    def ambiguous_route_message(prefixes: list[str]) -> str:
        unique = sorted({prefix for prefix in prefixes if prefix})
        if not unique:
            return (
                "I found multiple Codex thread bindings for this conversation, "
                "but none have a usable prefix."
            )
        return "Please include a thread prefix: " + ", ".join(
            f"`{prefix}:`" for prefix in unique
        )

    @staticmethod
    def format_prompt(
        message: BotInboundMessage,
        provider: str,
        text: str,
    ) -> str:
        sender = message.sender_name or message.sender_id or "unknown sender"
        return (
            f"Message received from {provider} conversation "
            f"{message.external_conversation_id} by {sender}.\n\n{text}"
        )

    def slack_reply_username(self, binding: BotBinding) -> str:
        name = self.binding_report_name(binding)
        return f"Codex · {name}" if name else "Codex"

    def slack_reply_icon(self, binding: BotBinding) -> str:
        assignments = self.load_slack_icons()
        assigned = assignments.get(binding.thread_id)
        if assigned:
            return assigned
        used = set(assignments.values())
        seed = f"{self.binding_prefix(binding)}:{binding.thread_id}"
        start = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % len(
            SLACK_ICONS
        )
        for offset in range(len(SLACK_ICONS)):
            candidate = SLACK_ICONS[(start + offset) % len(SLACK_ICONS)]
            if candidate not in used:
                assignments[binding.thread_id] = candidate
                self.save_slack_icons(assignments)
                return candidate
        candidate = SLACK_ICONS[start]
        assignments[binding.thread_id] = candidate
        self.save_slack_icons(assignments)
        return candidate

    @staticmethod
    def is_details_command(text: str) -> bool:
        return text.strip().lower() in {"details", "detail"}

    @staticmethod
    def truncate_text(text: str, limit: int = 28000) -> str:
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        return f"{text[:limit]}\n\n... truncated {omitted} chars"

    @staticmethod
    def format_code_block(text: str, language: str = "") -> str:
        sanitized = text.replace("```", "'''")
        return f"```{language}\n{sanitized}\n```"

    def format_detail_response(self, detail: BotThreadDetail) -> str:
        return (
            f"{detail.title}\n"
            f"{self.format_code_block(self.truncate_text(detail.text))}"
        )

    @staticmethod
    def format_detail_item(
        item: dict[str, Any],
    ) -> dict[str, str] | None:
        item_type = item.get("type")
        if item_type == "commandExecution":
            command = (item.get("command") or "").strip()
            output = (item.get("aggregatedOutput") or "").strip()
            if not command and not output:
                return None
            body = []
            if command:
                body.append(f"$ {command}")
            body.append(output or "No command output.")
            return {
                "title": "Command details",
                "text": "\n\n".join(body),
            }

        if item_type == "fileChange":
            changes = (
                item.get("changes")
                if isinstance(item.get("changes"), list)
                else []
            )
            if not changes:
                return None
            parts = []
            for change in changes:
                path = change.get("path") or "unknown path"
                kind = change.get("kind") or "change"
                diff = (change.get("diff") or "").strip()
                parts.append(f"# {kind}: {path}\n{diff}".strip())
            summary = (
                "File details"
                if len(changes) == 1
                else f"File details ({len(changes)} files)"
            )
            return {"title": summary, "text": "\n\n".join(parts)}
        return None

    def format_outbound_item(
        self,
        item: dict[str, Any],
        prefix: str | None,
    ) -> str | None:
        item_type = item.get("type")
        if item_type == "agentMessage":
            normalized = (item.get("text") or "").strip()
            sender = (prefix or "").strip()
            if sender:
                stripped = self.strip_prefix(normalized, sender)
                if stripped:
                    normalized = stripped
            return normalized or None

        if item_type == "commandExecution":
            command = (item.get("command") or "").strip()
            output = (item.get("aggregatedOutput") or "").strip()
            if not command and not output:
                return None
            parts = ["Command result"]
            if command:
                parts.append(
                    self.format_code_block(
                        self.truncate_text(command, 3000),
                        "sh",
                    )
                )
            if output:
                parts.append(
                    self.format_code_block(self.truncate_text(output))
                )
            else:
                parts.append("_No command output._")
            return "\n".join(parts)

        if item_type == "fileChange":
            changes = (
                item.get("changes")
                if isinstance(item.get("changes"), list)
                else []
            )
            if not changes:
                return None
            summary = (
                f"{len(changes)} file changed"
                if len(changes) == 1
                else f"{len(changes)} files changed"
            )
            parts = [summary]
            remaining = 26000
            for change in changes[:5]:
                path = change.get("path") or "unknown path"
                kind = change.get("kind") or "change"
                diff = (change.get("diff") or "").strip()
                header = (
                    f"*{self.slack_escape(kind)}*: "
                    f"`{self.slack_escape(path)}`"
                )
                if diff:
                    snippet = self.truncate_text(
                        diff,
                        max(1000, remaining),
                    )
                    remaining -= len(snippet)
                    parts.append(
                        f"{header}\n"
                        f"{self.format_code_block(snippet, 'diff')}"
                    )
                else:
                    parts.append(header)
                if remaining <= 0:
                    break
            if len(changes) > 5:
                parts.append(
                    f"... plus {len(changes) - 5} more file changes"
                )
            return "\n".join(parts)
        return None

    @staticmethod
    def slack_escape(value: Any) -> str:
        return (
            str(value if value is not None else "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def approval_summary(request: dict[str, Any]) -> str:
        params = request.get("params") or {}
        method = request.get("method") or "approval"
        command = (
            params.get("command")
            or params.get("reason")
            or (params.get("item") or {}).get("command")
            or (params.get("action") or {}).get("command")
        )
        if command:
            return str(command)
        action = params.get("action") or {}
        if action.get("type") == "applyPatch":
            return "Apply patch: " + ", ".join(action.get("files") or [])
        if method == "item/permissions/requestApproval":
            return params.get("reason") or "Permission change requested"
        text = json.dumps(params, separators=(",", ":"))
        return text[:700] + ("..." if len(text) > 700 else "")

    def approval_blocks(
        self,
        request: dict[str, Any],
        binding: BotBinding,
    ) -> list[dict[str, Any]]:
        request_id = request.get("id")
        summary = self.slack_escape(self.approval_summary(request))
        context = self.slack_escape(
            self.binding_prefix(binding) or binding.thread_id
        )

        def value(decision: str) -> str:
            return json.dumps(
                {"request_id": request_id, "decision": decision},
                separators=(",", ":"),
            )

        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Approval requested for `{context}`*\n```{summary}```",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve once"},
                        "style": "primary",
                        "action_id": "codex_approval_accept",
                        "value": value("accept"),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve session"},
                        "action_id": "codex_approval_accept_session",
                        "value": value("acceptForSession"),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "codex_approval_decline",
                        "value": value("decline"),
                    },
                ],
            },
        ]

    def approval_resolved_blocks(
        self,
        request: dict[str, Any] | None,
        context: str,
        status: str,
    ) -> list[dict[str, Any]]:
        summary = (
            self.slack_escape(self.approval_summary(request))
            if request
            else "This approval request is no longer pending."
        )
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Approval resolved for `{self.slack_escape(context)}`*\n"
                        f"{self.slack_escape(status)}\n```{summary}```"
                    ),
                },
            }
        ]

    @staticmethod
    def slack_interaction_message_ts(payload: dict[str, Any]) -> str | None:
        container = payload.get("container") or {}
        message = payload.get("message") or {}
        return container.get("message_ts") or message.get("ts")

    @staticmethod
    def slack_interaction_context(
        payload: dict[str, Any],
        fallback: str,
    ) -> str:
        blocks = (payload.get("message") or {}).get("blocks") or []
        for block in blocks:
            text = (
                (block.get("text") or {}).get("text")
                if isinstance(block, dict)
                else None
            )
            if not text:
                continue
            match = re.search(r"Approval requested for `([^`]+)`", text)
            if match:
                return match.group(1)
        return fallback


def install_bot_presentation_service(
    app: Any,
    host: Any,
    *,
    load_slack_icons: Callable[[], dict[str, str]] | None = None,
    save_slack_icons: Callable[[dict[str, str]], None] | None = None,
) -> BotPresentationService:
    icon_state = getattr(app.state, "bot_slack_icon_assignments", None)
    if icon_state is None:
        icon_state = {}
        app.state.bot_slack_icon_assignments = icon_state

    load_icons = load_slack_icons or getattr(
        host,
        "_load_slack_thread_icons",
        None,
    )
    save_icons = save_slack_icons or getattr(
        host,
        "_save_slack_thread_icons",
        None,
    )
    if load_icons is None:
        load_icons = lambda: dict(icon_state)
    if save_icons is None:
        def save_icons(values: dict[str, str]) -> None:
            icon_state.clear()
            icon_state.update(values)

    service = BotPresentationService(
        load_slack_icons=load_icons,
        save_slack_icons=save_icons,
    )
    app.state.bot_presentation_service = service
    host._load_slack_thread_icons = load_icons
    host._save_slack_thread_icons = save_icons
    host._binding_prefix = service.binding_prefix
    host._binding_report_name = service.binding_report_name
    host._binding_prefix_candidates = service.binding_prefix_candidates
    host._strip_prefix = service.strip_prefix
    host._strip_slack_mentions = service.strip_slack_mentions
    host._ambiguous_route_message = service.ambiguous_route_message
    host._format_bot_prompt = service.format_prompt
    host._is_details_command = service.is_details_command
    host._truncate_text = service.truncate_text
    host._format_code_block = service.format_code_block
    host._format_bot_detail_response = service.format_detail_response
    host._format_bot_detail_item = service.format_detail_item
    host._format_bot_outbound_item = service.format_outbound_item
    host._slack_reply_username = service.slack_reply_username
    host._slack_reply_icon = service.slack_reply_icon
    host._slack_escape = service.slack_escape
    host._approval_summary = service.approval_summary
    host._approval_blocks = service.approval_blocks
    host._approval_resolved_blocks = service.approval_resolved_blocks
    host._slack_interaction_message_ts = service.slack_interaction_message_ts
    host._slack_interaction_context = service.slack_interaction_context
    return service
