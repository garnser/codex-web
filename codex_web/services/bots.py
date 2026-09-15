from __future__ import annotations

import asyncio
import os
from typing import Any

from codex_web.models import BotBindingCreate, BotConnectionCreate, BotInboundMessage


class BotService:
    """Bot management operations separated from the legacy runtime routes.

    Provider discovery remains implemented by the compatibility runtime for now,
    but potentially blocking synchronous provider calls are always executed in a
    worker thread instead of the FastAPI event loop.
    """

    def __init__(self, host: Any) -> None:
        self.host = host

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

    async def save_connection(self, payload: BotConnectionCreate) -> dict[str, Any]:
        connection = self.host._upsert_bot_connection(payload)
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
        # Slack conversations.list is synchronous in the compatibility provider
        # layer and can paginate repeatedly. Never execute it on the event loop.
        return await asyncio.to_thread(self.host._bot_channels, project_id)

    async def create_binding(self, payload: BotBindingCreate) -> dict[str, Any]:
        binding = await self.host._start_bot_thread(payload)
        await self.host.bot_runtime.sync()
        return binding.model_dump()

    async def inbound(self, payload: BotInboundMessage) -> dict[str, Any]:
        return await self.host._handle_bot_inbound(payload)
