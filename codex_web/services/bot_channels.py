from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from typing import Any

from codex_web.integrations.slack_client import SlackClient
from codex_web.models import BotConnection
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.secrets import SecretBroker


class BotChannelDiscoveryService:
    """Own known/discovered conversation channels and their bounded cache."""

    CACHE_SECONDS = 300.0
    NEGATIVE_CACHE_SECONDS = 60.0
    MAX_METADATA_CONCURRENCY = 8
    DEFAULT_RATE_LIMIT_SECONDS = 30.0

    def __init__(
        self,
        *,
        connections: BotConnectionService,
        bindings: BotBindingSelectionService,
        projects: ProjectRuntimeService,
        slack_client: SlackClient,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.connections = connections
        self.bindings = bindings
        self.projects = projects
        self.slack = slack_client
        self.secret_broker = secret_broker
        self.cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self.negative_cache: dict[tuple[str, str], float] = {}
        self.cooldowns: dict[str, float] = {}
        self.discovery_metrics: dict[str, dict[str, Any]] = {}

    @staticmethod
    def channel_label(channel_id: str, name: str | None = None) -> str:
        normalized = (name or "").strip()
        if not normalized:
            return channel_id
        return normalized if normalized.startswith("#") else f"#{normalized}"

    @staticmethod
    def channel_needs_name(channel: dict[str, str]) -> bool:
        channel_id = channel.get("id") or ""
        name = channel.get("name") or ""
        label = channel.get("label") or ""
        return (
            not name
            or name == channel_id
            or label in {channel_id, f"#{channel_id}"}
        )

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

    @classmethod
    def _credential_group_key(
        cls,
        connection: BotConnection,
    ) -> str | None:
        identity = cls._credential_identity(connection, "bot_token")
        if not identity:
            return None
        return hashlib.sha256(str(identity).encode()).hexdigest()

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        response = getattr(exc, "response", None)
        if response is None or getattr(response, "status_code", None) != 429:
            return None
        headers = getattr(response, "headers", {})
        value = headers.get("Retry-After") if headers is not None else None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id is not None:
            return dict(self.discovery_metrics.get(project_id, {}))
        return {
            key: dict(value)
            for key, value in self.discovery_metrics.items()
        }

    async def _with_credential(
        self,
        connection: BotConnection,
        field: str,
        operation: str,
        consumer,
    ):
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

    def known(self, project_id: str) -> list[dict[str, str]]:
        self.projects.get(project_id)
        channels: dict[tuple[str, str], dict[str, str]] = {}
        for connection in self.connections.load_connections():
            if (
                connection.project_id != project_id
                or not connection.default_external_conversation_id
            ):
                continue
            key = (
                connection.provider,
                connection.default_external_conversation_id,
            )
            channels[key] = {
                "provider": connection.provider,
                "id": connection.default_external_conversation_id,
                "name": connection.default_external_name or "",
                "label": self.channel_label(
                    connection.default_external_conversation_id,
                    connection.default_external_name,
                ),
            }
        for binding in self.bindings.load_bindings():
            if binding.project_id != project_id:
                continue
            key = (binding.provider, binding.external_conversation_id)
            channels.setdefault(
                key,
                {
                    "provider": binding.provider,
                    "id": binding.external_conversation_id,
                    "name": binding.external_name or "",
                    "label": self.channel_label(
                        binding.external_conversation_id,
                        binding.external_name,
                    ),
                },
            )
        return sorted(
            channels.values(),
            key=lambda item: (item["provider"], item["label"]),
        )

    async def list(self, project_id: str) -> list[dict[str, str]]:
        self.projects.get(project_id)
        cached = self.cache.get(project_id)
        if cached and time.time() - cached[0] < self.CACHE_SECONDS:
            return cached[1]

        channels = {
            (item["provider"], item["id"]): item
            for item in self.known(project_id)
        }
        for connection in self.connections.load_connections():
            if (
                connection.project_id != project_id
                or connection.provider != "slack"
                or not self._credential_identity(connection, "bot_token")
            ):
                continue
            with contextlib.suppress(Exception):
                async def list_with_token(token: str):
                    return await self.slack.list_channels(token)

                discovered = await self._with_credential(
                    connection,
                    "bot_token",
                    "slack.list_channels",
                    list_with_token,
                )
                for channel in discovered:
                    channels[(channel["provider"], channel["id"])] = channel

            unresolved = [
                channel
                for channel in channels.values()
                if channel["provider"] == "slack"
                and self.channel_needs_name(channel)
            ]
            for channel in unresolved:
                with contextlib.suppress(Exception):
                    async def channel_info(token: str):
                        return await self.slack.channel_info(
                            token,
                            channel["id"],
                        )

                    resolved = await self._with_credential(
                        connection,
                        "bot_token",
                        "slack.channel_info",
                        channel_info,
                    )
                    if resolved:
                        channels[(resolved["provider"], resolved["id"])] = (
                            resolved
                        )

        result = sorted(
            channels.values(),
            key=lambda item: (item["provider"], item["label"]),
        )
        self.cache[project_id] = (time.time(), result)
        return result

    def invalidate(self, project_id: str | None = None) -> None:
        if project_id is None:
            self.cache.clear()
        else:
            self.cache.pop(project_id, None)


def install_bot_channel_discovery_service(
    app,
    host,
    *,
    connections=None,
    bindings=None,
    projects=None,
    slack_client=None,
    secret_broker=None,
) -> BotChannelDiscoveryService:
    service = BotChannelDiscoveryService(
        connections=connections or app.state.bot_connection_service,
        bindings=bindings or app.state.bot_binding_selection_service,
        projects=projects or app.state.project_runtime_service,
        slack_client=slack_client or app.state.slack_client,
        secret_broker=secret_broker or getattr(
            app.state,
            "secret_broker",
            None,
        ),
    )
    app.state.bot_channel_discovery_service = service

    # Transitional aliases for compatibility consumers only.
    host._channel_label = service.channel_label
    host._channel_needs_name = service.channel_needs_name
    host._known_bot_channels = service.known
    host._bot_channels = service.list
    host.BOT_CHANNEL_CACHE = service.cache
    return service
