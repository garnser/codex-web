from __future__ import annotations

import contextlib
from typing import Any

from codex_web.integrations.slack_client import SlackClient
from codex_web.integrations.telegram_client import TelegramClient
from codex_web.models import BotBinding, BotConnection
from codex_web.services.secrets import SecretBroker
from codex_web.services.bot_targets import install_bot_target_service


class BotDeliveryService:
    """Own outbound provider delivery and Slack approval projection."""

    def __init__(
        self,
        host: Any,
        *,
        slack_client: SlackClient | None = None,
        telegram_client: TelegramClient | None = None,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.host = host
        self.slack = slack_client or SlackClient()
        self.telegram = telegram_client or TelegramClient()
        self.secret_broker = secret_broker

    @staticmethod
    def _credential_identity(connection: BotConnection, field: str) -> str | None:
        return getattr(connection, f"{field}_secret_id", None) or getattr(connection, field, None)

    async def _with_credential(
        self,
        connection: BotConnection,
        field: str,
        operation: str,
        consumer: Any,
    ) -> Any:
        secret_id = getattr(connection, f"{field}_secret_id", None)
        if secret_id and self.secret_broker is not None:
            actor = self.host._bot_runtime_actor(connection.project_id)
            return await self.secret_broker.use_async(
                secret_id,
                actor=actor,
                operation=operation,
                consumer=consumer,
                context={"connection_id": connection.id, "provider": connection.provider},
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
        h = self.host
        connection = h._bot_connection(binding.connection_id) if binding.connection_id else None
        if not connection or not self._credential_identity(connection, "bot_token"):
            return {"sent": False, "reason": "missing_bot_token"}
        try:
            if binding.provider == "slack":
                target, should_thread = h._thread_target_for_outbound(binding, reply_in_thread)
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
                        username=username or h._slack_reply_username(binding),
                        icon_emoji=icon_emoji or h._slack_reply_icon(binding),
                        thread_ts=thread_ts,
                    )
                return await self._with_credential(
                    connection, "bot_token", "slack.post_message", send_slack
                )
            if binding.provider == "telegram":
                async def send_telegram(token: str):
                    return await self.telegram.send_message(
                        token,
                        binding.external_conversation_id,
                        text,
                    )
                return await self._with_credential(
                    connection, "bot_token", "telegram.send_message", send_telegram
                )
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}
        return {"sent": False, "reason": "unsupported_provider"}

    async def send_details(self, binding: BotBinding) -> dict[str, Any]:
        h = self.host
        detail = h._latest_bot_detail(binding.thread_id)
        if not detail:
            return await self.send_outbound(
                binding,
                "No command or file details are available for this thread yet.",
                reply_in_thread=True,
            )
        return await self.send_outbound(
            binding,
            h._format_bot_detail_response(detail),
            reply_in_thread=True,
        )

    async def record_outbound(self, message: dict[str, Any]) -> None:
        h = self.host
        if message.get("method") != "item/completed":
            return
        params = message.get("params") or {}
        item = params.get("item") or {}
        thread_id = params.get("threadId")
        if not thread_id:
            return
        detail = h._format_bot_detail_item(item)
        if detail:
            h._record_bot_detail(
                thread_id,
                item.get("type") or "detail",
                detail["title"],
                detail["text"],
            )
            return
        if item.get("type") != "agentMessage":
            return
        bindings = h._bindings_for_thread(thread_id)
        if not bindings:
            bindings = await h._project_scoped_bindings_for_thread(thread_id)
        for binding in h._outbound_bindings_for_thread(thread_id, bindings):
            route_prefix = h._binding_prefix(binding)
            report_name = h._binding_report_name(binding)
            outbound_text = h._format_bot_outbound_item(item, report_name)
            if not outbound_text:
                continue
            workflow_findings, mentioned_states = h._workflow_outbound_claim_findings(outbound_text)
            workflow_verification: dict[str, Any] | None = None
            if workflow_findings:
                workflow_verification = {
                    "corrected": True,
                    "findings": workflow_findings,
                    "refs": [state.ref for state in mentioned_states],
                    "original_text": h._truncate_text(outbound_text, 1000),
                }
                outbound_text = h._canonical_workflow_correction(
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
            h._remember_bot_delivery_target(binding, delivery)
            event["delivery"] = delivery
            h._append_bot_event(event)
            await h.hub.publish({"type": "bot.outbound", **event})

    async def record_approval_request(self, request: dict[str, Any]) -> None:
        h = self.host
        thread_id = h._approval_thread_id(request)
        if not thread_id:
            return
        for binding in h._outbound_bindings_for_thread(thread_id, h._bindings_for_thread(thread_id)):
            if binding.provider != "slack" or not binding.connection_id:
                continue
            connection = h._bot_connection(binding.connection_id)
            if not self._credential_identity(connection, "bot_token"):
                continue
            text = f"Approval requested for {h._binding_prefix(binding) or thread_id}"
            target = h._active_reply_target_for_binding(binding) or h._reply_target_for_binding(binding)
            thread_ts = (
                (target.external_thread_id or target.message_id)
                if (h._should_reply_in_external_thread(binding) and target)
                else None
            )
            async def send_approval(token: str):
                return await self.slack.post_message(
                    token,
                    binding.external_conversation_id,
                    text,
                    username=h._slack_reply_username(binding),
                    icon_emoji=h._slack_reply_icon(binding),
                    thread_ts=thread_ts,
                    blocks=h._approval_blocks(request, binding),
                )
            delivery = await self._with_credential(
                connection, "bot_token", "slack.approval_request", send_approval
            )
            response = delivery.get("providerResponse") or {}
            if delivery.get("sent") and response.get("ts"):
                h._remember_approval_message(
                    request.get("id"),
                    connection_id=connection.id,
                    channel=binding.external_conversation_id,
                    message_ts=str(response["ts"]),
                    context=h._binding_prefix(binding) or thread_id,
                    thread_id=thread_id,
                )
            h._append_bot_event(
                {
                    "type": "approval_request_sent",
                    "provider": "slack",
                    "external_conversation_id": binding.external_conversation_id,
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
        h = self.host
        messages = h._load_approval_messages().get(str(request_id), [])
        status = f"{actor} selected `{decision}`."
        for message in messages:
            with contextlib.suppress(Exception):
                connection = h._bot_connection(message.connection_id)
                if not self._credential_identity(connection, "bot_token"):
                    continue
                async def update_message(token: str):
                    return await self.slack.update_message(
                        token,
                        message.channel,
                        message.message_ts,
                        f"{actor} selected {decision} for approval request {request_id}.",
                        blocks=h._approval_resolved_blocks(request, message.context, status),
                    )
                await self._with_credential(
                    connection, "bot_token", "slack.approval_update", update_message
                )
        h._forget_approval_messages(request_id)

    async def resolve_approval_request(
        self,
        request_id: int | str,
        decision: str,
        *,
        actor: str,
    ) -> dict[str, bool]:
        h = self.host
        request = h._pending_codex_approvals().get(request_id)
        if not request:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Approval request not found")
        result = h._approval_result(request["method"], decision)
        await h._respond_codex_approval(request_id, result)
        await self.update_approval_messages(
            request_id,
            request,
            decision=decision,
            actor=actor,
        )
        h._append_bot_event(
            {
                "type": "approval_resolved",
                "request_id": request_id,
                "decision": decision,
                "actor": actor,
                "thread_id": h._approval_thread_id(request),
            }
        )
        return {"ok": True}

    async def handle_slack_interaction(
        self,
        connection: BotConnection,
        payload: dict[str, Any],
    ) -> None:
        h = self.host
        actions = payload.get("actions") or []
        for action in actions:
            if not str(action.get("action_id") or "").startswith("codex_approval_"):
                continue
            try:
                import json

                value = json.loads(action.get("value") or "{}")
            except json.JSONDecodeError:
                continue
            request_id = h._request_id_value(value.get("request_id"))
            decision = value.get("decision")
            request = h._pending_codex_approvals().get(request_id)
            channel = (payload.get("channel") or {}).get("id") or connection.default_external_conversation_id
            message_ts = h._slack_interaction_message_ts(payload)
            user = (
                (payload.get("user") or {}).get("username")
                or (payload.get("user") or {}).get("id")
                or "Slack"
            )
            context = h._slack_interaction_context(payload, str(request_id))
            if not request or not decision:
                if channel and message_ts and self._credential_identity(connection, "bot_token"):
                    async def update_expired(token: str):
                        return await self.slack.update_message(
                            token,
                            channel,
                            message_ts,
                            "That approval request is no longer pending.",
                            blocks=h._approval_resolved_blocks(None, context, "Already resolved."),
                        )
                    await self._with_credential(
                        connection, "bot_token", "slack.approval_expired", update_expired
                    )
                return
            await self.resolve_approval_request(request_id, decision, actor=user)
            h._append_bot_event(
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
    slack_client: SlackClient | None = None,
    telegram_client: TelegramClient | None = None,
) -> BotDeliveryService:
    # Delivery depends on canonical reply-target semantics. Install that owner
    # here so every composition path gets the same behavior before traffic can
    # reach provider delivery.
    install_bot_target_service(app, host)

    existing = getattr(app.state, "bot_delivery_service", None)
    if isinstance(existing, BotDeliveryService) and existing.host is host:
        service = existing
    else:
        service = BotDeliveryService(
            host,
            slack_client=slack_client,
            telegram_client=telegram_client,
            secret_broker=getattr(app.state, "secret_broker", None),
        )
        app.state.bot_delivery_service = service

    host._send_bot_outbound = service.send_outbound
    host._send_bot_details = service.send_details
    host._record_bot_outbound = service.record_outbound
    host._record_bot_approval_request = service.record_approval_request
    host._update_slack_approval_messages = service.update_approval_messages
    host._resolve_approval_request = service.resolve_approval_request
    host._handle_slack_interaction = service.handle_slack_interaction
    return service
