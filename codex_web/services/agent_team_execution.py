from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from codex_web.agent_teams import (
    AgentTeamDelegationRecord,
    TeamDecisionExecutionRequest,
    TeamDelegationPlan,
    TeamDelegationRequest,
    TeamExecutionLaunchResult,
    TeamExecutionLinksUpdate,
    TeamExecutionRequest,
    TeamLaunchedExecution,
    TeamMemberResultEvent,
    TeamMemberResultOutcome,
)
from codex_web.identity import AuthenticationActor
from codex_web.models import TurnCreate
from codex_web.services.agent_teams import AgentTeamService
from codex_web.services.threads import ThreadService
from codex_web.services.turns import TurnService


class AgentTeamExecutionService:
    """Launch bounded Team work through the canonical thread/turn runtime."""

    def __init__(
        self,
        teams: AgentTeamService,
        *,
        threads: ThreadService,
        turns: TurnService,
    ) -> None:
        self.teams = teams
        self.threads = threads
        self.turns = turns

    @staticmethod
    def _thread_id(response: dict[str, Any]) -> str:
        payload = response.get("thread")
        if not isinstance(payload, dict):
            payload = response
        thread_id = payload.get("id") if isinstance(payload, dict) else None
        if not thread_id:
            raise RuntimeError("profile-bound thread creation returned no thread id")
        return str(thread_id)

    async def _launch_profile(
        self,
        profile_id: str,
        *,
        role: str,
        project_id: str | None,
        work_item_id: str,
        message: str,
        repository_resource_id: str | None,
        read_only_repository_resource_ids: tuple[str, ...],
        actor: AuthenticationActor,
    ) -> TeamLaunchedExecution:
        if not project_id:
            raise RuntimeError(
                "Team execution requires an explicit canonical project id"
            )
        profile, _decision = self.teams.profiles.resolve_for_execution(
            profile_id,
            actor=actor,
            project_id=project_id,
        )
        thread = await self.threads.create(
            project_id=project_id,
            repository_resource_id=repository_resource_id,
            read_only_repository_resource_ids=read_only_repository_resource_ids,
            actor=actor,
            agent_profile_id=profile_id,
            agent_profile_revision=profile.revision,
        )
        thread_id = self._thread_id(thread)
        execution_id = f"team-turn-{uuid.uuid4().hex}"
        turn = await self.turns.start(
            thread_id,
            TurnCreate(
                message=message,
                project_id=project_id,
                repository_resource_id=repository_resource_id,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
                agent_profile_id=profile_id,
                agent_profile_revision=profile.revision,
            ),
            actor=actor,
            execution_id=execution_id,
            work_item_ref=work_item_id,
        )
        return TeamLaunchedExecution(
            profile_id=profile_id,
            profile_revision=profile.revision,
            thread_id=thread_id,
            execution_id=execution_id,
            role=role,
            queued=bool(turn.get("queued")),
        )

    def _coordinator_message(
        self,
        team_id: str,
        request: TeamDelegationRequest,
        task: str,
        plan: TeamDelegationPlan,
        *,
        actor: AuthenticationActor,
        result_summary: str | None = None,
    ) -> str:
        team = self.teams.get(team_id, actor=actor)
        instructions: dict[str, Any] | None = None
        if team.instructions_ref is not None:
            record = self.teams.definitions.get_record(
                team.instructions_ref.record_id
            )
            instructions = dict(record.payload)
        roster = [
            {
                "profileId": member.profile_id,
                "role": member.role,
                "capabilities": list(member.capability_tags),
            }
            for member in team.members
            if (
                not plan.metadata.get("eligibleProfileIds")
                or member.profile_id in plan.metadata.get("eligibleProfileIds", [])
            )
        ]
        context = {
            "workItemId": request.work_item_id,
            "projectId": request.project_id,
            "task": task,
            "eligibleRoster": roster,
            "teamInstructions": instructions,
            "handoffCount": request.handoff_count,
            "coordinatorRound": plan.coordinator_round,
        }
        if result_summary is not None:
            context["memberResultSummary"] = result_summary
        return (
            "You are the bounded coordinator for this Agent Team. "
            "Choose only the minimum eligible member set needed for the next "
            "step. Do not message or invoke members yourself. Return a "
            "structured delegation decision for the control plane; the control "
            "plane validates authority, budgets and execution eligibility.\n\n"
            + json.dumps(context, sort_keys=True)
        )

    @staticmethod
    def _member_message(
        request: TeamDelegationRequest,
        task: str,
        *,
        reason: str,
    ) -> str:
        return (
            f"Work Item: {request.work_item_id}\n"
            f"Delegation reason: {reason}\n\n"
            f"{task}"
        )

    async def _record_launch_failure(
        self,
        team_id: str,
        request: TeamDelegationRequest,
        *,
        profile_id: str,
        role: str,
        reason: str,
        actor: AuthenticationActor,
    ) -> None:
        team = self.teams.get(team_id, actor=actor)
        plan = TeamDelegationPlan(
            team_id=team.team_id,
            team_revision=team.revision,
            work_item_id=request.work_item_id,
            mode="launch_blocked",
            selected_profile_ids=(
                (profile_id,) if role == "member" else ()
            ),
            leader_profile_id=(
                profile_id if role == "coordinator" else None
            ),
            reason=reason[:2000],
            handoff_count=request.handoff_count,
            coordinator_round=request.coordinator_round,
            blocked=True,
            attention_required=True,
        )
        record = self.teams._record(
            team,
            request,
            plan,
            actor=actor,
            event_type=f"{role}_launch_failed",
        )
        await self.teams._attention_for(record)

    async def execute(
        self,
        team_id: str,
        payload: TeamExecutionRequest,
        *,
        actor: AuthenticationActor,
    ) -> TeamExecutionLaunchResult:
        request = payload.delegation
        plan = await self.teams.plan_and_record(
            team_id,
            request,
            actor=actor,
        )
        if plan.blocked:
            return TeamExecutionLaunchResult(plan=plan)

        if plan.mode == "direct":
            launched: list[TeamLaunchedExecution] = []
            for profile_id in plan.selected_profile_ids:
                try:
                    launched.append(
                        await self._launch_profile(
                            profile_id,
                            role="member",
                            project_id=request.project_id,
                            work_item_id=request.work_item_id,
                            message=self._member_message(
                                request,
                                payload.message,
                                reason=plan.reason,
                            ),
                            repository_resource_id=payload.repository_resource_id,
                            read_only_repository_resource_ids=(
                                payload.read_only_repository_resource_ids
                            ),
                            actor=actor,
                        )
                    )
                except Exception as exc:
                    await self._record_launch_failure(
                        team_id,
                        request,
                        profile_id=profile_id,
                        role="member",
                        reason=str(exc),
                        actor=actor,
                    )
                    blocked = plan.model_copy(
                        update={
                            "mode": "launch_blocked",
                            "reason": str(exc)[:2000],
                            "blocked": True,
                            "attention_required": True,
                        }
                    )
                    return TeamExecutionLaunchResult(plan=blocked)
            self.teams.link_executions(
                team_id,
                request.work_item_id,
                TeamExecutionLinksUpdate(
                    member_execution_ids={
                        item.profile_id: item.execution_id
                        for item in launched
                    }
                ),
                actor=actor,
            )
            return TeamExecutionLaunchResult(
                plan=plan,
                members=tuple(launched),
            )

        if plan.mode == "coordinator" and plan.leader_profile_id:
            try:
                coordinator = await self._launch_profile(
                    plan.leader_profile_id,
                    role="coordinator",
                    project_id=request.project_id,
                    work_item_id=request.work_item_id,
                    message=self._coordinator_message(
                        team_id,
                        request,
                        payload.message,
                        plan,
                        actor=actor,
                    ),
                    repository_resource_id=payload.repository_resource_id,
                    read_only_repository_resource_ids=(
                        payload.read_only_repository_resource_ids
                    ),
                    actor=actor,
                )
            except Exception as exc:
                await self._record_launch_failure(
                    team_id,
                    request,
                    profile_id=plan.leader_profile_id,
                    role="coordinator",
                    reason=str(exc),
                    actor=actor,
                )
                blocked = plan.model_copy(
                    update={
                        "mode": "launch_blocked",
                        "reason": str(exc)[:2000],
                        "blocked": True,
                        "attention_required": True,
                    }
                )
                return TeamExecutionLaunchResult(plan=blocked)
            self.teams.link_executions(
                team_id,
                request.work_item_id,
                TeamExecutionLinksUpdate(
                    coordinator_execution_id=coordinator.execution_id,
                ),
                actor=actor,
            )
            return TeamExecutionLaunchResult(
                plan=plan,
                coordinator=coordinator,
            )

        return TeamExecutionLaunchResult(plan=plan)

    async def execute_decision(
        self,
        team_id: str,
        payload: TeamDecisionExecutionRequest,
        *,
        actor: AuthenticationActor,
    ) -> TeamExecutionLaunchResult:
        request = payload.delegation
        plan = await self.teams.decide_and_record(
            team_id,
            request,
            payload.decision,
            actor=actor,
        )
        if plan.blocked:
            return TeamExecutionLaunchResult(plan=plan)

        launched: list[TeamLaunchedExecution] = []
        for profile_id in plan.selected_profile_ids:
            try:
                launched.append(
                    await self._launch_profile(
                        profile_id,
                        role="member",
                        project_id=request.project_id,
                        work_item_id=request.work_item_id,
                        message=self._member_message(
                            request,
                            payload.message,
                            reason=plan.reason,
                        ),
                        repository_resource_id=payload.repository_resource_id,
                        read_only_repository_resource_ids=(
                            payload.read_only_repository_resource_ids
                        ),
                        actor=actor,
                    )
                )
            except Exception as exc:
                await self._record_launch_failure(
                    team_id,
                    request,
                    profile_id=profile_id,
                    role="member",
                    reason=str(exc),
                    actor=actor,
                )
                blocked = plan.model_copy(
                    update={
                        "mode": "launch_blocked",
                        "reason": str(exc)[:2000],
                        "blocked": True,
                        "attention_required": True,
                    }
                )
                return TeamExecutionLaunchResult(
                    plan=blocked,
                    members=tuple(launched),
                )

        self.teams.link_executions(
            team_id,
            request.work_item_id,
            TeamExecutionLinksUpdate(
                member_execution_ids={
                    item.profile_id: item.execution_id
                    for item in launched
                }
            ),
            actor=actor,
        )
        return TeamExecutionLaunchResult(
            plan=plan,
            members=tuple(launched),
        )

    def _latest_member_link(
        self,
        team_id: str,
        work_item_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamDelegationRecord | None:
        records = self.teams.store.list_delegations(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            work_item_id=work_item_id,
            team_id=team_id,
        )
        return next(
            (
                record
                for record in reversed(records)
                if record.event_type == "execution_linked"
                and record.member_execution_ids
            ),
            None,
        )

    def _result_seen(
        self,
        team_id: str,
        event: TeamMemberResultEvent,
        *,
        actor: AuthenticationActor,
    ) -> bool:
        return any(
            record.trigger_id == event.result_id
            and record.event_type in {
                "member_result",
                "member_result_stale",
            }
            for record in self.teams.store.list_delegations(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                work_item_id=event.work_item_id,
                team_id=team_id,
            )
        )

    def _append_result_record(
        self,
        team_id: str,
        event: TeamMemberResultEvent,
        *,
        actor: AuthenticationActor,
        event_type: str,
        reason: str,
        source: AgentTeamDelegationRecord | None,
    ) -> AgentTeamDelegationRecord:
        team = self.teams.get(team_id, actor=actor)
        material = {
            "team": team_id,
            "workItem": event.work_item_id,
            "result": event.result_id,
            "execution": event.execution_id,
            "eventType": event_type,
        }
        return self.teams.store.append_delegation(
            AgentTeamDelegationRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                team_id=team_id,
                team_revision=team.revision,
                work_item_id=event.work_item_id,
                project_id=event.project_id,
                event_type=event_type,
                mode=event_type,
                reason=reason[:2000],
                selected_profile_ids=(event.profile_id,),
                leader_profile_id=team.leader_profile_id,
                trigger_id=event.result_id,
                dedupe_key=hashlib.sha256(
                    json.dumps(material, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                member_execution_ids={
                    event.profile_id: event.execution_id
                },
                handoff_count=(
                    source.handoff_count if source is not None else 0
                ),
                coordinator_round=(
                    source.coordinator_round if source is not None else 0
                ),
                actor_identity_id=actor.identity_id,
            )
        )

    async def member_result(
        self,
        team_id: str,
        event: TeamMemberResultEvent,
        *,
        actor: AuthenticationActor,
    ) -> TeamMemberResultOutcome:
        team = self.teams.get(team_id, actor=actor)
        if self._result_seen(team_id, event, actor=actor):
            return TeamMemberResultOutcome(
                status="deduped",
                reason="duplicate_member_result",
            )

        source = self._latest_member_link(
            team_id,
            event.work_item_id,
            actor=actor,
        )
        expected_execution = (
            source.member_execution_ids.get(event.profile_id)
            if source is not None
            else None
        )
        if expected_execution != event.execution_id:
            self._append_result_record(
                team_id,
                event,
                actor=actor,
                event_type="member_result_stale",
                reason="stale_or_superseded_member_execution",
                source=source,
            )
            return TeamMemberResultOutcome(
                status="stale",
                reason="stale_or_superseded_member_execution",
            )

        self._append_result_record(
            team_id,
            event,
            actor=actor,
            event_type="member_result",
            reason="member result accepted for bounded coordinator synthesis",
            source=source,
        )

        handoff_count = (source.handoff_count if source else 0) + 1
        coordinator_round = (source.coordinator_round if source else 0) + 1
        if (
            handoff_count > team.budgets.max_handoffs
            or coordinator_round > team.budgets.max_coordinator_rounds
        ):
            request = TeamDelegationRequest(
                work_item_id=event.work_item_id,
                project_id=event.project_id,
                handoff_count=handoff_count,
                coordinator_round=coordinator_round,
                trigger_id=event.result_id,
            )
            plan = TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=event.work_item_id,
                mode="budget_exhausted",
                reason=(
                    "member_result_recoordination_budget_exhausted"
                ),
                handoff_count=handoff_count,
                coordinator_round=coordinator_round,
                blocked=True,
                attention_required=True,
            )
            record = self.teams._record(
                team,
                request,
                plan,
                actor=actor,
                event_type="member_result_budget_exhausted",
            )
            await self.teams._attention_for(record)
            return TeamMemberResultOutcome(
                status="budget_exhausted",
                reason=plan.reason,
                attention_required=True,
            )

        request = TeamDelegationRequest(
            work_item_id=event.work_item_id,
            project_id=event.project_id,
            prior_participant_ids=(
                source.selected_profile_ids if source is not None else ()
            ),
            handoff_count=handoff_count,
            coordinator_round=coordinator_round,
            trigger_id=event.result_id,
        )
        plan = TeamDelegationPlan(
            team_id=team.team_id,
            team_revision=team.revision,
            work_item_id=event.work_item_id,
            mode="coordinator",
            leader_profile_id=team.leader_profile_id,
            instructions_ref=team.instructions_ref,
            reason="relevant_member_result_requires_bounded_synthesis",
            handoff_count=handoff_count,
            coordinator_round=coordinator_round,
        )
        try:
            coordinator = await self._launch_profile(
                team.leader_profile_id,
                role="coordinator",
                project_id=event.project_id,
                work_item_id=event.work_item_id,
                message=self._coordinator_message(
                    team_id,
                    request,
                    "Synthesize the accepted member result and choose the next step.",
                    plan,
                    actor=actor,
                    result_summary=event.summary,
                ),
                repository_resource_id=None,
                read_only_repository_resource_ids=(),
                actor=actor,
            )
        except Exception as exc:
            await self._record_launch_failure(
                team_id,
                request,
                profile_id=team.leader_profile_id,
                role="coordinator",
                reason=str(exc),
                actor=actor,
            )
            return TeamMemberResultOutcome(
                status="launch_blocked",
                reason=str(exc)[:2000],
                delegation=request,
                attention_required=True,
            )

        material = {
            "team": team_id,
            "workItem": event.work_item_id,
            "result": event.result_id,
            "coordinatorExecution": coordinator.execution_id,
        }
        self.teams.store.append_delegation(
            AgentTeamDelegationRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                team_id=team_id,
                team_revision=team.revision,
                work_item_id=event.work_item_id,
                project_id=event.project_id,
                event_type="member_result_coordinator_retrigger",
                mode="coordinator",
                reason=plan.reason,
                leader_profile_id=team.leader_profile_id,
                trigger_id=event.result_id,
                dedupe_key=hashlib.sha256(
                    json.dumps(material, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                coordinator_execution_id=coordinator.execution_id,
                handoff_count=handoff_count,
                coordinator_round=coordinator_round,
                actor_identity_id=actor.identity_id,
            )
        )
        return TeamMemberResultOutcome(
            status="coordinator_retriggered",
            reason=plan.reason,
            coordinator=coordinator,
            delegation=request,
        )
