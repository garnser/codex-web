from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.agent_profiles import AgentProfileAccessMode
from codex_web.agent_teams import (
    AGENT_TEAM_ROUTING_KIND,
    AGENT_TEAM_ROUTING_SCHEMA_VERSION,
    AgentTeamAssignmentRequest,
    AgentTeamCoordinatorDecision,
    AgentTeamCreate,
    AgentTeamDelegationMode,
    AgentTeamDelegationRecord,
    AgentTeamDelegationStatus,
    AgentTeamExecutionLink,
    AgentTeamLifecycle,
    AgentTeamLifecycleChange,
    AgentTeamMember,
    AgentTeamMemberInput,
    AgentTeamMemberKind,
    AgentTeamRevision,
    AgentTeamRoutingDefinition,
    AgentTeamUpdate,
    AgentTeamUsageUpdate,
)
from codex_web.attention import (
    AttentionItemCreate,
    AttentionSeverity,
    AttentionSource,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublishRequest,
    DefinitionScope,
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
from codex_web.services.attention import AttentionService
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.storage.agent_teams import AgentTeamStore


class AgentTeamError(RuntimeError):
    pass


class AgentTeamNotFound(AgentTeamError):
    pass


class AgentTeamConflict(AgentTeamError):
    pass


class AgentTeamAccessDenied(AgentTeamError):
    pass


class AgentTeamBudgetExceeded(AgentTeamConflict):
    pass


class AgentTeamStaleDecision(AgentTeamConflict):
    pass


MemberDispatcher = Callable[
    [
        str,
        int,
        str,
        str,
        AuthenticationActor,
        dict[str, Any] | None,
        bool,
    ],
    Awaitable[AgentTeamExecutionLink],
]
WorkItemGetter = Callable[[str], Any]
WorkItemAssigner = Callable[
    [str, str, int, str, str],
    None,
]
WorkItemEventSink = Callable[
    [str, str, dict[str, Any], str],
    None,
]


class AgentTeamService:
    def __init__(
        self,
        store: AgentTeamStore,
        *,
        definitions: DefinitionRegistryService,
        profiles: AgentProfileService,
        authority: Any | None = None,
        attention: AttentionService | None = None,
        work_item_getter: WorkItemGetter | None = None,
        work_item_assigner: WorkItemAssigner | None = None,
        work_item_event: WorkItemEventSink | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.definitions = definitions
        self.profiles = profiles
        self.authority = authority
        self.attention = attention
        self.work_item_getter = work_item_getter
        self.work_item_assigner = work_item_assigner
        self.work_item_event = work_item_event
        self.clock = clock
        self.dispatcher: MemberDispatcher | None = None
        self.coordination = KeyedTaskCoordinator(
            max_concurrency=16,
            per_scope_concurrency=4,
        )
        metadata = {
            (item["kind"], item["schema_version"])
            for item in self.definitions.schemas.metadata()
        }
        if (
            AGENT_TEAM_ROUTING_KIND,
            AGENT_TEAM_ROUTING_SCHEMA_VERSION,
        ) not in metadata:
            self.definitions.register_schema(
                DefinitionKindSchema(
                    kind=AGENT_TEAM_ROUTING_KIND,
                    schema_version=AGENT_TEAM_ROUTING_SCHEMA_VERSION,
                    validate=self._validate_routing_definition,
                )
            )

    @staticmethod
    def _validate_routing_definition(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return AgentTeamRoutingDefinition.model_validate(
            payload
        ).model_dump(mode="json")

    def bind_dispatcher(self, dispatcher: MemberDispatcher) -> None:
        self.dispatcher = dispatcher

    @staticmethod
    def _is_admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "agent-teams:admin" in actor.service_scopes
        return actor.has_role(
            MembershipRole.OWNER,
            MembershipRole.ADMIN,
        )

    @classmethod
    def _require_admin_or_owner(
        cls,
        actor: AuthenticationActor,
        team: AgentTeamRevision | None = None,
        *,
        requested_owner: str | None = None,
    ) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "agent-teams:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "agent-teams:admin service scope required"
                )
            return
        if cls._is_admin(actor):
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
            return
        owner = (
            team.owner_identity_id
            if team is not None
            else requested_owner
        )
        if owner != actor.identity_id:
            raise AuthorizationError(
                "agent team owner or tenant administrator required"
            )
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )

    def _actor_authority_roles(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None = None,
    ) -> tuple[str, ...]:
        if self.authority is None:
            return ()
        try:
            return tuple(
                self.authority.role_ids_for_actor(
                    actor,
                    project_id=project_id,
                )
            )
        except Exception:
            return ()

    def _can_invoke(
        self,
        team: AgentTeamRevision,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
    ) -> bool:
        if (
            team.organization_id != actor.organization_id
            or team.workspace_id != actor.workspace_id
            or team.lifecycle != AgentTeamLifecycle.ACTIVE
        ):
            return False
        if self._is_admin(actor) or team.owner_identity_id == actor.identity_id:
            return True
        access = team.access
        if access.mode == AgentProfileAccessMode.TENANT:
            return True
        if access.mode == AgentProfileAccessMode.OWNER:
            return False
        roles = self._actor_authority_roles(
            actor,
            project_id=project_id,
        )
        return (
            actor.identity_id in access.identity_ids
            or bool(set(access.role_ids) & set(roles))
        )

    def _latest(
        self,
        team_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        team = self.store.latest(
            team_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if team is None:
            raise AgentTeamNotFound("agent team not found")
        return team

    def get(
        self,
        team_id: str,
        *,
        actor: AuthenticationActor,
        revision: int | None = None,
    ) -> AgentTeamRevision:
        current = self._latest(team_id, actor=actor)
        if not (
            self._can_invoke(current, actor=actor)
            or current.owner_identity_id == actor.identity_id
            or self._is_admin(actor)
        ):
            raise AgentTeamNotFound("agent team not found")
        if revision is None:
            return current
        item = self.store.revision(
            team_id,
            revision,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if item is None:
            raise AgentTeamNotFound("agent team revision not found")
        return item

    def list(
        self,
        *,
        actor: AuthenticationActor,
        include_archived: bool = False,
    ) -> list[AgentTeamRevision]:
        revisions = self.store.list_revisions(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        latest: dict[str, AgentTeamRevision] = {}
        for item in revisions:
            latest[item.team_id] = item
        return sorted(
            (
                item
                for item in latest.values()
                if (
                    include_archived
                    or item.lifecycle != AgentTeamLifecycle.ARCHIVED
                )
                and (
                    self._can_invoke(item, actor=actor)
                    or item.owner_identity_id == actor.identity_id
                    or self._is_admin(actor)
                )
            ),
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

    def _resolve_profile(
        self,
        profile_id: str,
        revision: int | None,
        *,
        actor: AuthenticationActor,
        project_id: str = "home",
    ):
        profile, _ = self.profiles.resolve_for_execution(
            profile_id,
            actor=actor,
            project_id=project_id,
            revision=revision,
        )
        return profile

    def _normalize_members(
        self,
        values: tuple[AgentTeamMemberInput, ...],
        *,
        actor: AuthenticationActor,
    ) -> tuple[AgentTeamMember, ...]:
        members: list[AgentTeamMember] = []
        for value in values:
            if value.kind == AgentTeamMemberKind.AGENT:
                if not value.profile_id:
                    raise AgentTeamConflict(
                        "agent team member requires profile_id"
                    )
                profile = self._resolve_profile(
                    value.profile_id,
                    value.profile_revision,
                    actor=actor,
                )
                members.append(
                    AgentTeamMember(
                        member_id=value.member_id,
                        kind=value.kind,
                        profile_id=profile.profile_id,
                        profile_revision=profile.revision,
                        capability_tags=value.capability_tags,
                        role_description=value.role_description,
                        enabled=value.enabled,
                    )
                )
                continue
            members.append(
                AgentTeamMember(
                    member_id=value.member_id,
                    kind=value.kind,
                    identity_id=value.identity_id,
                    capability_tags=value.capability_tags,
                    role_description=value.role_description,
                    enabled=value.enabled,
                )
            )
        return tuple(members)

    def _publish_routing_definition(
        self,
        *,
        team_id: str,
        instructions: str,
        actor: AuthenticationActor,
        reason: str | None,
        previous: AgentTeamRevision | None = None,
    ):
        definition_id = f"agent-team-routing.{team_id}"
        draft = self.definitions.create_draft(
            DefinitionDraftCreate(
                definition_id=definition_id,
                kind=AGENT_TEAM_ROUTING_KIND,
                definition_schema_version=(
                    AGENT_TEAM_ROUTING_SCHEMA_VERSION
                ),
                scope_type=DefinitionScope.WORKSPACE,
                scope_id=actor.workspace_id,
                payload=AgentTeamRoutingDefinition(
                    team_id=team_id,
                    instructions=instructions,
                ).model_dump(mode="json"),
                actor=actor.identity_id,
                reason=reason,
                derived_from_record_id=(
                    previous.routing_definition_ref.record_id
                    if previous is not None
                    else None
                ),
            )
        )
        published = self.definitions.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=actor.identity_id,
                reason=reason,
                expected_active_revision=(
                    previous.routing_definition_ref.revision
                    if previous is not None
                    else None
                ),
            ),
        )
        return reference_for(published)

    def create(
        self,
        payload: AgentTeamCreate,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        owner = payload.owner_identity_id or actor.identity_id
        self._require_admin_or_owner(
            actor,
            requested_owner=owner,
        )
        if self.store.latest(
            payload.team_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        ) is not None:
            raise AgentTeamConflict("agent team already exists")
        leader = self._resolve_profile(
            payload.leader_profile_id,
            payload.leader_profile_revision,
            actor=actor,
        )
        members = self._normalize_members(
            payload.members,
            actor=actor,
        )
        routing_ref = self._publish_routing_definition(
            team_id=payload.team_id,
            instructions=payload.routing_instructions,
            actor=actor,
            reason=payload.reason,
        )
        now = float(self.clock())
        return self.store.append_revision(
            AgentTeamRevision(
                team_id=payload.team_id,
                revision=1,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                name=payload.name,
                description=payload.description,
                owner_identity_id=owner,
                created_by=actor.identity_id,
                updated_by=actor.identity_id,
                leader_profile_id=leader.profile_id,
                leader_profile_revision=leader.revision,
                members=members,
                routing_definition_ref=routing_ref,
                access=payload.access,
                budgets=payload.budgets,
                escalation_identity_ids=(
                    payload.escalation_identity_ids
                ),
                escalation_team_ids=payload.escalation_team_ids,
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
        self._require_admin_or_owner(actor, current)
        fields = payload.model_fields_set
        updates: dict[str, Any] = {}
        for field_name in (
            "name",
            "description",
            "owner_identity_id",
            "access",
            "budgets",
            "escalation_identity_ids",
            "escalation_team_ids",
        ):
            if field_name in fields:
                updates[field_name] = getattr(payload, field_name)

        if (
            "leader_profile_id" in fields
            or "leader_profile_revision" in fields
        ):
            leader_id = (
                payload.leader_profile_id
                if "leader_profile_id" in fields
                else current.leader_profile_id
            )
            leader_revision = (
                payload.leader_profile_revision
                if "leader_profile_revision" in fields
                else current.leader_profile_revision
            )
            if leader_id is None:
                raise AgentTeamConflict("agent team leader is required")
            leader = self._resolve_profile(
                leader_id,
                leader_revision,
                actor=actor,
            )
            updates["leader_profile_id"] = leader.profile_id
            updates["leader_profile_revision"] = leader.revision

        if "members" in fields:
            updates["members"] = self._normalize_members(
                payload.members or (),
                actor=actor,
            )

        if "routing_instructions" in fields:
            updates["routing_definition_ref"] = (
                self._publish_routing_definition(
                    team_id=team_id,
                    instructions=payload.routing_instructions or "",
                    actor=actor,
                    reason=payload.reason,
                    previous=current,
                )
            )

        owner = updates.get(
            "owner_identity_id",
            current.owner_identity_id,
        )
        if (
            owner != current.owner_identity_id
            and not self._is_admin(actor)
        ):
            raise AuthorizationError(
                "only tenant administrators may transfer team ownership"
            )

        now = float(self.clock())
        next_record = current.model_copy(
            update={
                **updates,
                "record_id": f"agent-team-rev-{uuid.uuid4().hex}",
                "revision": current.revision + 1,
                "updated_by": actor.identity_id,
                "updated_at": now,
                "change_reason": payload.reason,
            }
        )
        return self.store.append_revision(next_record)

    def lifecycle(
        self,
        team_id: str,
        lifecycle: AgentTeamLifecycle,
        payload: AgentTeamLifecycleChange,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        current = self._latest(team_id, actor=actor)
        self._require_admin_or_owner(actor, current)
        if current.lifecycle == lifecycle:
            return current
        now = float(self.clock())
        return self.store.append_revision(
            current.model_copy(
                update={
                    "record_id": f"agent-team-rev-{uuid.uuid4().hex}",
                    "revision": current.revision + 1,
                    "lifecycle": lifecycle,
                    "updated_by": actor.identity_id,
                    "updated_at": now,
                    "change_reason": payload.reason,
                }
            )
        )

    @staticmethod
    def _decision_key(
        member_ids: tuple[str, ...],
    ) -> str:
        raw = json.dumps(
            sorted(set(member_ids)),
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _request_key(
        team: AgentTeamRevision,
        payload: AgentTeamAssignmentRequest,
    ) -> str:
        raw = json.dumps(
            {
                "team": team.team_id,
                "revision": team.revision,
                "work_item": payload.work_item_ref,
                "project": payload.project_id,
                "objective": payload.objective,
                "capabilities": payload.required_capabilities,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _work_item(
        self,
        payload: AgentTeamAssignmentRequest,
        *,
        actor: AuthenticationActor,
    ) -> Any | None:
        if self.work_item_getter is None:
            return None
        state = self.work_item_getter(payload.work_item_ref)
        if (
            state.organization_id != actor.organization_id
            or state.workspace_id != actor.workspace_id
        ):
            raise AgentTeamNotFound("work item not found")
        if (
            state.project_id is not None
            and state.project_id != payload.project_id
        ):
            raise AgentTeamConflict(
                "team assignment project does not match Work Item"
            )
        if state.closed_at is not None:
            raise AgentTeamConflict(
                "closed Work Item cannot receive a Team assignment"
            )
        return state

    def _eligible_members(
        self,
        team: AgentTeamRevision,
        payload: AgentTeamAssignmentRequest,
        *,
        actor: AuthenticationActor,
    ) -> list[AgentTeamMember]:
        required = set(payload.required_capabilities)
        eligible: list[AgentTeamMember] = []
        for member in team.members:
            if (
                not member.enabled
                or member.kind != AgentTeamMemberKind.AGENT
                or not member.profile_id
                or member.profile_revision is None
            ):
                continue
            if required and not required.issubset(
                set(member.capability_tags)
            ):
                continue
            try:
                self.profiles.resolve_for_execution(
                    member.profile_id,
                    actor=actor,
                    project_id=payload.project_id,
                    revision=member.profile_revision,
                )
            except (
                AgentProfileAccessDenied,
                AgentProfileNotFound,
            ):
                continue
            eligible.append(member)
        return eligible

    def _routing_context(
        self,
        team: AgentTeamRevision,
        record: AgentTeamDelegationRecord,
        members: list[AgentTeamMember],
    ) -> dict[str, Any]:
        routing_record = self.definitions.get_record(
            team.routing_definition_ref.record_id
        )
        if (
            routing_record.lifecycle != DefinitionLifecycle.PUBLISHED
            or reference_for(routing_record)
            != team.routing_definition_ref
        ):
            raise AgentTeamConflict(
                "team routing Definition is not the pinned published revision"
            )
        routing = AgentTeamRoutingDefinition.model_validate(
            routing_record.payload
        )
        return {
            "delegationId": record.delegation_id,
            "teamId": team.team_id,
            "teamRevision": team.revision,
            "workItemRef": record.work_item_ref,
            "projectId": record.project_id,
            "objective": record.objective,
            "requiredCapabilities": list(
                record.required_capabilities
            ),
            "routingInstructions": routing.instructions,
            "decisionContract": routing.decision_contract,
            "members": [
                {
                    "memberId": member.member_id,
                    "profileId": member.profile_id,
                    "profileRevision": member.profile_revision,
                    "capabilityTags": list(
                        member.capability_tags
                    ),
                    "roleDescription": member.role_description,
                }
                for member in members[
                    : team.budgets.max_participants
                ]
            ],
            "budgets": team.budgets.model_dump(mode="json"),
        }

    async def _attention(
        self,
        team: AgentTeamRevision,
        record: AgentTeamDelegationRecord,
        *,
        code: str,
        reason: str,
        actor_id: str,
    ) -> None:
        if self.attention is None:
            return
        await self.attention.upsert(
            AttentionItemCreate(
                organization_id=record.organization_id,
                workspace_id=record.workspace_id,
                type=f"agent_team.{code}",
                severity=AttentionSeverity.HIGH,
                source=AttentionSource(
                    object_type="agent_team_delegation",
                    object_id=record.delegation_id,
                    event_id=record.event_id,
                ),
                reason=reason,
                dedupe_key=(
                    f"agent-team:{record.delegation_id}:{code}"
                ),
                recipient_identity_ids=(
                    team.escalation_identity_ids
                ),
                recipient_team_ids=team.escalation_team_ids,
                deep_link=(
                    f"/?workItem={record.work_item_ref}"
                    f"&team={team.team_id}"
                ),
            ),
            actor_id=actor_id,
        )

    def _record_work_item_event(
        self,
        record: AgentTeamDelegationRecord,
        event_type: str,
        *,
        actor_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.work_item_event is None:
            return
        self.work_item_event(
            record.work_item_ref,
            event_type,
            {
                "team_id": record.team_id,
                "team_revision": record.team_revision,
                "delegation_id": record.delegation_id,
                **(payload or {}),
            },
            actor_id,
        )

    async def _dispatch_profile(
        self,
        *,
        profile_id: str,
        profile_revision: int,
        objective: str,
        project_id: str,
        actor: AuthenticationActor,
        context: dict[str, Any] | None,
        coordinator: bool,
    ) -> AgentTeamExecutionLink:
        if self.dispatcher is None:
            return AgentTeamExecutionLink(
                member_id=(
                    "__coordinator__"
                    if coordinator
                    else profile_id
                ),
                profile_id=profile_id,
                profile_revision=profile_revision,
            )
        return await self.dispatcher(
            profile_id,
            profile_revision,
            objective,
            project_id,
            actor,
            context,
            coordinator,
        )

    async def assign(
        self,
        team_id: str,
        payload: AgentTeamAssignmentRequest,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamDelegationRecord:
        team = self._latest(team_id, actor=actor)
        if not self._can_invoke(
            team,
            actor=actor,
            project_id=payload.project_id,
        ):
            raise AgentTeamAccessDenied(
                "agent team cannot be invoked"
            )
        self._work_item(payload, actor=actor)

        request_key = self._request_key(team, payload)
        existing = self.store.list_delegations(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            team_id=team_id,
            work_item_ref=payload.work_item_ref,
            limit=20,
        )
        for item in existing:
            if (
                payload.event_id
                and item.event_id == payload.event_id
            ):
                return item
            if (
                item.equivalent_decision_key == request_key
                and item.status
                in {
                    AgentTeamDelegationStatus.PLANNED,
                    AgentTeamDelegationStatus.COORDINATOR_PENDING,
                    AgentTeamDelegationStatus.DISPATCHED,
                }
            ):
                return item

        eligible = self._eligible_members(
            team,
            payload,
            actor=actor,
        )
        if len(eligible) == 1:
            mode = AgentTeamDelegationMode.DIRECT
            status = AgentTeamDelegationStatus.PLANNED
            selected = (eligible[0].member_id,)
            reason_codes = ("deterministic_capability_match",)
        elif len(eligible) > 1:
            mode = AgentTeamDelegationMode.COORDINATOR
            status = AgentTeamDelegationStatus.COORDINATOR_PENDING
            selected = ()
            reason_codes = ("ambiguous_member_match",)
        else:
            mode = AgentTeamDelegationMode.COORDINATOR
            status = AgentTeamDelegationStatus.BLOCKED
            selected = ()
            reason_codes = ("no_eligible_member",)

        record = AgentTeamDelegationRecord(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            team_id=team.team_id,
            team_revision=team.revision,
            team_record_id=team.record_id,
            work_item_ref=payload.work_item_ref,
            project_id=payload.project_id,
            objective=payload.objective,
            required_capabilities=(
                payload.required_capabilities
            ),
            mode=mode,
            status=status,
            reason_codes=reason_codes,
            selected_member_ids=selected,
            coordinator_profile_id=(
                team.leader_profile_id
                if mode == AgentTeamDelegationMode.COORDINATOR
                else None
            ),
            coordinator_profile_revision=(
                team.leader_profile_revision
                if mode == AgentTeamDelegationMode.COORDINATOR
                else None
            ),
            event_id=payload.event_id,
            equivalent_decision_key=request_key,
            blocker=(
                "no eligible Team member"
                if not eligible
                else None
            ),
            created_by=actor.identity_id,
            created_at=float(self.clock()),
            updated_at=float(self.clock()),
        )
        self.store.append_delegation(record)
        if self.work_item_assigner is not None:
            self.work_item_assigner(
                payload.work_item_ref,
                team.team_id,
                team.revision,
                record.delegation_id,
                actor.identity_id,
            )
        self._record_work_item_event(
            record,
            "work_item_team_assigned",
            actor_id=actor.identity_id,
            payload={
                "mode": mode.value,
                "reason_codes": list(reason_codes),
            },
        )

        if not eligible:
            await self._attention(
                team,
                record,
                code="member_unavailable",
                reason=(
                    "No Team member is both capability-matched and "
                    "authorized for this Work Item"
                ),
                actor_id=actor.identity_id,
            )
            return record

        if mode == AgentTeamDelegationMode.DIRECT:
            member = eligible[0]
            link = await self._dispatch_profile(
                profile_id=member.profile_id or "",
                profile_revision=member.profile_revision or 1,
                objective=payload.objective,
                project_id=payload.project_id,
                actor=actor,
                context=None,
                coordinator=False,
            )
            if link.member_id != member.member_id:
                link = link.model_copy(
                    update={"member_id": member.member_id}
                )
            updated = record.model_copy(
                update={
                    "status": AgentTeamDelegationStatus.DISPATCHED,
                    "active_member_ids": (
                        member.member_id,
                    ),
                    "execution_links": (link,),
                    "equivalent_decision_key": self._decision_key(
                        (member.member_id,)
                    ),
                    "updated_at": float(self.clock()),
                }
            )
            self.store.update_delegation(updated)
            self._record_work_item_event(
                updated,
                "work_item_team_member_dispatched",
                actor_id=actor.identity_id,
                payload={
                    "member_ids": [member.member_id],
                    "deterministic": True,
                },
            )
            return updated

        try:
            leader, _ = self.profiles.resolve_for_execution(
                team.leader_profile_id,
                actor=actor,
                project_id=payload.project_id,
                revision=team.leader_profile_revision,
            )
        except Exception as exc:
            blocked = record.model_copy(
                update={
                    "status": AgentTeamDelegationStatus.BLOCKED,
                    "blocker": f"team leader unavailable: {exc}",
                    "updated_at": float(self.clock()),
                }
            )
            self.store.update_delegation(blocked)
            await self._attention(
                team,
                blocked,
                code="leader_unavailable",
                reason=blocked.blocker or "Team leader unavailable",
                actor_id=actor.identity_id,
            )
            return blocked

        context = self._routing_context(
            team,
            record,
            eligible,
        )
        link = await self._dispatch_profile(
            profile_id=leader.profile_id,
            profile_revision=leader.revision,
            objective=(
                "Coordinate this Team Work Item. Return only the "
                "structured delegation decision required by the Team "
                "routing contract."
            ),
            project_id=payload.project_id,
            actor=actor,
            context=context,
            coordinator=True,
        )
        self._record_work_item_event(
            record,
            "work_item_team_coordinator_dispatched",
            actor_id=actor.identity_id,
            payload={
                "leader_profile_id": leader.profile_id,
                "thread_id": link.thread_id,
                "execution_id": link.execution_id,
            },
        )
        return record

    def _team_for_record(
        self,
        record: AgentTeamDelegationRecord,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamRevision:
        team = self.store.revision(
            record.team_id,
            record.team_revision,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if team is None:
            raise AgentTeamNotFound(
                "delegation Team revision not found"
            )
        return team

    async def submit_coordinator_decision(
        self,
        decision: AgentTeamCoordinatorDecision,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamDelegationRecord:
        record = self.store.get_delegation(
            decision.delegation_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if record is None:
            raise AgentTeamNotFound("team delegation not found")
        team = self._team_for_record(record, actor=actor)
        if decision.team_revision != record.team_revision:
            raise AgentTeamStaleDecision(
                "coordinator decision targets a stale Team revision"
            )
        if decision.source_profile_id != team.leader_profile_id:
            raise AgentTeamAccessDenied(
                "only the pinned Team leader may coordinate delegation"
            )
        if (
            record.event_id
            and decision.observed_event_id
            and decision.observed_event_id != record.event_id
        ):
            raise AgentTeamStaleDecision(
                "coordinator decision observed a stale trigger event"
            )
        if record.status not in {
            AgentTeamDelegationStatus.COORDINATOR_PENDING,
            AgentTeamDelegationStatus.DISPATCHED,
        }:
            raise AgentTeamConflict(
                "delegation is not awaiting coordinator routing"
            )
        member_ids = tuple(
            dict.fromkeys(decision.selected_member_ids)
        )
        if not member_ids:
            raise AgentTeamConflict(
                "coordinator must select at least one member"
            )
        if len(member_ids) > team.budgets.max_participants:
            raise AgentTeamBudgetExceeded(
                "Team participant budget exhausted"
            )
        if len(member_ids) > team.budgets.max_parallel_executions:
            raise AgentTeamBudgetExceeded(
                "Team parallel execution budget exhausted"
            )
        next_round = record.coordinator_round + 1
        if next_round > team.budgets.max_coordinator_rounds:
            raise AgentTeamBudgetExceeded(
                "Team coordinator reasoning budget exhausted"
            )
        next_handoffs = record.handoff_count + len(member_ids)
        if next_handoffs > team.budgets.max_handoffs:
            raise AgentTeamBudgetExceeded(
                "Team handoff budget exhausted"
            )

        by_id = {
            member.member_id: member
            for member in team.members
        }
        selected: list[AgentTeamMember] = []
        for member_id in member_ids:
            member = by_id.get(member_id)
            if (
                member is None
                or not member.enabled
                or member.kind != AgentTeamMemberKind.AGENT
                or not member.profile_id
                or member.profile_revision is None
            ):
                raise AgentTeamConflict(
                    f"selected Team member is unavailable: {member_id}"
                )
            if member.profile_id == team.leader_profile_id:
                looped = record.model_copy(
                    update={
                        "status": AgentTeamDelegationStatus.ESCALATED,
                        "blocker": (
                            "Team leader attempted to delegate to itself"
                        ),
                        "updated_at": float(self.clock()),
                    }
                )
                self.store.update_delegation(looped)
                await self._attention(
                    team,
                    looped,
                    code="self_loop",
                    reason=looped.blocker or "Team self-loop",
                    actor_id=actor.identity_id,
                )
                return looped
            try:
                self.profiles.resolve_for_execution(
                    member.profile_id,
                    actor=actor,
                    project_id=record.project_id,
                    revision=member.profile_revision,
                )
            except Exception as exc:
                blocked = record.model_copy(
                    update={
                        "status": AgentTeamDelegationStatus.ESCALATED,
                        "blocker": (
                            f"selected Team member unavailable: "
                            f"{member_id}: {exc}"
                        ),
                        "updated_at": float(self.clock()),
                    }
                )
                self.store.update_delegation(blocked)
                await self._attention(
                    team,
                    blocked,
                    code="member_unavailable",
                    reason=blocked.blocker
                    or "Selected Team member unavailable",
                    actor_id=actor.identity_id,
                )
                return blocked
            selected.append(member)

        decision_key = self._decision_key(member_ids)
        if (
            record.equivalent_decision_key == decision_key
            and any(
                member_id in set(getattr(record, "failed_member_ids", ()))
                for member_id in member_ids
            )
        ):
            looped = record.model_copy(
                update={
                    "status": AgentTeamDelegationStatus.ESCALATED,
                    "blocker": (
                        "repeated equivalent delegation after failure"
                    ),
                    "updated_at": float(self.clock()),
                }
            )
            self.store.update_delegation(looped)
            await self._attention(
                team,
                looped,
                code="delegation_loop",
                reason=looped.blocker or "Team delegation loop",
                actor_id=actor.identity_id,
            )
            return looped

        links = await asyncio.gather(
            *(
                self._dispatch_profile(
                    profile_id=member.profile_id or "",
                    profile_revision=member.profile_revision or 1,
                    objective=record.objective,
                    project_id=record.project_id,
                    actor=actor,
                    context=None,
                    coordinator=False,
                )
                for member in selected
            )
        )
        normalized_links = tuple(
            link.model_copy(
                update={"member_id": member.member_id}
            )
            for member, link in zip(
                selected,
                links,
                strict=True,
            )
        )
        updated = record.model_copy(
            update={
                "status": AgentTeamDelegationStatus.DISPATCHED,
                "selected_member_ids": member_ids,
                "active_member_ids": member_ids,
                "coordinator_round": next_round,
                "handoff_count": next_handoffs,
                "equivalent_decision_key": decision_key,
                "execution_links": (
                    *record.execution_links,
                    *normalized_links,
                ),
                "blocker": None,
                "updated_at": float(self.clock()),
            }
        )
        self.store.update_delegation(updated)
        self._record_work_item_event(
            updated,
            "work_item_team_member_dispatched",
            actor_id=actor.identity_id,
            payload={
                "member_ids": list(member_ids),
                "deterministic": False,
                "reason": decision.reason,
                "coordinator_round": next_round,
            },
        )
        return updated

    async def _schedule_recoordination(
        self,
        record: AgentTeamDelegationRecord,
        *,
        actor: AuthenticationActor,
        event_id: str,
    ) -> None:
        team = self._team_for_record(record, actor=actor)
        if record.coordinator_round >= team.budgets.max_coordinator_rounds:
            exhausted = record.model_copy(
                update={
                    "status": AgentTeamDelegationStatus.ESCALATED,
                    "blocker": (
                        "Team coordinator reasoning budget exhausted"
                    ),
                    "updated_at": float(self.clock()),
                }
            )
            self.store.update_delegation(exhausted)
            await self._attention(
                team,
                exhausted,
                code="budget_exhausted",
                reason=exhausted.blocker
                or "Team coordinator budget exhausted",
                actor_id=actor.identity_id,
            )
            return

        async def run() -> None:
            latest = self.store.get_delegation(
                record.delegation_id,
                organization_id=record.organization_id,
                workspace_id=record.workspace_id,
            )
            if (
                latest is None
                or latest.active_member_ids
                or latest.status
                in {
                    AgentTeamDelegationStatus.COMPLETED,
                    AgentTeamDelegationStatus.ESCALATED,
                }
            ):
                return
            leader, _ = self.profiles.resolve_for_execution(
                team.leader_profile_id,
                actor=actor,
                project_id=record.project_id,
                revision=team.leader_profile_revision,
            )
            candidates = [
                member
                for member in team.members
                if member.enabled
                and member.kind == AgentTeamMemberKind.AGENT
                and member.member_id
                not in set(getattr(latest, "failed_member_ids", ()))
            ]
            context = self._routing_context(
                team,
                latest,
                candidates,
            )
            context["failedMemberIds"] = list(
                getattr(latest, "failed_member_ids", ())
            )
            await self._dispatch_profile(
                profile_id=leader.profile_id,
                profile_revision=leader.revision,
                objective=(
                    "Re-coordinate this Team Work Item after member "
                    "results. Return only a structured delegation decision."
                ),
                project_id=record.project_id,
                actor=actor,
                context=context,
                coordinator=True,
            )
            pending = latest.model_copy(
                update={
                    "status": AgentTeamDelegationStatus.COORDINATOR_PENDING,
                    "updated_at": float(self.clock()),
                }
            )
            self.store.update_delegation(pending)

        self.coordination.schedule(
            f"agent-team-coordinate:{record.delegation_id}",
            run,
            revision=event_id,
            scope=record.project_id,
            timeout_seconds=120.0,
        )

    async def record_member_result(
        self,
        delegation_id: str,
        *,
        member_id: str,
        event_id: str,
        succeeded: bool,
        actor: AuthenticationActor,
    ) -> AgentTeamDelegationRecord:
        record = self.store.get_delegation(
            delegation_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if record is None:
            raise AgentTeamNotFound("team delegation not found")
        seen = tuple(getattr(record, "result_event_ids", ()))
        if event_id in seen:
            return record
        if member_id not in record.active_member_ids:
            return record

        active = tuple(
            value
            for value in record.active_member_ids
            if value != member_id
        )
        completed = tuple(
            dict.fromkeys(
                (
                    *getattr(record, "completed_member_ids", ()),
                    *((member_id,) if succeeded else ()),
                )
            )
        )
        failed = tuple(
            dict.fromkeys(
                (
                    *getattr(record, "failed_member_ids", ()),
                    *((member_id,) if not succeeded else ()),
                )
            )
        )
        status = (
            AgentTeamDelegationStatus.COMPLETED
            if not active and not failed
            else record.status
        )
        updated = record.model_copy(
            update={
                "active_member_ids": active,
                "completed_member_ids": completed,
                "failed_member_ids": failed,
                "result_event_ids": (
                    *seen,
                    event_id,
                )[-200:],
                "status": status,
                "updated_at": float(self.clock()),
            }
        )
        self.store.update_delegation(updated)
        self._record_work_item_event(
            updated,
            "work_item_team_member_result",
            actor_id=actor.identity_id,
            payload={
                "member_id": member_id,
                "succeeded": succeeded,
                "event_id": event_id,
            },
        )
        if not active and failed:
            await self._schedule_recoordination(
                updated,
                actor=actor,
                event_id=event_id,
            )
        return updated

    def usage(
        self,
        delegation_id: str,
        payload: AgentTeamUsageUpdate,
        *,
        actor: AuthenticationActor,
    ) -> AgentTeamDelegationRecord:
        record = self.store.get_delegation(
            delegation_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if record is None:
            raise AgentTeamNotFound("team delegation not found")
        updates = {
            field: getattr(record, field) + getattr(payload, field)
            for field in (
                "coordinator_input_tokens",
                "coordinator_output_tokens",
                "coordinator_cost_usd",
                "worker_input_tokens",
                "worker_output_tokens",
                "worker_cost_usd",
            )
        }
        updated = record.model_copy(
            update={
                **updates,
                "updated_at": float(self.clock()),
            }
        )
        return self.store.update_delegation(updated)

    def history(
        self,
        *,
        actor: AuthenticationActor,
        team_id: str | None = None,
        work_item_ref: str | None = None,
        limit: int = 50,
    ) -> list[AgentTeamDelegationRecord]:
        if team_id is not None:
            self.get(team_id, actor=actor)
        return self.store.list_delegations(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            team_id=team_id,
            work_item_ref=work_item_ref,
            limit=limit,
        )

    async def stop(self) -> None:
        await self.coordination.stop()
