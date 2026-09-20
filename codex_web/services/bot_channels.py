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

    def _known_from(
        self,
        project_id: str,
        connections: list[BotConnection],
        bindings: list[Any],
    ) -> list[dict[str, str]]:
        channels: dict[tuple[str, str], dict[str, str]] = {}
        for connection in connections:
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
        for binding in bindings:
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

    def known(self, project_id: str) -> list[dict[str, str]]:
        self.projects.get(project_id)
        return self._known_from(
            project_id,
            self.connections.load_connections(),
            self.bindings.load_bindings(),
        )

    async def list(self, project_id: str) -> list[dict[str, str]]:
        self.projects.get(project_id)
        started = time.perf_counter()
        now = time.time()
        cached = self.cache.get(project_id)
        if cached and now - cached[0] < self.CACHE_SECONDS:
            result = [dict(item) for item in cached[1]]
            self.discovery_metrics[project_id] = {
                "cacheHit": True,
                "credentialGroups": 0,
                "providerCalls": 0,
                "listCalls": 0,
                "metadataCandidateCount": 0,
                "unresolvedCount": sum(
                    1
                    for item in result
                    if item.get("provider") == "slack"
                    and self.channel_needs_name(item)
                ),
                "metadataLookupCount": 0,
                "negativeCacheHits": 0,
                "cooldownSkips": 0,
                "rateLimitEvents": 0,
                "providerFailures": 0,
                "discoveryDurationSeconds": (
                    time.perf_counter() - started
                ),
            }
            return result

        connections = self.connections.load_connections()
        bindings = self.bindings.load_bindings()
        channels = {
            (item["provider"], item["id"]): item
            for item in self._known_from(
                project_id,
                connections,
                bindings,
            )
        }

        groups: dict[str, BotConnection] = {}
        connection_groups: dict[str, str] = {}
        for connection in connections:
            if (
                connection.project_id != project_id
                or connection.provider != "slack"
            ):
                continue
            group_key = self._credential_group_key(connection)
            if group_key is None:
                continue
            groups.setdefault(group_key, connection)
            connection_groups[connection.id] = group_key

        channel_groups: dict[str, set[str]] = {}
        for connection in connections:
            if (
                connection.project_id == project_id
                and connection.provider == "slack"
                and connection.default_external_conversation_id
            ):
                group_key = connection_groups.get(connection.id)
                if group_key:
                    channel_groups.setdefault(
                        connection.default_external_conversation_id,
                        set(),
                    ).add(group_key)

        all_group_keys = set(groups)
        for binding in bindings:
            if binding.project_id != project_id or binding.provider != "slack":
                continue
            group_key = connection_groups.get(binding.connection_id or "")
            target_groups = {group_key} if group_key else all_group_keys
            channel_groups.setdefault(
                binding.external_conversation_id,
                set(),
            ).update(target_groups)

        metrics: dict[str, Any] = {
            "cacheHit": False,
            "credentialGroups": len(groups),
            "providerCalls": 0,
            "listCalls": 0,
            "metadataCandidateCount": 0,
            "unresolvedCount": 0,
            "metadataLookupCount": 0,
            "negativeCacheHits": 0,
            "cooldownSkips": 0,
            "rateLimitEvents": 0,
            "providerFailures": 0,
            "discoveryDurationSeconds": 0.0,
        }
        semaphore = asyncio.Semaphore(self.MAX_METADATA_CONCURRENCY)

        def apply_rate_limit(group_key: str, exc: Exception) -> None:
            retry_after = self._retry_after_seconds(exc)
            if retry_after is None:
                return
            metrics["rateLimitEvents"] += 1
            self.cooldowns[group_key] = time.time() + max(
                retry_after,
                self.DEFAULT_RATE_LIMIT_SECONDS,
            )

        async def discover_group(
            group_key: str,
            connection: BotConnection,
        ) -> None:
            if self.cooldowns.get(group_key, 0.0) > time.time():
                metrics["cooldownSkips"] += 1
                return

            async def list_with_token(token: str):
                return await self.slack.list_channels(token)

            try:
                async with semaphore:
                    metrics["providerCalls"] += 1
                    metrics["listCalls"] += 1
                    discovered = await self._with_credential(
                        connection,
                        "bot_token",
                        "slack.list_channels",
                        list_with_token,
                    )
            except Exception as exc:
                metrics["providerFailures"] += 1
                apply_rate_limit(group_key, exc)
                return

            for channel in discovered:
                channel_id = str(channel.get("id") or "").strip()
                if not channel_id:
                    continue
                channels[("slack", channel_id)] = channel
                channel_groups.setdefault(channel_id, set()).add(
                    group_key
                )

        await asyncio.gather(
            *(
                discover_group(group_key, connection)
                for group_key, connection in sorted(groups.items())
            )
        )

        unresolved = {
            str(channel["id"]): channel
            for channel in channels.values()
            if channel.get("provider") == "slack"
            and channel.get("id")
            and self.channel_needs_name(channel)
        }
        metrics["metadataCandidateCount"] = len(unresolved)

        async def resolve_channel(
            channel_id: str,
            group_keys: tuple[str, ...],
        ) -> None:
            for group_key in group_keys:
                connection = groups.get(group_key)
                if connection is None:
                    continue
                negative_key = (group_key, channel_id)
                if self.negative_cache.get(negative_key, 0.0) > time.time():
                    metrics["negativeCacheHits"] += 1
                    continue
                if self.cooldowns.get(group_key, 0.0) > time.time():
                    metrics["cooldownSkips"] += 1
                    continue

                async def info_with_token(token: str):
                    return await self.slack.channel_info(
                        token,
                        channel_id,
                    )

                try:
                    async with semaphore:
                        metrics["providerCalls"] += 1
                        metrics["metadataLookupCount"] += 1
                        resolved = await self._with_credential(
                            connection,
                            "bot_token",
                            "slack.channel_info",
                            info_with_token,
                        )
                except Exception as exc:
                    metrics["providerFailures"] += 1
                    apply_rate_limit(group_key, exc)
                    self.negative_cache[negative_key] = (
                        time.time() + self.NEGATIVE_CACHE_SECONDS
                    )
                    continue

                if resolved:
                    channels[("slack", channel_id)] = resolved
                    self.negative_cache.pop(negative_key, None)
                    return
                self.negative_cache[negative_key] = (
                    time.time() + self.NEGATIVE_CACHE_SECONDS
                )

        await asyncio.gather(
            *(
                resolve_channel(
                    channel_id,
                    tuple(
                        sorted(
                            channel_groups.get(channel_id)
                            or all_group_keys
                        )
                    ),
                )
                for channel_id in sorted(unresolved)
            )
        )

        metrics["unresolvedCount"] = sum(
            1
            for channel in channels.values()
            if channel.get("provider") == "slack"
            and self.channel_needs_name(channel)
        )

        result = sorted(
            channels.values(),
            key=lambda item: (item["provider"], item["label"]),
        )
        self.cache[project_id] = (
            time.time(),
            [dict(item) for item in result],
        )
        metrics["discoveryDurationSeconds"] = (
            time.perf_counter() - started
        )
        self.discovery_metrics[project_id] = metrics

        expiry = time.time()
        self.negative_cache = {
            key: value
            for key, value in self.negative_cache.items()
            if value > expiry
        }
        self.cooldowns = {
            key: value
            for key, value in self.cooldowns.items()
            if value > expiry
        }
        return result

    def invalidate(self, project_id: str | None = None) -> None:
        if project_id is None:
            self.cache.clear()
            self.discovery_metrics.clear()
        else:
            self.cache.pop(project_id, None)
            self.discovery_metrics.pop(project_id, None)
        # Explicit configuration invalidation must not leave stale provider
        # failures suppressing newly valid discovery attempts.
        self.negative_cache.clear()
        self.cooldowns.clear()


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
