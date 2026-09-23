from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable

from codex_web.agent_teams import (
    TEAM_INSTRUCTIONS_KIND,
    TEAM_INSTRUCTIONS_SCHEMA_VERSION,
    AgentTeamCreate,
    AgentTeamDelegationRecord,
    AgentTeamLifecycle,
    AgentTeamLifecycleChange,
    AgentTeamRevision,
    AgentTeamUpdate,
    TeamCoordinatorDecision,
    TeamDelegationPlan,
    TeamDelegationRequest,
    TeamExecutionLinksUpdate,
    TeamInstructionsDefinition,
)
from codex_web.attention import (
    AttentionItemCreate,
    AttentionSeverity,
    AttentionSource,
    EscalationPolicy,
)
from codex_web.definitions import (
    DefinitionLifecycle,
    DefinitionReference,
    definition_is_effective,
    reference_for,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_profiles import (
    AgentProfileAccessDenied,
    AgentProfileNotFound,
    AgentProfileService,
)
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.agent_teams import AgentTeamStore


class AgentTeamError(RuntimeError):
    pass


class AgentTeamNotFound(AgentTeamError):
    pass


class AgentTeamConflict(AgentTeamError):
    pass


class AgentTeamAccessDenied(AgentTeamError):
    pass


def validate_team_instructions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return TeamInstructionsDefinition.model_validate(payload).model_dump(mode="json")


def install_team_instructions_schema(
    definitions: DefinitionRegistryService,
) -> None:
    try:
        definitions.schemas.get(
            TEAM_INSTRUCTIONS_KIND,
            TEAM_INSTRUCTIONS_SCHEMA_VERSION,
        )
        return
    except Exception:
        pass
    definitions.register_schema(
        DefinitionKindSchema(
            kind=TEAM_INSTRUCTIONS_KIND,
            schema_version=TEAM_INSTRUCTIONS_SCHEMA_VERSION,
            validate=validate_team_instructions_payload,
        )
    )


class AgentTeamService:
    def __init__(
        self,
        store: AgentTeamStore,
        *,
        profiles: AgentProfileService,
        definitions: DefinitionRegistryService,
        attention: Any | None = None,
        runtime_usage_store: Any | None = None,
        assignment_loader: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.definitions = definitions
        self.attention = attention
        self.runtime_usage_store = runtime_usage_store
        self.assignment_loader = assignment_loader
        self.clock = clock
        install_team_instructions_schema(definitions)
        self._seen_triggers: set[str] = set()
        self._seen_decisions: set[str] = set()

    @staticmethod
    def _is_admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "agent-teams:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @classmethod
    def _require_mutation(
        cls,
        actor: AuthenticationActor,
        *,
        owner_identity_id: str,
    ) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "agent-teams:admin" not in actor.service_scopes:
                raise AuthorizationError("agent-teams:admin service scope required")
            return
        if not cls._is_admin(actor) and actor.identity_id != owner_identity_id:
            raise AuthorizationError(
                "agent team owner or tenant administrator required"
            )
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )

    def _latest(
        self,
        team_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        item = self.store.latest(
            team_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if item is None:
            raise AgentTeamNotFound("agent team not found")
        return item

    def _instructions_ref(
        self,
        value: DefinitionReference | None,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionReference | None:
        if value is None:
            return None
        record = self.definitions.get_record(value.record_id)
        actual = reference_for(record)
        if actual != value:
            raise AgentTeamConflict(
                "team instructions definition reference does not match canonical record"
            )
        if record.kind != TEAM_INSTRUCTIONS_KIND:
            raise AgentTeamConflict(
                f"team instructions must use {TEAM_INSTRUCTIONS_KIND}"
            )
        if record.definition_schema_version != TEAM_INSTRUCTIONS_SCHEMA_VERSION:
            raise AgentTeamConflict("unsupported team instructions schema")
        if record.lifecycle not in {
            DefinitionLifecycle.PUBLISHED,
            DefinitionLifecycle.SUPERSEDED,
        }:
            raise AgentTeamConflict("team instructions must reference a published revision")
        if not definition_is_effective(record):
            raise AgentTeamConflict("team instructions are not currently effective")
        scope = record.scope_type.value
        if scope == "organization" and record.scope_id != actor.organization_id:
            raise AgentTeamConflict("team instructions are outside actor organization")
        if scope == "workspace" and record.scope_id != actor.workspace_id:
            raise AgentTeamConflict("team instructions are outside actor workspace")
        if scope == "project":
            raise AgentTeamConflict(
                "project-scoped instructions cannot back a reusable workspace team"
            )
        return actual

    def _validate_profiles(
        self,
        *,
        leader_profile_id: str,
        member_profile_ids: tuple[str, ...],
        actor: AuthenticationActor,
    ) -> None:
        ids = (leader_profile_id, *member_profile_ids)
        for profile_id in ids:
            try:
                profile = self.profiles.get(
                    profile_id,
                    actor=actor,
                    require_visible=False,
                )
            except AgentProfileNotFound as exc:
                raise AgentTeamConflict(
                    f"unknown agent profile in team: {profile_id}"
                ) from exc
            if (
                profile.organization_id != actor.organization_id
                or profile.workspace_id != actor.workspace_id
            ):
                raise AgentTeamConflict("team profile is outside actor workspace")

    def can_view(
        self,
        team: AgentTeamRevision,
        *,
        actor: AuthenticationActor,
    ) -> bool:
        if (
            team.organization_id != actor.organization_id
            or team.workspace_id != actor.workspace_id
        ):
            return False
        if self._is_admin(actor) or team.owner_identity_id == actor.identity_id:
            return True
        if not team.allowed_identity_ids and not team.allowed_role_ids:
            return True
        if actor.identity_id in team.allowed_identity_ids:
            return True
        try:
            actor_roles = set(
                self.profiles._actor_authority_roles(actor)
            )
        except Exception:
            actor_roles = set()
        return bool(actor_roles & set(team.allowed_role_ids))

    def get(
        self,
        team_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        item = self._latest(team_id, actor=actor)
        if not self.can_view(item, actor=actor):
            raise AgentTeamNotFound("agent team not found")
        return item

    def list(
        self,
        *,
        actor: AuthenticationActor,
        include_archived: bool = False,
    ) -> list[AgentTeamRevision]:
        latest: dict[str, AgentTeamRevision] = {}
        for item in self.store.list_revisions(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        ):
            latest[item.team_id] = item
        return sorted(
            [
                item
                for item in latest.values()
                if (include_archived or item.lifecycle != AgentTeamLifecycle.ARCHIVED)
                and self.can_view(item, actor=actor)
            ],
            key=lambda item: (item.name.casefold(), item.team_id),
        )

    def revisions(
        self,
        team_id: str,
        *,
        actor: AuthenticationActor,
    ) -> list[AgentTeamRevision]:
        self.get(team_id, actor=actor)
        return self.store.list_revisions(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            team_id=team_id,
        )

    def create(
        self,
        payload: AgentTeamCreate,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        owner = payload.owner_identity_id or actor.identity_id
        self._require_mutation(actor, owner_identity_id=owner)
        if self.store.latest(
            payload.team_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        ) is not None:
            raise AgentTeamConflict("agent team already exists")
        self._validate_profiles(
            leader_profile_id=payload.leader_profile_id,
            member_profile_ids=tuple(item.profile_id for item in payload.members),
            actor=actor,
        )
        now = float(self.clock())
        return self.store.append(
            AgentTeamRevision(
                team_id=payload.team_id,
                revision=1,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                name=payload.name,
                description=payload.description,
                owner_identity_id=owner,
                leader_profile_id=payload.leader_profile_id,
                members=payload.members,
                instructions_ref=self._instructions_ref(
                    payload.instructions_ref,
                    actor=actor,
                ),
                budgets=payload.budgets,
                allowed_identity_ids=payload.allowed_identity_ids,
                allowed_role_ids=payload.allowed_role_ids,
                escalation_target=payload.escalation_target,
                created_by=actor.identity_id,
                updated_by=actor.identity_id,
                change_reason=payload.reason,
                created_at=now,
                updated_at=now,
            )
        )

    def update(
        self,
        team_id: str,
        payload: AgentTeamUpdate,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        current = self._latest(team_id, actor=actor)
        self._require_mutation(actor, owner_identity_id=current.owner_identity_id)
        fields = payload.model_fields_set
        updates: dict[str, Any] = {}
        for field_name in (
            "name",
            "description",
            "owner_identity_id",
            "leader_profile_id",
            "members",
            "budgets",
            "allowed_identity_ids",
            "allowed_role_ids",
            "escalation_target",
        ):
            if field_name in fields:
                updates[field_name] = getattr(payload, field_name)
        if "instructions_ref" in fields:
            updates["instructions_ref"] = self._instructions_ref(
                payload.instructions_ref,
                actor=actor,
            )
        leader = updates.get("leader_profile_id", current.leader_profile_id)
        members = updates.get("members", current.members)
        self._validate_profiles(
            leader_profile_id=leader,
            member_profile_ids=tuple(item.profile_id for item in members),
            actor=actor,
        )
        next_item = current.model_copy(
            update={
                **updates,
                "record_id": f"agent-team-rev-{uuid.uuid4().hex}",
                "revision": current.revision + 1,
                "updated_by": actor.identity_id,
                "updated_at": float(self.clock()),
                "change_reason": payload.reason,
            }
        )
        return self.store.append(next_item)

    def lifecycle(
        self,
        team_id: str,
        lifecycle: AgentTeamLifecycle,
        payload: AgentTeamLifecycleChange,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        current = self._latest(team_id, actor=actor)
        self._require_mutation(actor, owner_identity_id=current.owner_identity_id)
        if current.lifecycle == lifecycle:
            return current
        return self.store.append(
            current.model_copy(
                update={
                    "record_id": f"agent-team-rev-{uuid.uuid4().hex}",
                    "revision": current.revision + 1,
                    "lifecycle": lifecycle,
                    "updated_by": actor.identity_id,
                    "updated_at": float(self.clock()),
                    "change_reason": payload.reason,
                }
            )
        )

    @staticmethod
    def _dedupe_key(
        team: AgentTeamRevision,
        request: TeamDelegationRequest,
        selected: tuple[str, ...],
        reason: str,
    ) -> str:
        material = {
            "team": team.team_id,
            "revision": team.revision,
            "work_item": request.work_item_id,
            "selected": selected,
            "required": request.required_capabilities,
            "reason": reason,
            "round": request.coordinator_round,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _eligible_members(
        self,
        team: AgentTeamRevision,
        request: TeamDelegationRequest,
        *,
        actor: AuthenticationActor,
    ) -> list[str]:
        required = set(request.required_capabilities)
        unavailable = set(request.unavailable_profile_ids)
        active = set(request.active_profile_ids)
        eligible: list[str] = []
        for member in team.members:
            if member.profile_id in unavailable:
                continue
            if member.profile_id in active:
                continue
            if required and not required.issubset(set(member.capability_tags)):
                continue
            try:
                self.profiles.resolve_for_execution(
                    member.profile_id,
                    actor=actor,
                    project_id=request.project_id,
                )
            except (AgentProfileNotFound, AgentProfileAccessDenied):
                continue
            eligible.append(member.profile_id)
        return eligible

    def plan(
        self,
        team_id: str,
        request: TeamDelegationRequest,
        *,
        actor: AuthenticationActor,
    ) -> TeamDelegationPlan:
        team = self.get(team_id, actor=actor)
        if team.lifecycle != AgentTeamLifecycle.ACTIVE:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="blocked",
                reason=f"team_{team.lifecycle.value}",
                blocked=True,
                attention_required=True,
            )
        if request.trigger_id and (
            request.trigger_id in self._seen_triggers
            or any(
                item.trigger_id == request.trigger_id
                and item.team_id == team.team_id
                and item.work_item_id == request.work_item_id
                for item in self.store.list_delegations(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    work_item_id=request.work_item_id,
                    team_id=team.team_id,
                )
            )
        ):
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="deduped",
                reason="duplicate_trigger",
                blocked=True,
            )
        if request.trigger_id:
            self._seen_triggers.add(request.trigger_id)
        if request.handoff_count >= team.budgets.max_handoffs:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="budget_exhausted",
                reason="max_handoffs_exhausted",
                blocked=True,
                attention_required=True,
                handoff_count=request.handoff_count,
            )
        if request.coordinator_round >= team.budgets.max_coordinator_rounds:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="budget_exhausted",
                reason="max_coordinator_rounds_exhausted",
                blocked=True,
                attention_required=True,
                coordinator_round=request.coordinator_round,
            )

        eligible = self._eligible_members(team, request, actor=actor)
        if len(eligible) == 1:
            selected = (eligible[0],)
            reason = "deterministic_unique_capability_match"
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="direct",
                selected_profile_ids=selected,
                reason=reason,
                handoff_count=request.handoff_count + 1,
                coordinator_round=request.coordinator_round,
                dedupe_key=self._dedupe_key(team, request, selected, reason),
            )
        if not eligible:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="blocked",
                reason="no_eligible_team_member",
                blocked=True,
                attention_required=True,
            )

        try:
            self.profiles.resolve_for_execution(
                team.leader_profile_id,
                actor=actor,
                project_id=request.project_id,
            )
        except (AgentProfileNotFound, AgentProfileAccessDenied):
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="blocked",
                reason="leader_unavailable_or_denied",
                blocked=True,
                attention_required=True,
            )
        return TeamDelegationPlan(
            team_id=team.team_id,
            team_revision=team.revision,
            work_item_id=request.work_item_id,
            mode="coordinator",
            leader_profile_id=team.leader_profile_id,
            instructions_ref=team.instructions_ref,
            reason="ambiguous_eligible_members_require_structured_coordinator_decision",
            handoff_count=request.handoff_count,
            coordinator_round=request.coordinator_round + 1,
            metadata={
                "eligibleProfileIds": eligible,
                "maxSelected": min(
                    team.budgets.max_parallel_executions,
                    team.budgets.max_participants,
                ),
            },
        )

    def apply_coordinator_decision(
        self,
        team_id: str,
        request: TeamDelegationRequest,
        decision: TeamCoordinatorDecision,
        *,
        actor: AuthenticationActor,
    ) -> TeamDelegationPlan:
        team = self.get(team_id, actor=actor)
        if (
            decision.decision_key in self._seen_decisions
            or any(
                item.decision_key == decision.decision_key
                and item.team_id == team.team_id
                and item.work_item_id == request.work_item_id
                for item in self.store.list_delegations(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    work_item_id=request.work_item_id,
                    team_id=team.team_id,
                )
            )
        ):
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="deduped",
                reason="duplicate_coordinator_decision",
                blocked=True,
                dedupe_key=decision.decision_key,
            )
        self._seen_decisions.add(decision.decision_key)

        selected = tuple(dict.fromkeys(decision.selected_profile_ids))
        if request.coordinator_round > team.budgets.max_coordinator_rounds:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="budget_exhausted",
                reason="max_coordinator_rounds_exhausted",
                blocked=True,
                attention_required=True,
                handoff_count=request.handoff_count,
                coordinator_round=request.coordinator_round,
                dedupe_key=decision.decision_key,
            )
        if team.leader_profile_id in selected:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="loop_suppressed",
                reason="leader_cannot_delegate_to_itself",
                blocked=True,
                attention_required=True,
                dedupe_key=decision.decision_key,
            )
        prior = self.store.list_delegations(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            work_item_id=request.work_item_id,
            team_id=team.team_id,
        )
        if any(
            item.event_type == "coordinator_decision"
            and item.mode == "delegated"
            and item.selected_profile_ids == selected
            and item.reason == decision.reason
            for item in prior
        ):
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="loop_suppressed",
                reason="equivalent_delegation_loop_detected",
                blocked=True,
                attention_required=True,
                handoff_count=request.handoff_count,
                coordinator_round=request.coordinator_round,
                dedupe_key=decision.decision_key,
            )
        eligible = set(self._eligible_members(team, request, actor=actor))
        if any(profile_id not in eligible for profile_id in selected):
            raise AgentTeamConflict(
                "coordinator selected an ineligible or unauthorized member"
            )
        limit = min(
            team.budgets.max_parallel_executions,
            team.budgets.max_participants,
        )
        if not selected:
            raise AgentTeamConflict("coordinator must select at least one member")
        if len(selected) > limit:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="budget_exhausted",
                reason="coordinator_selection_exceeds_parallel_or_participant_budget",
                blocked=True,
                attention_required=True,
                dedupe_key=decision.decision_key,
            )
        participant_ids = set(request.prior_participant_ids)
        participant_ids.update(selected)
        if len(participant_ids) > team.budgets.max_participants:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="budget_exhausted",
                reason="max_participants_exhausted",
                blocked=True,
                attention_required=True,
                dedupe_key=decision.decision_key,
            )
        if request.handoff_count + len(selected) > team.budgets.max_handoffs:
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="budget_exhausted",
                reason="max_handoffs_exhausted",
                blocked=True,
                attention_required=True,
                dedupe_key=decision.decision_key,
            )
        return TeamDelegationPlan(
            team_id=team.team_id,
            team_revision=team.revision,
            work_item_id=request.work_item_id,
            mode="delegated",
            selected_profile_ids=selected,
            leader_profile_id=team.leader_profile_id,
            instructions_ref=team.instructions_ref,
            reason=decision.reason,
            handoff_count=request.handoff_count + len(selected),
            coordinator_round=request.coordinator_round,
            dedupe_key=decision.decision_key,
            metadata={"participantIds": sorted(participant_ids)},
        )


    def _record(
        self,
        team: AgentTeamRevision,
        request: TeamDelegationRequest,
        plan: TeamDelegationPlan,
        *,
        actor: AuthenticationActor,
        event_type: str,
        decision_key: str | None = None,
    ) -> AgentTeamDelegationRecord:
        dedupe_key = plan.dedupe_key or self._dedupe_key(
            team,
            request,
            plan.selected_profile_ids,
            f"{event_type}:{plan.mode}:{plan.reason}",
        )
        return self.store.append_delegation(
            AgentTeamDelegationRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                project_id=request.project_id,
                event_type=event_type,
                mode=plan.mode,
                reason=plan.reason,
                selected_profile_ids=plan.selected_profile_ids,
                leader_profile_id=plan.leader_profile_id,
                trigger_id=request.trigger_id,
                decision_key=decision_key,
                dedupe_key=dedupe_key,
                handoff_count=plan.handoff_count,
                coordinator_round=plan.coordinator_round,
                blocked=plan.blocked,
                attention_required=plan.attention_required,
                actor_identity_id=actor.identity_id,
            )
        )

    async def _attention_for(
        self,
        record: AgentTeamDelegationRecord,
    ) -> str | None:
        if not record.attention_required or self.attention is None:
            return None
        item = await self.attention.upsert(
            AttentionItemCreate(
                organization_id=record.organization_id,
                workspace_id=record.workspace_id,
                type="agent_team.delegation_blocked",
                severity=AttentionSeverity.HIGH,
                source=AttentionSource(
                    object_type="agent_team_delegation",
                    object_id=record.id,
                ),
                reason=record.reason,
                dedupe_key=f"agent-team:{record.team_id}:{record.work_item_id}:{record.reason}",
                requesting_agent_team_id=record.team_id,
                deep_link=f"/?work_item={record.work_item_id}",
                escalation=EscalationPolicy(mandatory=True),
            ),
            actor_id="agent-team-orchestration",
        )
        self.store.update_delegation(
            record.id,
            organization_id=record.organization_id,
            workspace_id=record.workspace_id,
            updater=lambda current: current.model_copy(
                update={"attention_item_id": item.id}
            ),
        )
        return item.id

    async def plan_and_record(
        self,
        team_id: str,
        request: TeamDelegationRequest,
        *,
        actor: AuthenticationActor,
    ) -> TeamDelegationPlan:
        plan = self.plan(team_id, request, actor=actor)
        team = self.get(team_id, actor=actor)
        dedupe_key = plan.dedupe_key or self._dedupe_key(
            team,
            request,
            plan.selected_profile_ids,
            f"routing_plan:{plan.mode}:{plan.reason}",
        )
        existing = self.store.delegation_by_dedupe_key(
            dedupe_key,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if existing is not None and plan.mode != "deduped":
            return TeamDelegationPlan(
                team_id=team.team_id,
                team_revision=team.revision,
                work_item_id=request.work_item_id,
                mode="deduped",
                reason="equivalent_pending_or_recorded_delegation",
                blocked=True,
                dedupe_key=dedupe_key,
            )
        if plan.mode != "deduped":
            plan = plan.model_copy(update={"dedupe_key": dedupe_key})
            record = self._record(
                team,
                request,
                plan,
                actor=actor,
                event_type="routing_plan",
            )
            await self._attention_for(record)
        return plan

    async def decide_and_record(
        self,
        team_id: str,
        request: TeamDelegationRequest,
        decision: TeamCoordinatorDecision,
        *,
        actor: AuthenticationActor,
    ) -> TeamDelegationPlan:
        plan = self.apply_coordinator_decision(
            team_id,
            request,
            decision,
            actor=actor,
        )
        team = self.get(team_id, actor=actor)
        if plan.mode != "deduped":
            record = self._record(
                team,
                request,
                plan,
                actor=actor,
                event_type="coordinator_decision",
                decision_key=decision.decision_key,
            )
            await self._attention_for(record)
        return plan

    def link_executions(
        self,
        team_id: str,
        work_item_id: str,
        payload: TeamExecutionLinksUpdate,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamDelegationRecord:
        team = self.get(team_id, actor=actor)
        history = self.store.list_delegations(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            work_item_id=work_item_id,
            team_id=team_id,
        )
        source = next(
            (
                item
                for item in reversed(history)
                if item.event_type in {"coordinator_decision", "routing_plan"}
            ),
            None,
        )
        if source is None:
            raise AgentTeamConflict(
                "execution links require an existing delegation plan"
            )
        allowed = set(source.selected_profile_ids)
        if any(key not in allowed for key in payload.member_execution_ids):
            raise AgentTeamConflict(
                "member execution link does not match selected team member"
            )
        material = json.dumps(
            {
                "team": team_id,
                "work_item": work_item_id,
                "coordinator": payload.coordinator_execution_id,
                "members": payload.member_execution_ids,
                "children": payload.child_work_item_refs,
            },
            sort_keys=True,
        )
        dedupe_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return self.store.append_delegation(
            AgentTeamDelegationRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                team_id=team_id,
                team_revision=team.revision,
                work_item_id=work_item_id,
                project_id=source.project_id,
                event_type="execution_linked",
                mode="linked",
                reason="canonical execution links recorded",
                selected_profile_ids=source.selected_profile_ids,
                leader_profile_id=source.leader_profile_id,
                dedupe_key=dedupe_key,
                coordinator_execution_id=payload.coordinator_execution_id,
                member_execution_ids=payload.member_execution_ids,
                child_work_item_refs=payload.child_work_item_refs,
                handoff_count=source.handoff_count,
                coordinator_round=source.coordinator_round,
                actor_identity_id=actor.identity_id,
            )
        )

    def work_item_history(
        self,
        work_item_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        records = self.store.list_delegations(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            work_item_id=work_item_id,
        )
        visible = []
        for record in records:
            try:
                self.get(record.team_id, actor=actor)
            except AgentTeamNotFound:
                continue
            visible.append(record)

        execution_roles: dict[str, str] = {}
        for record in visible:
            if record.coordinator_execution_id:
                execution_roles[record.coordinator_execution_id] = "coordinator"
            for execution_id in record.member_execution_ids.values():
                execution_roles[execution_id] = "workers"

        usage = {"coordinator": {}, "workers": {}}
        if self.runtime_usage_store is not None:
            totals = {
                "coordinator": {"tokens": 0, "costUsd": 0.0, "records": 0},
                "workers": {"tokens": 0, "costUsd": 0.0, "records": 0},
            }
            for item in self.runtime_usage_store.list():
                if (
                    item.organization_id != actor.organization_id
                    or item.workspace_id != actor.workspace_id
                    or item.work_item_ref != work_item_id
                ):
                    continue
                role = execution_roles.get(item.execution_id or "")
                if role is None:
                    continue
                bucket = totals[role]
                bucket["records"] += 1
                if item.total_tokens is not None:
                    bucket["tokens"] += item.total_tokens
                if item.cost_usd is not None:
                    bucket["costUsd"] += item.cost_usd
            usage = totals

        active_member_ids: set[str] = set()
        if self.assignment_loader is not None:
            assignments = self.assignment_loader()
            active_status = {"pending", "claimed", "running"}
            for record in visible:
                for profile_id, execution_id in record.member_execution_ids.items():
                    if any(
                        getattr(item, "execution_id", None) == execution_id
                        and str(getattr(item, "status", "")).split(".")[-1].casefold()
                        in active_status
                        for item in assignments
                    ):
                        active_member_ids.add(profile_id)

        return {
            "items": [
                item.model_dump(mode="json")
                for item in visible
            ],
            "count": len(visible),
            "activeMemberProfileIds": sorted(active_member_ids),
            "usage": usage,
            "blockers": [
                {
                    "recordId": item.id,
                    "teamId": item.team_id,
                    "reason": item.reason,
                    "attentionItemId": item.attention_item_id,
                }
                for item in visible
                if item.blocked or item.attention_required
            ],
        }
