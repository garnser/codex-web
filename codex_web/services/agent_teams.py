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
    AgentTeamLifecycle,
    AgentTeamLifecycleChange,
    AgentTeamRevision,
    AgentTeamUpdate,
    TeamCoordinatorDecision,
    TeamDelegationPlan,
    TeamDelegationRequest,
    TeamInstructionsDefinition,
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.definitions = definitions
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
        if request.trigger_id and request.trigger_id in self._seen_triggers:
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
        if decision.decision_key in self._seen_decisions:
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
