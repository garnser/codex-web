from __future__ import annotations

import unittest

from pydantic import ValidationError

from codex_web.models import (
    AgentChannelPresenceProjectSettings,
    AgentChannelPresenceSettings,
    BotConnectionCreate,
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    ProjectCreate,
    ThreadRunSettings,
    TurnCreate,
)


class ApiModelValidationTests(unittest.TestCase):
    def test_project_rejects_unknown_sandbox(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectCreate(name="demo", path="/tmp/demo", sandbox="host-write")

    def test_project_rejects_unknown_approval_policy(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectCreate(name="demo", path="/tmp/demo", approval_policy="sometimes")

    def test_turn_rejects_unknown_reasoning_effort(self) -> None:
        with self.assertRaises(ValidationError):
            TurnCreate(message="hello", reasoning_effort="extreme")

    def test_thread_settings_allow_empty_reasoning_effort_for_ui_clear(self) -> None:
        settings = ThreadRunSettings(reasoning_effort="")
        self.assertEqual(settings.reasoning_effort, "")

    def test_bot_connection_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValidationError):
            BotConnectionCreate(provider="discord", name="demo")

    def test_known_values_are_accepted(self) -> None:
        project = ProjectCreate(
            name="demo",
            path="/tmp/demo",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        bot = BotConnectionCreate(provider="slack", name="team bot")
        self.assertEqual(project.sandbox, "workspace-write")
        self.assertEqual(bot.provider, "slack")

    def test_fresh_integration_models_do_not_embed_deployment_topology(self) -> None:
        self.assertEqual(AgentChannelPresenceProjectSettings().agent_channels, {})
        self.assertEqual(AgentChannelPresenceSettings().projects, {})
        self.assertEqual(GitLabRoutingSettings().projects, {})

    def test_explicit_integration_topology_round_trips(self) -> None:
        presence = AgentChannelPresenceSettings(
            projects={
                "project-a": AgentChannelPresenceProjectSettings(
                    agent_channels={"agent-a": ["channel-a"]}
                )
            }
        )
        routing = GitLabRoutingSettings(
            projects={
                "project-a": GitLabProjectRoutingSettings(
                    project_paths=["example/team"],
                    channel_ids=["channel-a"],
                )
            }
        )

        self.assertEqual(presence.projects["project-a"].agent_channels["agent-a"], ["channel-a"])
        self.assertEqual(routing.projects["project-a"].project_paths, ["example/team"])


if __name__ == "__main__":
    unittest.main()
