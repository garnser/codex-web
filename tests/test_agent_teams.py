from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from codex_web.agent_profiles import AgentProfileCreate
from codex_web.agent_teams import (
    AgentTeamBudgets,
    AgentTeamCreate,
    AgentTeamMember,
    TeamCoordinatorDecision,
    TeamDelegationRequest,
    TeamExecutionLinksUpdate,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    reference_for,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_teams import AgentTeamService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_teams import AgentTeamStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor(identity_id: str, *, admin: bool = False) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id=identity_id,
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="workspace-a",
        roles=((MembershipRole.ADMIN,) if admin else (MembershipRole.MEMBER,)),
        assurance=AuthenticationAssurance.MFA,
    )


class AgentTeamServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.definitions = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.profiles = AgentProfileService(
            AgentProfileStore(self.sqlite),
            definitions=self.definitions,
        )
        self.teams = AgentTeamService(
            AgentTeamStore(self.sqlite),
            profiles=self.profiles,
            definitions=self.definitions,
        )
        self.admin = _actor("admin-a", admin=True)
        self.member = _actor("member-a")
        for profile_id in ("leader", "python", "docs", "ops"):
            self.profiles.create(
                AgentProfileCreate(
                    profile_id=profile_id,
                    name=profile_id.title(),
                ),
                actor=self.admin,
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _team(self, **updates):
        data = {
            "team_id": "delivery",
            "name": "Delivery",
            "leader_profile_id": "leader",
            "members": (
                AgentTeamMember(
                    profile_id="python",
                    role="developer",
                    capability_tags=("python", "backend"),
                ),
                AgentTeamMember(
                    profile_id="docs",
                    role="writer",
                    capability_tags=("docs",),
                ),
                AgentTeamMember(
                    profile_id="ops",
                    role="operator",
                    capability_tags=("ops", "backend"),
                ),
            ),
            "budgets": AgentTeamBudgets(
                max_handoffs=4,
                max_participants=3,
                max_coordinator_rounds=2,
                max_parallel_executions=2,
            ),
        }
        data.update(updates)
        return self.teams.create(AgentTeamCreate(**data), actor=self.admin)

    def test_unique_capability_match_routes_directly_without_coordinator(self) -> None:
        self._team()
        plan = self.teams.plan(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-1",
                project_id="project-a",
                required_capabilities=("python",),
            ),
            actor=self.member,
        )
        self.assertEqual(plan.mode, "direct")
        self.assertEqual(plan.selected_profile_ids, ("python",))
        self.assertIsNone(plan.leader_profile_id)

    def test_ambiguous_match_wakes_only_leader_for_structured_decision(self) -> None:
        self._team()
        plan = self.teams.plan(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-2",
                project_id="project-a",
                required_capabilities=("backend",),
            ),
            actor=self.member,
        )
        self.assertEqual(plan.mode, "coordinator")
        self.assertEqual(plan.leader_profile_id, "leader")
        self.assertEqual(
            sorted(plan.metadata["eligibleProfileIds"]),
            ["ops", "python"],
        )
        self.assertEqual(plan.selected_profile_ids, ())

    def test_coordinator_cannot_delegate_to_itself(self) -> None:
        self._team()
        plan = self.teams.apply_coordinator_decision(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-3",
                project_id="project-a",
                required_capabilities=("backend",),
            ),
            TeamCoordinatorDecision(
                selected_profile_ids=("leader",),
                reason="bad recursive choice",
                decision_key="decision-self",
            ),
            actor=self.member,
        )
        self.assertEqual(plan.mode, "loop_suppressed")
        self.assertTrue(plan.blocked)
        self.assertTrue(plan.attention_required)

    def test_duplicate_trigger_is_deduped_without_recoordination(self) -> None:
        self._team()
        request = TeamDelegationRequest(
            work_item_id="work-4",
            project_id="project-a",
            required_capabilities=("backend",),
            trigger_id="result-17",
        )
        first = self.teams.plan("delivery", request, actor=self.member)
        second = self.teams.plan("delivery", request, actor=self.member)
        self.assertEqual(first.mode, "coordinator")
        self.assertEqual(second.mode, "deduped")
        self.assertTrue(second.blocked)

    def test_handoff_and_reasoning_budgets_fail_closed(self) -> None:
        self._team()
        handoffs = self.teams.plan(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-5",
                required_capabilities=("python",),
                handoff_count=4,
            ),
            actor=self.member,
        )
        self.assertEqual(handoffs.mode, "budget_exhausted")
        self.assertTrue(handoffs.attention_required)

        rounds = self.teams.plan(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-6",
                required_capabilities=("backend",),
                coordinator_round=2,
            ),
            actor=self.member,
        )
        self.assertEqual(rounds.mode, "budget_exhausted")
        self.assertTrue(rounds.attention_required)

    def test_parallel_selection_is_bounded_and_deduped(self) -> None:
        self._team()
        request = TeamDelegationRequest(
            work_item_id="work-7",
            project_id="project-a",
            required_capabilities=("backend",),
        )
        decision = TeamCoordinatorDecision(
            selected_profile_ids=("python", "ops"),
            reason="split implementation and operational review",
            decision_key="decision-7",
        )
        plan = self.teams.apply_coordinator_decision(
            "delivery",
            request,
            decision,
            actor=self.member,
        )
        self.assertEqual(plan.mode, "delegated")
        self.assertEqual(set(plan.selected_profile_ids), {"python", "ops"})

        duplicate = self.teams.apply_coordinator_decision(
            "delivery",
            request,
            decision,
            actor=self.member,
        )
        self.assertEqual(duplicate.mode, "deduped")

    def test_unavailable_or_denied_member_is_not_selected(self) -> None:
        self._team()
        plan = self.teams.plan(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-8",
                project_id="project-a",
                required_capabilities=("backend",),
                unavailable_profile_ids=("python",),
            ),
            actor=self.member,
        )
        self.assertEqual(plan.mode, "direct")
        self.assertEqual(plan.selected_profile_ids, ("ops",))

    def test_team_instructions_are_exact_versioned_definition_reference(self) -> None:
        draft = self.definitions.create_draft(
            DefinitionDraftCreate(
                definition_id="delivery-routing",
                kind="agent.team.instructions",
                definition_schema_version="1.0",
                payload={
                    "instructions": "Prefer deterministic routing first.",
                    "routing_notes": ["Use capability metadata before coordinator."],
                },
                actor=self.admin.identity_id,
            )
        )
        published = self.definitions.publish(
            draft.record_id,
            DefinitionPublishRequest(actor=self.admin.identity_id),
        )
        team = self._team(instructions_ref=reference_for(published))
        self.assertEqual(team.instructions_ref.record_id, published.record_id)

    def test_membership_capability_labels_do_not_bypass_profile_access(self) -> None:
        self._team()
        self.profiles.lifecycle(
            "python",
            lifecycle=__import__(
                "codex_web.agent_profiles",
                fromlist=["AgentProfileLifecycle"],
            ).AgentProfileLifecycle.DISABLED,
            payload=__import__(
                "codex_web.agent_profiles",
                fromlist=["AgentProfileLifecycleChange"],
            ).AgentProfileLifecycleChange(reason="maintenance"),
            actor=self.admin,
        )
        plan = self.teams.plan(
            "delivery",
            TeamDelegationRequest(
                work_item_id="work-9",
                project_id="project-a",
                required_capabilities=("python",),
            ),
            actor=self.member,
        )
        self.assertEqual(plan.mode, "blocked")
        self.assertEqual(plan.reason, "no_eligible_team_member")

    def test_persisted_equivalent_route_is_deduped_after_service_restart(self) -> None:
        self._team()
        request = TeamDelegationRequest(
            work_item_id="work-persist",
            project_id="project-a",
            required_capabilities=("backend",),
        )
        first = asyncio.run(
            self.teams.plan_and_record(
                "delivery",
                request,
                actor=self.member,
            )
        )
        self.assertEqual(first.mode, "coordinator")

        restarted = AgentTeamService(
            AgentTeamStore(self.sqlite),
            profiles=self.profiles,
            definitions=self.definitions,
        )
        duplicate = asyncio.run(
            restarted.plan_and_record(
                "delivery",
                request,
                actor=self.member,
            )
        )
        self.assertEqual(duplicate.mode, "deduped")
        self.assertEqual(
            duplicate.reason,
            "equivalent_pending_or_recorded_delegation",
        )
        history = restarted.work_item_history(
            "work-persist",
            actor=self.member,
        )
        self.assertEqual(history["count"], 1)

    def test_budget_exhaustion_creates_durable_attention_link(self) -> None:
        class _Attention:
            async def upsert(self, payload, *, actor_id):
                self.payload = payload
                self.actor_id = actor_id
                return type("_Item", (), {"id": "attention-team-1"})()

        attention = _Attention()
        service = AgentTeamService(
            AgentTeamStore(self.sqlite),
            profiles=self.profiles,
            definitions=self.definitions,
            attention=attention,
        )
        self._team()
        plan = asyncio.run(
            service.plan_and_record(
                "delivery",
                TeamDelegationRequest(
                    work_item_id="work-attention",
                    handoff_count=4,
                    required_capabilities=("python",),
                ),
                actor=self.member,
            )
        )
        self.assertEqual(plan.mode, "budget_exhausted")
        history = service.work_item_history(
            "work-attention",
            actor=self.member,
        )
        self.assertEqual(history["count"], 1)
        self.assertEqual(
            history["items"][0]["attention_item_id"],
            "attention-team-1",
        )
        self.assertEqual(len(history["blockers"]), 1)

    def test_work_item_history_links_executions_and_splits_usage(self) -> None:
        class _UsageStore:
            def list(self):
                def usage(execution_id, tokens, cost):
                    return type(
                        "_Usage",
                        (),
                        {
                            "organization_id": "org-a",
                            "workspace_id": "workspace-a",
                            "work_item_ref": "work-usage",
                            "execution_id": execution_id,
                            "total_tokens": tokens,
                            "cost_usd": cost,
                        },
                    )()
                return [
                    usage("exec-leader", 100, 0.10),
                    usage("exec-python", 400, 0.40),
                ]

        service = AgentTeamService(
            AgentTeamStore(self.sqlite),
            profiles=self.profiles,
            definitions=self.definitions,
            runtime_usage_store=_UsageStore(),
            assignment_loader=lambda: [
                type(
                    "_Assignment",
                    (),
                    {
                        "execution_id": "exec-python",
                        "status": "running",
                    },
                )()
            ],
        )
        self._team()
        request = TeamDelegationRequest(
            work_item_id="work-usage",
            project_id="project-a",
            required_capabilities=("backend",),
        )
        initial = asyncio.run(
            service.plan_and_record(
                "delivery",
                request,
                actor=self.member,
            )
        )
        self.assertEqual(initial.mode, "coordinator")
        delegated = asyncio.run(
            service.decide_and_record(
                "delivery",
                request,
                TeamCoordinatorDecision(
                    selected_profile_ids=("python",),
                    reason="python owns this backend change",
                    decision_key="decision-usage",
                ),
                actor=self.member,
            )
        )
        self.assertEqual(delegated.mode, "delegated")
        service.link_executions(
            "delivery",
            "work-usage",
            TeamExecutionLinksUpdate(
                coordinator_execution_id="exec-leader",
                member_execution_ids={"python": "exec-python"},
                child_work_item_refs=("child-1",),
            ),
            actor=self.member,
        )

        history = service.work_item_history(
            "work-usage",
            actor=self.member,
        )
        self.assertEqual(
            history["activeMemberProfileIds"],
            ["python"],
        )
        self.assertEqual(history["usage"]["coordinator"]["tokens"], 100)
        self.assertEqual(history["usage"]["workers"]["tokens"], 400)
        self.assertEqual(history["usage"]["coordinator"]["records"], 1)
        self.assertEqual(history["usage"]["workers"]["records"], 1)
        self.assertEqual(
            history["items"][-1]["child_work_item_refs"],
            ["child-1"],
        )



if __name__ == "__main__":
    unittest.main()
