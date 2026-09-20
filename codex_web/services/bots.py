from __future__ import annotations

import os
from typing import Any, Callable

from codex_web.identity import AuthenticationActor
from codex_web.models import (
    BotBindingCreate,
    BotConnectionCreate,
    BotInboundMessage,
)
from codex_web.runtime.bots import BotRuntime
from codex_web.services.bot_binding_selection import (
    BotBindingSelectionService,
)
from codex_web.services.bot_bindings import BotBindingLifecycleService
from codex_web.services.bot_channels import BotChannelDiscoveryService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry


class BotService:
    """Bot management operations over explicit bot-domain owners."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        connections: BotConnectionService | None = None,
        bindings: BotBindingSelectionService | None = None,
        binding_lifecycle: BotBindingLifecycleService | None = None,
        channels: BotChannelDiscoveryService | None = None,
        presentation: BotPresentationService | None = None,
        telemetry: BotRuntimeTelemetry | None = None,
        runtime: BotRuntime | None = None,
        routing_service: BotRoutingService | None = None,
        load_gitlab_routing_settings: Callable[[], Any] | None = None,
        slack_client: Any | None = None,
    ) -> None:
        self.connections = connections
        self.bindings = bindings
        self.binding_lifecycle = binding_lifecycle
        self.channels = channels
        self.presentation = presentation
        self.telemetry = telemetry
        self.runtime = runtime
        self.routing_service = routing_service
        self.load_gitlab_routing_settings = load_gitlab_routing_settings

        # Compatibility for historical direct service consumers while the runtime facade is retained.
        # removed. Keep only narrow callables; never retain the mutable host.
        self._legacy_known_channels = None
        self._legacy_load_connections = None
        self._legacy_channel_needs_name = None
        self._legacy_slack = slack_client
        if host is not None and channels is None:
            self._legacy_known_channels = getattr(
                host,
                "_known_bot_channels",
                None,
            )
            self._legacy_load_connections = getattr(
                host,
                "_load_bot_connections",
                None,
            )
            self._legacy_channel_needs_name = getattr(
                host,
                "_channel_needs_name",
                None,
            )

    @staticmethod
    def _credential_identity(connection, field: str) -> str | None:
        return getattr(connection, f"{field}_secret_id", None) or getattr(
            connection,
            field,
            None,
        )

    def status(self) -> dict[str, Any]:
        bindings = self.bindings.load_bindings()
        connections = self.connections.load_connections()
        gitlab_settings = self.load_gitlab_routing_settings()
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
                            self._credential_identity(
                                connection,
                                "signing_secret",
                            )
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
                            self._credential_identity(
                                connection,
                                "webhook_secret",
                            )
                            for connection in connections
                            if connection.provider == "telegram"
                        )
                    ),
                    "webhookPath": "/bots/telegram/webhook",
                },
                "gitlab": {
                    "enabled": gitlab_enabled,
                    "tokenVerification": bool(
                        os.environ.get(
                            "CODEX_WEB_GITLAB_WEBHOOK_SECRET"
                        )
                        or os.environ.get("GITLAB_WEBHOOK_SECRET")
                    ),
                    "webhookPath": "/bots/gitlab/events",
                },
            },
            "connections": len(connections),
            "bindings": len(bindings),
            "runtimeConnections": len(self.runtime.tasks),
            "runtimeStatus": list(
                self.telemetry.snapshot().values()
            ),
            "channelDiscovery": (
                self.channels.status()
                if callable(getattr(self.channels, "status", None))
                else {}
            ),
        }

    def list_connections(self) -> list[dict[str, Any]]:
        return [
            self.connections.public(connection)
            for connection in self.connections.load_connections()
        ]

    async def save_connection(
        self,
        payload: BotConnectionCreate,
        actor: AuthenticationActor | None = None,
    ) -> dict[str, Any]:
        connection = self.connections.upsert(payload, actor=actor)
        self.channels.invalidate(connection.project_id)
        await self.runtime.sync()
        return self.connections.public(connection)

    def list_bindings(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for binding in self.bindings.load_bindings():
            item = binding.model_dump()
            if binding.provider == "slack":
                item["slack_icon"] = (
                    self.presentation.slack_reply_icon(binding)
                )
                item["slack_username"] = (
                    self.presentation.slack_reply_username(binding)
                )
            results.append(item)
        return results

    async def list_channels(
        self,
        project_id: str,
    ) -> list[dict[str, str]]:
        if self.channels is not None:
            return await self.channels.list(project_id)
        if self._legacy_known_channels is None:
            return []

        channels = {
            (item["provider"], item["id"]): item
            for item in self._legacy_known_channels(project_id)
        }
        load_connections = self._legacy_load_connections
        if callable(load_connections) and self._legacy_slack is not None:
            for connection in load_connections():
                if (
                    getattr(connection, "project_id", None) != project_id
                    or getattr(connection, "provider", None) != "slack"
                    or not getattr(connection, "bot_token", None)
                ):
                    continue
                discovered = await self._legacy_slack.list_channels(
                    connection.bot_token
                )
                for channel in discovered:
                    channels[(channel["provider"], channel["id"])] = channel

                needs_name = self._legacy_channel_needs_name
                if callable(needs_name):
                    for channel in list(channels.values()):
                        if (
                            channel.get("provider") == "slack"
                            and needs_name(channel)
                        ):
                            resolved = await self._legacy_slack.channel_info(
                                connection.bot_token,
                                channel["id"],
                            )
                            if resolved:
                                channels[
                                    (resolved["provider"], resolved["id"])
                                ] = resolved
        return sorted(
            channels.values(),
            key=lambda item: (item["provider"], item["label"]),
        )

    async def create_binding(
        self,
        payload: BotBindingCreate,
    ) -> dict[str, Any]:
        binding = await self.binding_lifecycle.start(payload)
        self.channels.invalidate(binding.project_id)
        await self.runtime.sync()
        return binding.model_dump()

    async def inbound(
        self,
        payload: BotInboundMessage,
    ) -> dict[str, Any]:
        return await self.routing_service.handle_inbound(payload)
