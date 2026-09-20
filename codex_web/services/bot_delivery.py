from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from types import MethodType, SimpleNamespace
from typing import Any

from fastapi import HTTPException

from codex_web.integrations.slack_client import SlackClient
from codex_web.integrations.telegram_client import TelegramClient
from codex_web.models import BotBinding, BotConnection
from codex_web.services.approvals import ApprovalService
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_details import BotDetailService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.bot_targets import BotTargetService
from codex_web.services.secrets import SecretBroker
from codex_web.services.thread_bot_collaboration import (
    ThreadBotCollaborationService,
)


class BotDeliveryService:
    """Own outbound provider delivery and Slack approval projection."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        connections: BotConnectionService | Any | None = None,
        bindings: BotBindingSelectionService | Any | None = None,
        targets: BotTargetService | Any | None = None,
        presentation: BotPresentationService | Any | None = None,
        details: BotDetailService | Any | None = None,
        telemetry: BotRuntimeTelemetry | Any | None = None,
        collaboration: ThreadBotCollaborationService | Any | None = None,
        approvals: ApprovalService | Any | None = None,
        publish_event: Callable[[dict[str, Any]], Awaitable[object]] | None = None,
        workflow_claim_findings: Callable[[str], tuple[list[str], list[Any]]] | None = None,
        workflow_correction: Callable[[str, list[Any], list[str]], str] | None = None,
        slack_client: SlackClient | None = None,
        telegram_client: TelegramClient | None = None,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        async def _publish_noop(_event: dict[str, Any]) -> None:
            return None

        if host is not None:
            connections = connections or SimpleNamespace(
                get=getattr(host, "_bot_connection", lambda _connection_id: None),
                runtime_actor=getattr(host, "_bot_runtime_actor", lambda _project_id: None),
            )
            targets = targets or SimpleNamespace(
                thread_target_for_outbound=getattr(
                    host,
                    "_thread_target_for_outbound",
                    lambda _binding, _reply: (None, False),
                )
            )
            presentation = presentation or SimpleNamespace(
                slack_reply_username=getattr(
                    host,
                    "_slack_reply_username",
                    lambda _binding: "Codex",
                ),
                slack_reply_icon=getattr(
                    host,
                    "_slack_reply_icon",
                    lambda _binding: ":robot_face:",
                ),
            )

        self.connections = connections or SimpleNamespace(
            get=lambda _connection_id: None,
            runtime_actor=lambda _project_id: None,
        )
        self.bindings = bindings or SimpleNamespace(for_thread=lambda _thread_id: [])
        self.targets = targets or SimpleNamespace(
            thread_target_for_outbound=lambda _binding, _reply: (None, False)
        )
        self.presentation = presentation or SimpleNamespace(
            slack_reply_username=lambda _binding: "Codex",
            slack_reply_icon=lambda _binding: ":robot_face:",
        )
        self.details = details or SimpleNamespace()
        self.telemetry = telemetry or SimpleNamespace(append=lambda _event: None)
        self.collaboration = collaboration or SimpleNamespace()
        self.approvals = approvals or SimpleNamespace()
        self.publish_event = publish_event or _publish_noop
        self.workflow_claim_findings = workflow_claim_findings or (
            lambda _text: ([], [])
        )
        self.workflow_correction = workflow_correction or (
            lambda text, _states, _findings: text
        )
        self.slack = slack_client or SlackClient()
        self.telegram = telegram_client or TelegramClient()
        self.secret_broker = secret_broker

    @staticmethod
    def _credential_identity(
        connection: BotConnection,
        field: str,
    ) -> str | None:
        return getattr(connection, f"{field}_secret_id", None) or getattr(
            connection,
            field,
            None,
        )

    async def _with_credential(
        self,
        connection: BotConnection,
        field: str,
        operation: str,
        consumer: Any,
    ) -> Any:
        secret_id = getattr(connection, f"{field}_secret_id", None)
        if secret_id and self.secret_broker is not None:
            return await self.secret_broker.use_async(
                secret_id,
                actor=self.connections.runtime_actor(connection.project_id),
                operation=operation,
                consumer=consumer,
                context={
                    "connection_id": connection.id,
                    "provider": connection.provider,
                },
            )
        raw = getattr(connection, field, None)
        if raw:
            return await consumer(raw)
        raise RuntimeError(f"Bot connection is missing {field}")

    async def send_outbound(
        self,
        binding: BotBinding,
        text: str,
        *,
        reply_in_thread: bool | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
    ) -> dict[str, Any]:
        connection = (
            self.connections.get(binding.connection_id)
            if binding.connection_id
            else None
        )
        if (
            connection is None
            or not self._credential_identity(connection, "bot_token")
        ):
            return {"sent": False, "reason": "missing_bot_token"}
        try:
            if binding.provider == "slack":
                target, should_thread = self.targets.thread_target_for_outbound(
                    binding,
                    reply_in_thread,
                )
                thread_ts = (
                    (target.external_thread_id or target.message_id)
                    if (should_thread and target)
                    else None
                )

                async def send_slack(token: str):
                    return await self.slack.post_message(
                        token,
                        binding.external_conversation_id,
                        text,
                        username=(
                            username
                            or self.presentation.slack_reply_username(binding)
                        ),
                        icon_emoji=(
                            icon_emoji
                            or self.presentation.slack_reply_icon(binding)
                        ),
                        thread_ts=thread_ts,
                    )

                return await self._with_credential(
                    connection,
                    "bot_token",
                    "slack.post_message",
                    send_slack,
                )
            if binding.provider == "telegram":

                async def send_telegram(token: str):
                    return await self.telegram.send_message(
                        token,
                        binding.external_conversation_id,
                        text,
                    )

                return await self._with_credential(
                    connection,
                    "bot_token",
                    "telegram.send_message",
                    send_telegram,
                )
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}
        return {"sent": False, "reason": "unsupported_provider"}

    async def send_details(self, binding: BotBinding) -> dict[str, Any]:
        detail = self.details.latest(binding.thread_id)
        if not detail:
            return await self.send_outbound(
                binding,
                "No command or file details are available for this thread yet.",
                reply_in_thread=True,
            )
        return await self.send_outbound(
            binding,
            self.presentation.format_detail_response(detail),
            reply_in_thread=True,
        )

    async def record_outbound(self, message: dict[str, Any]) -> None:
        if message.get("method") != "item/completed":
            return
        params = message.get("params") or {}
        item = params.get("item") or {}
        thread_id = params.get("threadId")
        if not thread_id:
            return
        detail = self.presentation.format_detail_item(item)
        if detail:
            self.details.record(
                thread_id,
                item.get("type") or "detail",
                detail["title"],
                detail["text"],
            )
            return
        if item.get("type") != "agentMessage":
            return
        bindings = self.bindings.for_thread(thread_id)
        if not bindings:
            bindings = await self.collaboration.project_scoped_bindings_for_thread(
                thread_id
            )
        for binding in self.targets.outbound_bindings_for_thread(
            thread_id,
            bindings,
        ):
            route_prefix = self.presentation.binding_prefix(binding)
            report_name = self.presentation.binding_report_name(binding)
            outbound_text = self.presentation.format_outbound_item(
                item,
                report_name,
            )
            if not outbound_text:
                continue
            workflow_findings, mentioned_states = (
                self.workflow_claim_findings(outbound_text)
            )
            workflow_verification: dict[str, Any] | None = None
            if workflow_findings:
                workflow_verification = {
                    "corrected": True,
                    "findings": workflow_findings,
                    "refs": [state.ref for state in mentioned_states],
                    "original_text": self.presentation.truncate_text(
                        outbound_text,
                        1000,
                    ),
                }
                outbound_text = self.workflow_correction(
                    report_name,
                    mentioned_states,
                    workflow_findings,
                )
            event: dict[str, Any] = {
                "type": "outbound_ready",
                "provider": binding.provider,
                "external_conversation_id": binding.external_conversation_id,
                "thread_id": thread_id,
                "thread_name": binding.thread_name,
                "route_prefix": route_prefix,
                "report_name": report_name,
                "item_type": item.get("type"),
                "text": outbound_text,
            }
            if workflow_verification:
                event["workflow_verification"] = workflow_verification
            delivery = await self.send_outbound(binding, outbound_text)
            self.targets.remember_delivery_target(binding, delivery)
            event["delivery"] = delivery
            self.telemetry.append(event)
            await self.publish_event({"type": "bot.outbound", **event})

    async def record_approval_request(
        self,
        request: dict[str, Any],
    ) -> None:
        thread_id = self.approvals.thread_id(request)
        if not thread_id:
            return
        for binding in self.targets.outbound_bindings_for_thread(
            thread_id,
            self.bindings.for_thread(thread_id),
        ):
            if binding.provider != "slack" or not binding.connection_id:
                continue
            connection = self.connections.get(binding.connection_id)
            if not self._credential_identity(connection, "bot_token"):
                continue
            text = (
                "Approval requested for "
                f"{self.presentation.binding_prefix(binding) or thread_id}"
            )
            target = (
                self.targets.active_reply_target_for_binding(binding)
                or self.targets.reply_target_for_binding(binding)
            )
            _, should_thread = self.targets.thread_target_for_outbound(binding)
            thread_ts = (
                (target.external_thread_id or target.message_id)
                if (should_thread and target)
                else None
            )

            async def send_approval(token: str):
                return await self.slack.post_message(
                    token,
                    binding.external_conversation_id,
                    text,
                    username=self.presentation.slack_reply_username(binding),
                    icon_emoji=self.presentation.slack_reply_icon(binding),
                    thread_ts=thread_ts,
                    blocks=self.presentation.approval_blocks(
                        request,
                        binding,
                    ),
                )

            delivery = await self._with_credential(
                connection,
                "bot_token",
                "slack.approval_request",
                send_approval,
            )
            response = delivery.get("providerResponse") or {}
            if delivery.get("sent") and response.get("ts"):
                self.approvals.remember_message(
                    request.get("id"),
                    connection_id=connection.id,
                    channel=binding.external_conversation_id,
                    message_ts=str(response["ts"]),
                    context=(
                        self.presentation.binding_prefix(binding)
                        or thread_id
                    ),
                    thread_id=thread_id,
                )
            self.telemetry.append(
                {
                    "type": "approval_request_sent",
                    "provider": "slack",
                    "external_conversation_id": (
                        binding.external_conversation_id
                    ),
                    "thread_id": thread_id,
                    "request_id": request.get("id"),
                    "delivery": delivery,
                }
            )

    async def update_approval_messages(
        self,
        request_id: int | str,
        request: dict[str, Any],
        *,
        decision: str,
        actor: str,
    ) -> None:
        if self.approvals.load_approval_messages is None:
            return
        messages = self.approvals.load_approval_messages().get(
            str(request_id),
            [],
        )
        status = f"{actor} selected `{decision}`."
        for message in messages:
            with contextlib.suppress(Exception):
                connection = self.connections.get(message.connection_id)
                if not self._credential_identity(connection, "bot_token"):
                    continue

                async def update_message(token: str):
                    return await self.slack.update_message(
                        token,
                        message.channel,
                        message.message_ts,
                        (
                            f"{actor} selected {decision} for approval "
                            f"request {request_id}."
                        ),
                        blocks=self.presentation.approval_resolved_blocks(
                            request,
                            message.context,
                            status,
                        ),
                    )

                await self._with_credential(
                    connection,
                    "bot_token",
                    "slack.approval_update",
                    update_message,
                )
        self.approvals.forget_messages(request_id)

    async def resolve_approval_request(
        self,
        request_id: int | str,
        decision: str,
        *,
        actor: str,
    ) -> dict[str, bool]:
        request = self.approvals.pending().get(request_id)
        if not request:
            raise HTTPException(
                status_code=404,
                detail="Approval request not found",
            )
        result = self.approvals.approval_result(
            request["method"],
            decision,
        )
        if self.approvals.canonical is not None:
            await self.approvals.respond_compatibility(
                request_id,
                result,
            )
        else:
            await self.approvals.respond(request_id, result)
        await self.update_approval_messages(
            request_id,
            request,
            decision=decision,
            actor=actor,
        )
        self.telemetry.append(
            {
                "type": "approval_resolved",
                "request_id": request_id,
                "decision": decision,
                "actor": actor,
                "thread_id": self.approvals.thread_id(request),
            }
        )
        return {"ok": True}

    async def handle_slack_interaction(
        self,
        connection: BotConnection,
        payload: dict[str, Any],
    ) -> None:
        actions = payload.get("actions") or []
        for action in actions:
            if not str(action.get("action_id") or "").startswith(
                "codex_approval_"
            ):
                continue
            try:
                value = json.loads(action.get("value") or "{}")
            except json.JSONDecodeError:
                continue
            request_id = self.approvals.request_id_value(
                value.get("request_id")
            )
            decision = value.get("decision")
            request = self.approvals.pending().get(request_id)
            channel = (
                (payload.get("channel") or {}).get("id")
                or connection.default_external_conversation_id
            )
            message_ts = self.presentation.slack_interaction_message_ts(
                payload
            )
            user = (
                (payload.get("user") or {}).get("username")
                or (payload.get("user") or {}).get("id")
                or "Slack"
            )
            context = self.presentation.slack_interaction_context(
                payload,
                str(request_id),
            )
            if not request or not decision:
                if (
                    channel
                    and message_ts
                    and self._credential_identity(connection, "bot_token")
                ):

                    async def update_expired(token: str):
                        return await self.slack.update_message(
                            token,
                            channel,
                            message_ts,
                            "That approval request is no longer pending.",
                            blocks=(
                                self.presentation.approval_resolved_blocks(
                                    None,
                                    context,
                                    "Already resolved.",
                                )
                            ),
                        )

                    await self._with_credential(
                        connection,
                        "bot_token",
                        "slack.approval_expired",
                        update_expired,
                    )
                return
            await self.resolve_approval_request(
                request_id,
                decision,
                actor=user,
            )
            self.telemetry.append(
                {
                    "type": "approval_resolved_from_slack",
                    "provider": "slack",
                    "connection_id": connection.id,
                    "request_id": request_id,
                    "decision": decision,
                    "user": user,
                }
            )


def install_bot_delivery_service(
    app: Any,
    host: Any,
    *,
    connections=None,
    bindings=None,
    targets=None,
    presentation=None,
    details=None,
    telemetry=None,
    collaboration=None,
    approvals=None,
    publish_event=None,
    workflow_claim_findings=None,
    workflow_correction=None,
    slack_client: SlackClient | None = None,
    telegram_client: TelegramClient | None = None,
) -> BotDeliveryService:
    if (
        connections is None
        and not hasattr(app.state, "bot_connection_service")
    ):
        service = BotDeliveryService(
            host,
            slack_client=slack_client,
            telegram_client=telegram_client,
            secret_broker=getattr(app.state, "secret_broker", None),
        )
    else:
        service = BotDeliveryService(
            connections=connections or app.state.bot_connection_service,
            bindings=bindings or app.state.bot_binding_selection_service,
            targets=targets or app.state.bot_target_service,
            presentation=presentation or app.state.bot_presentation_service,
            details=details or app.state.bot_detail_service,
            telemetry=telemetry or app.state.bot_runtime_telemetry,
            collaboration=(
                collaboration or app.state.thread_bot_collaboration_service
            ),
            approvals=approvals or app.state.approval_service,
            publish_event=publish_event or host.hub.publish,
            workflow_claim_findings=(
                workflow_claim_findings
                or host._workflow_outbound_claim_findings
            ),
            workflow_correction=(
                workflow_correction
                or host._canonical_workflow_correction
            ),
            slack_client=slack_client,
            telegram_client=telegram_client,
            secret_broker=getattr(app.state, "secret_broker", None),
        )
    app.state.bot_delivery_service = service

    # Transitional aliases for direct imports until legacy_core is deleted.
    host._send_bot_outbound = service.send_outbound
    host._send_bot_details = service.send_details
    host._record_bot_outbound = service.record_outbound
    host._record_bot_approval_request = service.record_approval_request
    host._update_slack_approval_messages = service.update_approval_messages
    host._resolve_approval_request = service.resolve_approval_request
    host._handle_slack_interaction = service.handle_slack_interaction
    return service
