from __future__ import annotations

import contextlib
import os
import time
from typing import Any

from codex_web.integrations.slack_client import SlackClient
from codex_web.identity import AuthenticationActor
from codex_web.models import BotBindingCreate, BotConnectionCreate, BotInboundMessage
from codex_web.services.bot_details import install_bot_detail_service
from codex_web.services.bot_routing import BotRoutingService


class BotService:
    """Bot management operations separated from the legacy runtime routes."""

    def __init__(
        self,
        host: Any,
        *,
        slack_client: SlackClient | None = None,
        routing_service: BotRoutingService | None = None,
    ) -> None:
        self.host = host
        self.slack_client = slack_client or SlackClient()
        self.routing_service = routing_service
        # The real compatibility host exposes its FastAPI app and is composed
        # after auxiliary persistence. Lightweight service test hosts need not
        # emulate the whole application just to exercise channel discovery.
        app = getattr(host, "app", None)
        self.detail_service = install_bot_detail_service(app, host) if getattr(app, "state", None) is not None else None

    def status(self) -> dict[str, Any]:
        bindings = self.host._load_bot_bindings()
        connections = self.host._load_bot_connections()
        gitlab_settings = self.host._load_gitlab_routing_settings()
        gitlab_enabled = gitlab_settings.enabled and any(
            project.enabled and project.project_paths
            for project in gitlab_settings.projects.values()
        )
        return {
            "providers": {
                "slack": {
                    "enabled": True,
                    "signatureVerification": bool(
                        os.environ.get("SLACK_SIGNING_SECRET")
                        or any(
                            connection.signing_secret
                            for connection in connections
                            if connection.provider == "slack"
                        )
                    ),
                    "eventsPath": "/bots/slack/events",
                },
                "telegram": {
                    "enabled": True,
                    "secretVerification": bool(
                        os.environ.get("TELEGRAM_WEBHOOK_SECRET")
                        or any(
                            connection.webhook_secret
                            for connection in connections
                            if connection.provider == "telegram"
                        )
                    ),
                    "webhookPath": "/bots/telegram/webhook",
                },
                "gitlab": {
                    "enabled": gitlab_enabled,
                    "tokenVerification": bool(
                        os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
                        or os.environ.get("GITLAB_WEBHOOK_SECRET")
                    ),
                    "webhookPath": "/bots/gitlab/events",
                },
            },
            "connections": len(connections),
            "bindings": len(bindings),
            "runtimeConnections": len(self.host.bot_runtime.tasks),
            "runtimeStatus": list(self.host.BOT_RUNTIME_STATUS.values()),
        }

    def list_connections(self) -> list[dict[str, Any]]:
        return [
            self.host._bot_connection_public(connection)
            for connection in self.host._load_bot_connections()
        ]

    async def save_connection(
        self,
        payload: BotConnectionCreate,
        actor: AuthenticationActor | None = None,
    ) -> dict[str, Any]:
        connection = self.host._upsert_bot_connection(payload, actor=actor)
        await self.host.bot_runtime.sync()
        return self.host._bot_connection_public(connection)

    def list_bindings(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for binding in self.host._load_bot_bindings():
            item = binding.model_dump()
            if binding.provider == "slack":
                item["slack_icon"] = self.host._slack_reply_icon(binding)
                item["slack_username"] = self.host._slack_reply_username(binding)
            results.append(item)
        return results

    async def list_channels(self, project_id: str) -> list[dict[str, str]]:
        self.host._project(project_id)
        cached = self.host.BOT_CHANNEL_CACHE.get(project_id)
        if cached and time.time() - cached[0] < 300:
            return cached[1]

        channels = {
            (item["provider"], item["id"]): item
            for item in self.host._known_bot_channels(project_id)
        }
        for connection in self.host._load_bot_connections():
            if connection.project_id != project_id or connection.provider != "slack" or not connection.bot_token:
                continue
            with contextlib.suppress(Exception):
                for channel in await self.slack_client.list_channels(connection.bot_token):
                    channels[(channel["provider"], channel["id"])] = channel

            unresolved = [
                channel
                for channel in channels.values()
                if channel["provider"] == "slack" and self.host._channel_needs_name(channel)
            ]
            for channel in unresolved:
                with contextlib.suppress(Exception):
                    resolved = await self.slack_client.channel_info(connection.bot_token, channel["id"])
                    if resolved:
                        channels[(resolved["provider"], resolved["id"])] = resolved

        result = sorted(channels.values(), key=lambda item: (item["provider"], item["label"]))
        self.host.BOT_CHANNEL_CACHE[project_id] = (time.time(), result)
        return result

    async def create_binding(self, payload: BotBindingCreate) -> dict[str, Any]:
        binding = await self.host._start_bot_thread(payload)
        await self.host.bot_runtime.sync()
        return binding.model_dump()

    async def inbound(self, payload: BotInboundMessage) -> dict[str, Any]:
        if self.routing_service is not None:
            return await self.routing_service.handle_inbound(payload)
        return await self.host._handle_bot_inbound(payload)
