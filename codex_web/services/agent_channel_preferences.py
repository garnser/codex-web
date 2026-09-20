from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from typing import Any

from codex_web.models import AgentChannelPresenceSettings, BotBinding


class AgentChannelPreferenceService:
    """Own agent channel preferences over explicit binding/configuration owners."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        load_settings: Callable[[], AgentChannelPresenceSettings] | None = None,
        normalize_strings: Callable[[Any], list[str]] | None = None,
        known_channels: Callable[[str], list[dict[str, str]]] | None = None,
        clone_binding: Callable[..., BotBinding] | None = None,
        load_bindings: Callable[[], list[BotBinding]] | None = None,
        binding_prefix: Callable[[BotBinding], str] | None = None,
        same_logical_binding: Callable[[BotBinding, BotBinding], bool] | None = None,
    ) -> None:
        if host is not None:
            load_settings = load_settings or getattr(
                host,
                "_load_agent_channel_presence_settings",
                None,
            )
            normalize_strings = normalize_strings or getattr(
                host,
                "_normalize_string_list",
                None,
            )
            known_channels = known_channels or getattr(
                host,
                "_bot_channels",
                None,
            )
            clone_binding = clone_binding or getattr(
                host,
                "_clone_binding_to_conversation",
                None,
            )
            load_bindings = load_bindings or getattr(
                host,
                "_load_bot_bindings",
                None,
            )
            binding_prefix = binding_prefix or getattr(
                host,
                "_binding_prefix",
                None,
            )
            same_logical_binding = same_logical_binding or getattr(
                host,
                "_same_logical_binding",
                None,
            )
        required = (
            load_settings,
            normalize_strings,
            known_channels,
            clone_binding,
            load_bindings,
            binding_prefix,
            same_logical_binding,
        )
        if not all(required):
            raise TypeError(
                "AgentChannelPreferenceService requires explicit dependencies"
            )
        self.load_settings = load_settings
        self.normalize_strings = normalize_strings
        self.known_channels = known_channels
        self.clone_binding = clone_binding
        self.load_bindings = load_bindings
        self.binding_prefix = binding_prefix
        self.same_logical_binding = same_logical_binding

    @staticmethod
    def parse_overrides() -> dict[str, list[str]]:
        raw = os.environ.get("CODEX_WEB_AGENT_CHANNELS", "").strip()
        if not raw:
            return {}
        with contextlib.suppress(Exception):
            payload = json.loads(raw)
            if isinstance(payload, dict):
                result: dict[str, list[str]] = {}
                for key, value in payload.items():
                    normalized_key = str(key).lower()
                    if isinstance(value, str) and value.strip():
                        result[normalized_key] = [value.strip()]
                    elif isinstance(value, list):
                        channels = [
                            str(channel).strip()
                            for channel in value
                            if str(channel).strip()
                        ]
                        if channels:
                            result[normalized_key] = channels
                return result
        result: dict[str, list[str]] = {}
        for part in raw.split(","):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                result[key] = [value]
        return result

    def conversations(
        self,
        agent: str,
        project_id: str,
        allowed_channels: list[str] | None = None,
    ) -> list[str]:
        normalized = agent.lower()
        settings = self.load_settings()
        project_settings = settings.projects.get(project_id)
        allowed = (
            set(self.normalize_strings(allowed_channels))
            if allowed_channels
            else None
        )
        if (
            project_settings
            and normalized in project_settings.agent_channels
        ):
            channels = self.normalize_strings(
                project_settings.agent_channels[normalized]
            )
            if allowed is not None:
                channels = [
                    channel
                    for channel in channels
                    if channel in allowed
                ]
            if channels:
                return channels
        overrides = self.parse_overrides()
        if normalized in overrides:
            channels = self.normalize_strings(overrides[normalized])
            if allowed is not None:
                channels = [
                    channel
                    for channel in channels
                    if channel in allowed
                ]
            if channels:
                return channels
        return []

    def conversation(
        self,
        agent: str,
        project_id: str,
    ) -> str | None:
        channels = self.conversations(agent.lower(), project_id)
        return channels[0] if channels else None

    def clone_to_known_channel(
        self,
        source: BotBinding,
        channel_id: str,
    ) -> BotBinding:
        channel = next(
            (
                item
                for item in self.known_channels(source.project_id)
                if item.get("provider") == source.provider
                and item.get("id") == channel_id
            ),
            None,
        )
        return self.clone_binding(
            source,
            channel_id,
            external_name=(
                (channel or {}).get("name")
                or (channel or {}).get("label")
                or channel_id
            ),
        )

    def binding_for_agent(
        self,
        agent: str,
        project_id: str,
        *,
        preferred_conversation_id: str | None = None,
    ) -> BotBinding | None:
        normalized = agent.strip().lower()
        if not normalized:
            return None
        all_bindings = self.load_bindings()
        candidates = sorted(
            [
                binding
                for binding in all_bindings
                if binding.project_id == project_id
                and not binding.is_master
                and (self.binding_prefix(binding) or "")
                .strip()
                .lower()
                == normalized
            ],
            key=lambda binding: binding.updated_at,
            reverse=True,
        )
        if not candidates:
            candidates = sorted(
                [
                    binding
                    for binding in all_bindings
                    if binding.project_id == project_id
                    and not binding.is_master
                    and normalized
                    in {
                        (self.binding_prefix(binding) or "")
                        .strip()
                        .lower()
                        .split(" ", 1)[0],
                        (binding.thread_name or "")
                        .strip()
                        .lower()
                        .split(" ", 1)[0],
                    }
                ],
                key=lambda binding: binding.updated_at,
                reverse=True,
            )
        if not candidates:
            return None

        if preferred_conversation_id:
            preferred = [
                binding
                for binding in candidates
                if binding.external_conversation_id
                == preferred_conversation_id
            ]
            if preferred:
                return preferred[0]
            source = candidates[0]
            allowed_channels = [
                binding.external_conversation_id
                for binding in candidates
            ]
            if preferred_conversation_id in self.conversations(
                normalized,
                project_id,
                allowed_channels=allowed_channels,
            ):
                return self.clone_binding(
                    source,
                    preferred_conversation_id,
                )

        preferred_conversation = self.conversation(
            normalized,
            project_id,
        )
        if preferred_conversation:
            preferred = [
                binding
                for binding in candidates
                if binding.external_conversation_id
                == preferred_conversation
            ]
            if preferred:
                return preferred[0]
            return self.clone_binding(
                candidates[0],
                preferred_conversation,
            )
        return candidates[0]

    def logical_bindings(
        self,
        source: BotBinding,
    ) -> list[BotBinding]:
        return sorted(
            [
                binding
                for binding in self.load_bindings()
                if binding.thread_id == source.thread_id
                or self.same_logical_binding(binding, source)
            ],
            key=lambda binding: binding.updated_at,
            reverse=True,
        )


def install_agent_channel_preference_service(
    app: Any,
    host: Any,
    **dependencies: Any,
) -> AgentChannelPreferenceService:
    existing = getattr(
        app.state,
        "agent_channel_preference_service",
        None,
    )
    if isinstance(existing, AgentChannelPreferenceService):
        service = existing
    else:
        service = AgentChannelPreferenceService(
            host if not dependencies else None,
            **dependencies,
        )
        app.state.agent_channel_preference_service = service

    # Output-only compatibility aliases.
    host._parse_agent_channel_overrides = service.parse_overrides
    host._preferred_agent_conversations = service.conversations
    host._preferred_agent_conversation = service.conversation
    host._clone_binding_to_known_channel = (
        service.clone_to_known_channel
    )
    host._binding_for_agent = service.binding_for_agent
    host._logical_bindings_for_binding = service.logical_bindings
    return service
