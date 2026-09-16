from __future__ import annotations

import unittest

from pydantic import ValidationError

from codex_web.models import (
    AgentChannelPresenceProjectSettings,
    AgentChannelPresenceSettings,
    ApprovalDecision,
    BotConnectionCreate,
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    ProjectCreate,
    ThreadRunSettings,
    TurnCreate,
    WorkItemHandoff,
    WorkItemProgressUpdate,
    WorkItemState,
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

    def test_approval_rejects_unknown_decision_instead_of_silent_decline(self) -> None:
        with self.assertRaises(ValidationError):
            ApprovalDecision(decision="approve-maybe")
        self.assertEqual(ApprovalDecision(decision="acceptForSession").decision, "acceptForSession")
        self.assertEqual(ApprovalDecision(decision="decline").decision, "decline")

    def test_work_item_requests_reject_unknown_stage_and_artifact_state(self) -> None:
        with self.assertRaises(ValidationError):
            WorkItemProgressUpdate(current_stage="almost_done")
        with self.assertRaises(ValidationError):
            WorkItemProgressUpdate(artifact_state="uploaded_somewhere")

        update = WorkItemProgressUpdate(
            current_stage="ready_for_validation",
            artifact_state="merge_request",
        )
        self.assertEqual(update.current_stage, "ready_for_validation")
        self.assertEqual(update.artifact_state, "merge_request")

    def test_persisted_work_item_state_rejects_invalid_state_machine_values(self) -> None:
        base = {
            "ref": "group/project#1",
            "last_meaningful_update_at": 1.0,
            "updated_at": 1.0,
            "created_at": 1.0,
        }
        with self.assertRaises(ValidationError):
            WorkItemState(**base, current_stage="mystery_stage")
        with self.assertRaises(ValidationError):
            WorkItemState(**base, artifact_state="unknown_artifact")
        with self.assertRaises(ValidationError):
            WorkItemHandoff(
                from_agent="james",
                to_agent="quinn",
                requested_at=1.0,
                status="waiting_forever",
            )

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
