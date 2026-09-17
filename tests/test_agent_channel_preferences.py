from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import BotBinding
from codex_web.services.agent_channel_preferences import (
    AgentChannelPreferenceService,
    install_agent_channel_preference_service,
)


class Host:
    def __init__(self) -> None:
        self.bindings: list[BotBinding] = []
        self.settings = SimpleNamespace(projects={})
        self.channels: list[dict[str, str]] = []
        self.clones: list[tuple[str, str, str | None]] = []

    def _load_bot_bindings(self) -> list[BotBinding]:
        return [binding.model_copy(deep=True) for binding in self.bindings]

    def _load_agent_channel_presence_settings(self):
        return self.settings

    @staticmethod
    def _normalize_string_list(values):
        result = []
        for value in values or []:
            normalized = str(value).strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _binding_prefix(binding: BotBinding) -> str | None:
        return binding.route_prefix

    def _bot_channels(self, project_id: str) -> list[dict[str, str]]:
        return list(self.channels)

    def _clone_binding_to_conversation(
        self,
        source: BotBinding,
        channel_id: str,
        *,
        external_name: str | None = None,
        **_: object,
    ) -> BotBinding:
        self.clones.append((source.id, channel_id, external_name))
        cloned = source.model_copy(deep=True)
        cloned.id = f"{source.id}-{channel_id}"
        cloned.external_conversation_id = channel_id
        cloned.external_name = external_name
        return cloned

    @staticmethod
    def _same_logical_binding(left: BotBinding, right: BotBinding) -> bool:
        return (
            left.project_id == right.project_id
            and (left.route_prefix or "").lower() == (right.route_prefix or "").lower()
        )


def binding(
    id: str,
    *,
    conversation: str,
    thread: str = "t1",
    project: str = "home",
    prefix: str = "james",
    name: str = "James",
    master: bool = False,
    updated: float = 1,
) -> BotBinding:
    return BotBinding(
        id=id,
        provider="slack",
        external_conversation_id=conversation,
        thread_id=thread,
        project_id=project,
        thread_name=name,
        route_prefix=prefix,
        is_master=master,
        created_at=1,
        updated_at=updated,
    )


class AgentChannelPreferenceServiceTests(unittest.TestCase):
    def test_parse_overrides_supports_json_and_legacy_pairs(self) -> None:
        with patch.dict(os.environ, {"CODEX_WEB_AGENT_CHANNELS": '{"James":["C1","C2"],"Dana":"C3"}'}, clear=False):
            self.assertEqual(
                AgentChannelPreferenceService.parse_overrides(),
                {"james": ["C1", "C2"], "dana": ["C3"]},
            )
        with patch.dict(os.environ, {"CODEX_WEB_AGENT_CHANNELS": "James:C1,Dana:C2"}, clear=False):
            self.assertEqual(
                AgentChannelPreferenceService.parse_overrides(),
                {"james": ["C1"], "dana": ["C2"]},
            )

    def test_persisted_preferences_win_and_allowed_channels_filter(self) -> None:
        host = Host()
        host.settings.projects["home"] = SimpleNamespace(
            agent_channels={"james": [" C2 ", "C1", "C2"]}
        )
        service = AgentChannelPreferenceService(host)

        with patch.dict(os.environ, {"CODEX_WEB_AGENT_CHANNELS": "James:C9"}, clear=False):
            self.assertEqual(service.conversations("James", "home"), ["C2", "C1"])
            self.assertEqual(
                service.conversations("James", "home", allowed_channels=["C1"]),
                ["C1"],
            )
            self.assertEqual(service.conversation("James", "home"), "C2")

    def test_env_override_is_fallback_when_project_has_no_preference(self) -> None:
        host = Host()
        service = AgentChannelPreferenceService(host)
        with patch.dict(os.environ, {"CODEX_WEB_AGENT_CHANNELS": "James:C9"}, clear=False):
            self.assertEqual(service.conversations("james", "home"), ["C9"])

    def test_binding_for_agent_prefers_existing_channel_then_latest_candidate(self) -> None:
        host = Host()
        host.bindings = [
            binding("old", conversation="C1", updated=1),
            binding("new", conversation="C2", updated=5),
        ]
        service = AgentChannelPreferenceService(host)

        self.assertEqual(
            service.binding_for_agent("james", "home", preferred_conversation_id="C1").id,
            "old",
        )
        self.assertEqual(service.binding_for_agent("james", "home").id, "new")

    def test_binding_for_agent_clones_only_to_allowed_preferred_channel(self) -> None:
        host = Host()
        host.bindings = [binding("source", conversation="C1", updated=5)]
        host.settings.projects["home"] = SimpleNamespace(agent_channels={"james": ["C1"]})
        service = AgentChannelPreferenceService(host)

        selected = service.binding_for_agent(
            "james",
            "home",
            preferred_conversation_id="C9",
        )
        self.assertEqual(selected.id, "source")
        self.assertEqual(host.clones, [])

        host.bindings.append(binding("known", conversation="C9", updated=1))
        selected = service.binding_for_agent(
            "james",
            "home",
            preferred_conversation_id="C9",
        )
        self.assertEqual(selected.id, "known")

    def test_clone_to_known_channel_uses_channel_name(self) -> None:
        host = Host()
        source = binding("source", conversation="C1")
        host.channels = [{"provider": "slack", "id": "C2", "name": "engineering", "label": "Engineering"}]
        service = AgentChannelPreferenceService(host)

        cloned = service.clone_to_known_channel(source, "C2")

        self.assertEqual(cloned.external_conversation_id, "C2")
        self.assertEqual(host.clones, [("source", "C2", "engineering")])

    def test_logical_bindings_include_same_thread_or_same_logical_route(self) -> None:
        host = Host()
        source = binding("source", conversation="C1", thread="t1", updated=2)
        host.bindings = [
            source,
            binding("same-thread", conversation="C2", thread="t1", prefix="other", updated=3),
            binding("same-route", conversation="C3", thread="t2", prefix="james", updated=4),
            binding("other", conversation="C4", thread="t3", prefix="dana", updated=5),
        ]
        service = AgentChannelPreferenceService(host)

        self.assertEqual(
            [item.id for item in service.logical_bindings(source)],
            ["same-route", "same-thread", "source"],
        )

    def test_installer_rebinds_historical_surface(self) -> None:
        host = Host()
        app = SimpleNamespace(state=SimpleNamespace())

        service = install_agent_channel_preference_service(app, host)

        self.assertIs(app.state.agent_channel_preference_service, service)
        self.assertIs(host._binding_for_agent.__self__, service)
        self.assertIs(host._preferred_agent_conversations.__self__, service)
        self.assertIs(host._logical_bindings_for_binding.__self__, service)


if __name__ == "__main__":
    unittest.main()
