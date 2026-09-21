from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from codex_web.agent_profiles import (
    AgentProfileCreate,
    AgentProfileLifecycle,
    AgentProfileLifecycleChange,
)
from codex_web.agent_teams import (
    AgentTeamAssignmentRequest,
    AgentTeamBudgets,
    AgentTeamCoordinatorDecision,
    AgentTeamCreate,
    AgentTeamDelegationMode,
    AgentTeamDelegationStatus,
    AgentTeamExecutionLink,
    AgentTeamMemberInput,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_teams import (
    AgentTeamBudgetExceeded,
    AgentTeamService,
    AgentTeamStaleDecision,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_teams import AgentTeamStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor(
    identity_id: str,
    *,
    admin: bool = False,
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id=identity_id,
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="workspace-a",
        roles=(
            (MembershipRole.ADMIN,)
            if admin
            else (MembershipRole.MEMBER,)
        ),
        assurance=AuthenticationAssurance.MFA,
    )


class _Authority:
    def __init__(self) -> None:
        self.roles: dict[str, tuple[str, ...]] = {}

    def role_ids_for_actor(
        self,
        actor,
        *,
        project_id=None,
    ):
        del project_id
        return self.roles.get(actor.identity_id, ())


class _Attention:
    def __init__(self) -> None:
        self.items = []

    async def upsert(self, payload, *, actor_id):
        self.items.append((payload, actor_id))
        return payload


class AgentTeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.definitions = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.authority = _Authority()
        self.profile_store = AgentProfileStore(self.sqlite)
        self.profiles = AgentProfileService(
            self.profile_store,
            definitions=self.definitions,
            authority=self.authority,
        )
        self.admin = _actor("admin", admin=True)
        self.member = _actor("member")
        self.attention = _Attention()
        self.dispatches: list[dict] = []
        self.work_item = SimpleNamespace(
            ref="WI-1",
            organization_id="org-a",
            workspace_id="workspace-a",
            project_id="project-a",
            closed_at=None,
        )
        self.assignments: list[tuple] = []
        self.events: list[tuple] = []
        self.teams = AgentTeamService(
            AgentTeamStore(self.sqlite),
            definitions=self.definitions,
            profiles=self.profiles,
            authority=self.authority,
            attention=self.attention,
            work_item_getter=lambda ref: self.work_item,
            work_item_assigner=lambda *args: self.assignments.append(args),
            work_item_event=lambda *args: self.events.append(args),
        )

        async def dispatch(
            profile_id,
            profile_revision,
            objective,
            project_id,
            actor,
            context,
            coordinator,
        ):
            self.dispatches.append(
                {
                    "profile_id": profile_id,
                    "profile_revision": profile_revision,
                    "objective": objective,
                    "project_id": project_id,
                    "actor": actor.identity_id,
                    "context": context,
                    "coordinator": coordinator,
                }
            )
            return AgentTeamExecutionLink(
                member_id=profile_id,
                profile_id=profile_id,
                profile_revision=profile_revision,
                thread_id=f"thread-{len(self.dispatches)}",
                execution_id=f"exec-{len(self.dispatches)}",
            )

        self.teams.bind_dispatcher(dispatch)
        self._profile("leader")
        self._profile("backend")
        self._profile("frontend")

    async def asyncTearDown(self) -> None:
        await self.teams.stop()
        self.temp.cleanup()

    def _profile(self, profile_id: str):
        return self.profiles.create(
            AgentProfileCreate(
                profile_id=profile_id,
                name=profile_id.title(),
            ),
            actor=self.admin,
        )

    def _team(
        self,
        *,
        team_id: str = "delivery",
        members: tuple[AgentTeamMemberInput, ...] | None = None,
        budgets: AgentTeamBudgets | None = None,
    ):
        return self.teams.create(
            AgentTeamCreate(
                team_id=team_id,
                name="Delivery",
                leader_profile_id="leader",
                members=members
                or (
                    AgentTeamMemberInput(
                        member_id="backend-member",
                        profile_id="backend",
                        capability_tags=("python", "code"),
                    ),
                    AgentTeamMemberInput(
                        member_id="frontend-member",
                        profile_id="frontend",
                        capability_tags=("web", "code"),
                    ),
                ),
                routing_instructions=(
                    "Prefer one deterministic capability match. "
                    "Delegate in parallel only when work is independent."
                ),
                budgets=budgets or AgentTeamBudgets(),
            ),
            actor=self.admin,
        )

    def _request(
        self,
        *,
        capabilities: tuple[str, ...] = (),
        event_id: str | None = None,
    ) -> AgentTeamAssignmentRequest:
        return AgentTeamAssignmentRequest(
            work_item_ref="WI-1",
            project_id="project-a",
            objective="Implement the requested change",
            required_capabilities=capabilities,
            event_id=event_id,
        )

    async def test_direct_match_bypasses_coordinator(self) -> None:
        self._team()
        record = await self.teams.assign(
            "delivery",
            self._request(capabilities=("python",)),
            actor=self.member,
        )

        self.assertEqual(record.mode, AgentTeamDelegationMode.DIRECT)
        self.assertEqual(
            record.selected_member_ids,
            ("backend-member",),
        )
        self.assertEqual(record.status, AgentTeamDelegationStatus.DISPATCHED)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.dispatches[0]["profile_id"], "backend")
        self.assertFalse(self.dispatches[0]["coordinator"])
        self.assertEqual(self.assignments[0][1], "delivery")

    async def test_ambiguous_match_triggers_only_leader_then_parallel_members(
        self,
    ) -> None:
        team = self._team()
        record = await self.teams.assign(
            "delivery",
            self._request(capabilities=("code",), event_id="event-1"),
            actor=self.member,
        )

        self.assertEqual(record.mode, AgentTeamDelegationMode.COORDINATOR)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.dispatches[0]["profile_id"], "leader")
        self.assertTrue(self.dispatches[0]["coordinator"])
        self.assertEqual(
            {
                item["memberId"]
                for item in self.dispatches[0]["context"]["members"]
            },
            {"backend-member", "frontend-member"},
        )

        updated = await self.teams.submit_coordinator_decision(
            AgentTeamCoordinatorDecision(
                delegation_id=record.delegation_id,
                team_revision=team.revision,
                source_profile_id="leader",
                selected_member_ids=(
                    "backend-member",
                    "frontend-member",
                ),
                reason="Both specialties are independently required",
                observed_event_id="event-1",
            ),
            actor=self.member,
        )
        self.assertEqual(
            set(updated.active_member_ids),
            {"backend-member", "frontend-member"},
        )
        self.assertEqual(len(self.dispatches), 3)
        self.assertEqual(
            {item["profile_id"] for item in self.dispatches[1:]},
            {"backend", "frontend"},
        )
        dispatch_count = len(self.dispatches)
        duplicate = await self.teams.submit_coordinator_decision(
            AgentTeamCoordinatorDecision(
                delegation_id=record.delegation_id,
                team_revision=team.revision,
                source_profile_id="leader",
                selected_member_ids=(
                    "backend-member",
                    "frontend-member",
                ),
                reason="duplicate coordinator output",
                observed_event_id="event-1",
            ),
            actor=self.member,
        )
        self.assertEqual(duplicate.delegation_id, updated.delegation_id)
        self.assertEqual(len(self.dispatches), dispatch_count)

    async def test_duplicate_trigger_does_not_dispatch_again(self) -> None:
        self._team()
        first = await self.teams.assign(
            "delivery",
            self._request(
                capabilities=("python",),
                event_id="same-event",
            ),
            actor=self.member,
        )
        second = await self.teams.assign(
            "delivery",
            self._request(
                capabilities=("python",),
                event_id="same-event",
            ),
            actor=self.member,
        )
        self.assertEqual(first.delegation_id, second.delegation_id)
        self.assertEqual(len(self.dispatches), 1)

    async def test_stale_coordinator_decision_is_rejected(self) -> None:
        team = self._team()
        record = await self.teams.assign(
            "delivery",
            self._request(capabilities=("code",), event_id="current"),
            actor=self.member,
        )
        with self.assertRaises(AgentTeamStaleDecision):
            await self.teams.submit_coordinator_decision(
                AgentTeamCoordinatorDecision(
                    delegation_id=record.delegation_id,
                    team_revision=team.revision,
                    source_profile_id="leader",
                    selected_member_ids=("backend-member",),
                    reason="route backend",
                    observed_event_id="stale",
                ),
                actor=self.member,
            )

    async def test_self_delegation_escalates_instead_of_looping(self) -> None:
        self._team(
            team_id="self-loop",
            members=(
                AgentTeamMemberInput(
                    member_id="leader-member",
                    profile_id="leader",
                    capability_tags=("code",),
                ),
                AgentTeamMemberInput(
                    member_id="backend-member",
                    profile_id="backend",
                    capability_tags=("code",),
                ),
            ),
        )
        record = await self.teams.assign(
            "self-loop",
            self._request(capabilities=("code",)),
            actor=self.member,
        )
        updated = await self.teams.submit_coordinator_decision(
            AgentTeamCoordinatorDecision(
                delegation_id=record.delegation_id,
                team_revision=1,
                source_profile_id="leader",
                selected_member_ids=("leader-member",),
                reason="I should do it",
            ),
            actor=self.member,
        )
        self.assertEqual(
            updated.status,
            AgentTeamDelegationStatus.ESCALATED,
        )
        self.assertIn("itself", updated.blocker or "")
        self.assertEqual(len(self.dispatches), 1)
        self.assertTrue(self.attention.items)

    async def test_parallel_budget_is_enforced(self) -> None:
        self._team(
            team_id="bounded",
            budgets=AgentTeamBudgets(
                max_handoffs=4,
                max_participants=1,
                max_coordinator_rounds=1,
                max_parallel_executions=1,
            ),
        )
        record = await self.teams.assign(
            "bounded",
            self._request(capabilities=("code",)),
            actor=self.member,
        )
        with self.assertRaises(AgentTeamBudgetExceeded):
            await self.teams.submit_coordinator_decision(
                AgentTeamCoordinatorDecision(
                    delegation_id=record.delegation_id,
                    team_revision=1,
                    source_profile_id="leader",
                    selected_member_ids=(
                        "backend-member",
                        "frontend-member",
                    ),
                    reason="too many",
                ),
                actor=self.member,
            )
        self.assertEqual(len(self.dispatches), 1)

    async def test_waiting_for_parallel_member_consumes_no_new_coordination(
        self,
    ) -> None:
        team = self._team(team_id="waiting")
        record = await self.teams.assign(
            "waiting",
            self._request(capabilities=("code",)),
            actor=self.member,
        )
        record = await self.teams.submit_coordinator_decision(
            AgentTeamCoordinatorDecision(
                delegation_id=record.delegation_id,
                team_revision=team.revision,
                source_profile_id="leader",
                selected_member_ids=(
                    "backend-member",
                    "frontend-member",
                ),
                reason="parallel",
            ),
            actor=self.member,
        )
        dispatch_count = len(self.dispatches)
        updated = await self.teams.record_member_result(
            record.delegation_id,
            member_id="backend-member",
            event_id="backend-complete",
            succeeded=True,
            actor=self.member,
        )
        await asyncio.sleep(0)
        self.assertEqual(
            updated.active_member_ids,
            ("frontend-member",),
        )
        self.assertEqual(len(self.dispatches), dispatch_count)

    async def test_stale_member_result_does_not_mutate_active_execution(self) -> None:
        self._team(team_id="stale-result")
        record = await self.teams.assign(
            "stale-result",
            self._request(capabilities=("python",)),
            actor=self.member,
        )
        current_execution = record.execution_links[-1].execution_id
        stale = await self.teams.record_member_result(
            record.delegation_id,
            member_id="backend-member",
            event_id="late-result",
            execution_id="old-execution",
            succeeded=True,
            actor=self.member,
        )
        self.assertEqual(stale.active_member_ids, ("backend-member",))
        self.assertEqual(stale.result_event_ids, ())
        completed = await self.teams.record_member_result(
            record.delegation_id,
            member_id="backend-member",
            event_id="current-result",
            execution_id=current_execution,
            succeeded=True,
            actor=self.member,
        )
        self.assertEqual(completed.active_member_ids, ())
        self.assertEqual(
            completed.status,
            AgentTeamDelegationStatus.COMPLETED,
        )

    async def test_failed_direct_member_recoordinates_once_and_dedupes_result(
        self,
    ) -> None:
        self._team(team_id="retry")
        record = await self.teams.assign(
            "retry",
            self._request(capabilities=("python",)),
            actor=self.member,
        )
        first = await self.teams.record_member_result(
            record.delegation_id,
            member_id="backend-member",
            event_id="failure-1",
            succeeded=False,
            actor=self.member,
        )
        duplicate = await self.teams.record_member_result(
            record.delegation_id,
            member_id="backend-member",
            event_id="failure-1",
            succeeded=False,
            actor=self.member,
        )
        self.assertEqual(
            first.result_event_ids,
            duplicate.result_event_ids,
        )
        for _ in range(50):
            if len(self.dispatches) >= 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.dispatches), 2)
        self.assertTrue(self.dispatches[-1]["coordinator"])

    async def test_authority_denial_makes_member_ineligible(self) -> None:
        current = self.profiles.get(
            "backend",
            actor=self.admin,
        )
        privileged = current.model_copy(
            update={
                "record_id": f"agent-profile-rev-{uuid.uuid4().hex}",
                "revision": current.revision + 1,
                "authority_role_id": "privileged",
            }
        )
        self.profile_store.append(privileged)
        self.authority.roles["admin"] = ("privileged",)
        self._team(
            team_id="authority",
            members=(
                AgentTeamMemberInput(
                    member_id="backend-member",
                    profile_id="backend",
                    profile_revision=privileged.revision,
                    capability_tags=("python",),
                ),
            ),
        )
        record = await self.teams.assign(
            "authority",
            self._request(capabilities=("python",)),
            actor=self.member,
        )
        self.assertEqual(
            record.status,
            AgentTeamDelegationStatus.BLOCKED,
        )
        self.assertEqual(len(self.dispatches), 0)
        self.assertTrue(self.attention.items)

    async def test_member_unavailable_and_budget_exhaustion_escalate(self) -> None:
        self._team(team_id="unavailable")
        self.profiles.lifecycle(
            "backend",
            AgentProfileLifecycle.DISABLED,
            AgentProfileLifecycleChange(reason="maintenance"),
            actor=self.admin,
        )
        record = await self.teams.assign(
            "unavailable",
            self._request(capabilities=("python",)),
            actor=self.member,
        )
        self.assertEqual(
            record.status,
            AgentTeamDelegationStatus.BLOCKED,
        )
        self.assertTrue(self.attention.items)
        self.profiles.lifecycle(
            "backend",
            AgentProfileLifecycle.ACTIVE,
            AgentProfileLifecycleChange(reason="maintenance complete"),
            actor=self.admin,
        )

        team = self._team(team_id="round-budget")
        coordination = await self.teams.assign(
            "round-budget",
            self._request(capabilities=("code",)),
            actor=self.member,
        )
        coordination = await self.teams.submit_coordinator_decision(
            AgentTeamCoordinatorDecision(
                delegation_id=coordination.delegation_id,
                team_revision=team.revision,
                source_profile_id="leader",
                selected_member_ids=("frontend-member",),
                reason="frontend first",
            ),
            actor=self.member,
        )
        exhausted = await self.teams.record_member_result(
            coordination.delegation_id,
            member_id="frontend-member",
            event_id="frontend-failed",
            succeeded=False,
            actor=self.member,
        )
        latest = self.teams.history(
            actor=self.member,
            team_id="round-budget",
        )[0]
        self.assertEqual(
            latest.status,
            AgentTeamDelegationStatus.ESCALATED,
        )
        self.assertIn("budget exhausted", latest.blocker or "")
