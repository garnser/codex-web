from __future__ import annotations

import time
from typing import Any, Callable

from codex_web.agent_profiles import (
    AgentProfileAccessDecision,
    AgentProfileAccessMode,
    AgentProfileCreate,
    AgentProfileExecutionBinding,
    AgentProfileLifecycle,
    AgentProfileLifecycleChange,
    AgentProfileRevision,
    AgentProfileUpdate,
)
from codex_web.authority import AuthorityRoleCatalogDefinition
from codex_web.definitions import (
    DefinitionLifecycle,
    DefinitionReference,
    DefinitionScope,
    definition_is_effective,
    reference_for,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.agent_profiles import AgentProfileStore


class AgentProfileError(RuntimeError):
    pass


class AgentProfileNotFound(AgentProfileError):
    pass


class AgentProfileConflict(AgentProfileError):
    pass


class AgentProfileAccessDenied(AgentProfileError):
    def __init__(
        self,
        message: str,
        *,
        decision: AgentProfileAccessDecision | None = None,
    ) -> None:
        self.decision = decision
        super().__init__(message)


RoleResolver = Callable[
    [str, AuthenticationActor],
    DefinitionReference,
]


class AgentProfileService:
    def __init__(
        self,
        store: AgentProfileStore,
        *,
        definitions: DefinitionRegistryService,
        authority: Any | None = None,
        execution_profiles: Any | None = None,
        role_resolver: RoleResolver | None = None,
        assignment_history: Callable[
            [AuthenticationActor], list[Any]
        ] | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.definitions = definitions
        self.authority = authority
        self.execution_profiles = execution_profiles
        self.role_resolver = role_resolver
        self.assignment_history = assignment_history
        self.clock = clock

    @staticmethod
    def _is_admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "agent-profiles:admin" in actor.service_scopes
        return actor.has_role(
            MembershipRole.OWNER,
            MembershipRole.ADMIN,
        )

    @classmethod
    def _require_admin_or_owner(
        cls,
        actor: AuthenticationActor,
        profile: AgentProfileRevision | None = None,
        *,
        requested_owner: str | None = None,
    ) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "agent-profiles:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "agent-profiles:admin service scope required"
                )
            return
        if cls._is_admin(actor):
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
            return
        owner = (
            profile.owner_identity_id
            if profile is not None
            else requested_owner
        )
        if owner != actor.identity_id:
            raise AuthorizationError(
                "agent profile owner or tenant administrator required"
            )
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )

    @staticmethod
    def _same_scope(
        profile: AgentProfileRevision,
        actor: AuthenticationActor,
    ) -> bool:
        return (
            profile.organization_id == actor.organization_id
            and profile.workspace_id == actor.workspace_id
        )

    def _definition_ref(
        self,
        value: DefinitionReference | None,
        *,
        actor: AuthenticationActor,
        label: str,
    ) -> DefinitionReference | None:
        if value is None:
            return None
        record = self.definitions.get_record(value.record_id)
        actual = reference_for(record)
        if actual != value:
            raise AgentProfileConflict(
                f"{label} definition reference does not match canonical record"
            )
        if record.lifecycle != DefinitionLifecycle.PUBLISHED:
            raise AgentProfileConflict(
                f"{label} definition must be published"
            )
        if not definition_is_effective(record):
            raise AgentProfileConflict(
                f"{label} definition is not currently effective"
            )
        if (
            record.scope_type == DefinitionScope.ORGANIZATION
            and record.scope_id != actor.organization_id
        ):
            raise AgentProfileConflict(
                f"{label} definition is outside actor organization"
            )
        if (
            record.scope_type == DefinitionScope.WORKSPACE
            and record.scope_id != actor.workspace_id
        ):
            raise AgentProfileConflict(
                f"{label} definition is outside actor workspace"
            )
        if record.scope_type == DefinitionScope.PROJECT:
            raise AgentProfileConflict(
                f"{label} project-scoped definition cannot be attached "
                "to a reusable workspace Agent Profile"
            )
        return actual

    def _role_definition_ref(
        self,
        role_id: str | None,
        supplied: DefinitionReference | None,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionReference | None:
        if role_id is None:
            if supplied is not None:
                raise AgentProfileConflict(
                    "role definition reference requires role_id"
                )
            return None
        if supplied is not None:
            return self._definition_ref(
                supplied,
                actor=actor,
                label="role",
            )
        if self.role_resolver is None:
            raise AgentProfileConflict(
                "role resolver is unavailable"
            )
        return self.role_resolver(role_id, actor)

    def _authority_definition_ref(
        self,
        role_id: str | None,
        supplied: DefinitionReference | None,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionReference | None:
        if role_id is None:
            if supplied is not None:
                raise AgentProfileConflict(
                    "authority definition reference requires authority_role_id"
                )
            return None
        if self.authority is None:
            raise AgentProfileConflict(
                "canonical authority service is unavailable"
            )
        record = self.authority.catalog_record(actor=actor)
        catalog = AuthorityRoleCatalogDefinition.model_validate(
            record.payload
        )
        if role_id not in {item.id for item in catalog.roles}:
            raise AgentProfileConflict(
                f"unknown authority role: {role_id}"
            )
        actual = reference_for(record)
        if supplied is not None and supplied != actual:
            raise AgentProfileConflict(
                "authority definition reference is not the active canonical catalog"
            )
        return actual

    def _execution_profile(
        self,
        profile_id: str | None,
        *,
        actor: AuthenticationActor,
    ) -> str | None:
        if profile_id is None:
            return None
        if self.execution_profiles is None:
            raise AgentProfileConflict(
                "execution profile resolver is unavailable"
            )
        self.execution_profiles.resolve(
            profile_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        return profile_id

    def _normalized_refs(
        self,
        refs: tuple[DefinitionReference, ...],
        *,
        actor: AuthenticationActor,
    ) -> tuple[DefinitionReference, ...]:
        values = tuple(
            self._definition_ref(
                item,
                actor=actor,
                label="skill",
            )
            for item in refs
        )
        normalized = tuple(
            item for item in values if item is not None
        )
        invalid = [
            item
            for item in normalized
            if item.kind != "agent.skill"
        ]
        if invalid:
            raise AgentProfileConflict(
                "Agent Profile skill_refs must reference agent.skill "
                "Definition records"
            )
        return normalized

    def _latest(
        self,
        profile_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AgentProfileRevision:
        item = self.store.latest(
            profile_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if item is None:
            raise AgentProfileNotFound(
                "agent profile not found"
            )
        return item

    def get(
        self,
        profile_id: str,
        *,
        actor: AuthenticationActor,
        revision: int | None = None,
        require_visible: bool = True,
    ) -> AgentProfileRevision:
        item = (
            self.store.revision(
                profile_id,
                revision,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
            if revision is not None
            else self._latest(profile_id, actor=actor)
        )
        if item is None:
            raise AgentProfileNotFound(
                "agent profile revision not found"
            )
        if not self._same_scope(item, actor):
            raise AgentProfileNotFound(
                "agent profile not found"
            )
        if require_visible and not self.can_view(
            item,
            actor=actor,
        ):
            raise AgentProfileNotFound(
                "agent profile not found"
            )
        return item

    def revisions(
        self,
        profile_id: str,
        *,
        actor: AuthenticationActor,
    ) -> list[AgentProfileRevision]:
        latest = self._latest(profile_id, actor=actor)
        if not self.can_view(latest, actor=actor):
            raise AgentProfileNotFound(
                "agent profile not found"
            )
        return self.store.list_revisions(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            profile_id=profile_id,
        )

    def can_view(
        self,
        profile: AgentProfileRevision,
        *,
        actor: AuthenticationActor,
    ) -> bool:
        if not self._same_scope(profile, actor):
            return False
        if self._is_admin(actor):
            return True
        if profile.owner_identity_id == actor.identity_id:
            return True
        return self.access_decision(
            profile,
            actor=actor,
        ).allowed

    def list(
        self,
        *,
        actor: AuthenticationActor,
        include_archived: bool = False,
    ) -> list[AgentProfileRevision]:
        revisions = self.store.list_revisions(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        latest: dict[str, AgentProfileRevision] = {}
        for item in revisions:
            latest[item.profile_id] = item
        values = [
            item
            for item in latest.values()
            if (
                include_archived
                or item.lifecycle
                != AgentProfileLifecycle.ARCHIVED
            )
            and self.can_view(item, actor=actor)
        ]
        return sorted(values, key=lambda item: (item.name.casefold(), item.profile_id))

    def _actor_authority_roles(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None = None,
    ) -> tuple[str, ...]:
        if self.authority is None:
            return ()
        try:
            return self.authority.role_ids_for_actor(
                actor,
                project_id=project_id,
            )
        except Exception:
            return ()

    def access_decision(
        self,
        profile: AgentProfileRevision,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
    ) -> AgentProfileAccessDecision:
        roles = self._actor_authority_roles(
            actor,
            project_id=project_id,
        )
        reasons: list[str] = []
        allowed = False
        if not self._same_scope(profile, actor):
            reasons.append("profile_outside_actor_tenant")
        elif profile.lifecycle != AgentProfileLifecycle.ACTIVE:
            reasons.append(
                f"profile_{profile.lifecycle.value}"
            )
        elif profile.owner_identity_id == actor.identity_id:
            allowed = True
            reasons.append("profile_owner")
        elif profile.access.mode == AgentProfileAccessMode.TENANT:
            allowed = True
            reasons.append("tenant_access")
        elif profile.access.mode == AgentProfileAccessMode.OWNER:
            reasons.append("owner_only")
        else:
            identity_allowed = (
                actor.identity_id in profile.access.identity_ids
            )
            role_allowed = bool(
                set(profile.access.role_ids) & set(roles)
            )
            allowed = identity_allowed or role_allowed
            reasons.append(
                "identity_allowlist"
                if identity_allowed
                else (
                    "role_allowlist"
                    if role_allowed
                    else "not_in_profile_allowlist"
                )
            )

        if (
            allowed
            and profile.authority_role_id
            and profile.authority_role_id not in roles
        ):
            allowed = False
            reasons.append(
                "required_authority_role_missing"
            )

        return AgentProfileAccessDecision(
            allowed=allowed,
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            actor_identity_id=actor.identity_id,
            actor_role_ids=roles,
            required_authority_role_id=(
                profile.authority_role_id
            ),
            reasons=tuple(reasons),
        )

    def _append_revision(
        self,
        current: AgentProfileRevision,
        *,
        actor: AuthenticationActor,
        updates: dict[str, Any],
        reason: str,
    ) -> AgentProfileRevision:
        now = float(self.clock())
        next_record = current.model_copy(
            update={
                **updates,
                "record_id": (
                    f"agent-profile-rev-"
                    f"{__import__('uuid').uuid4().hex}"
                ),
                "revision": current.revision + 1,
                "updated_by": actor.identity_id,
                "updated_at": now,
                "change_reason": reason,
            }
        )
        return self.store.append(next_record)

    def create(
        self,
        payload: AgentProfileCreate,
        *,
        actor: AuthenticationActor,
    ) -> AgentProfileRevision:
        owner = payload.owner_identity_id or actor.identity_id
        self._require_admin_or_owner(
            actor,
            requested_owner=owner,
        )
        if self.store.latest(
            payload.profile_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        ) is not None:
            raise AgentProfileConflict(
                "agent profile already exists"
            )
        now = float(self.clock())
        record = AgentProfileRevision(
            profile_id=payload.profile_id,
            revision=1,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            name=payload.name,
            avatar_ref=payload.avatar_ref,
            description=payload.description,
            owner_identity_id=owner,
            created_by=actor.identity_id,
            updated_by=actor.identity_id,
            role_id=payload.role_id,
            role_definition_ref=self._role_definition_ref(
                payload.role_id,
                payload.role_definition_ref,
                actor=actor,
            ),
            instructions_ref=self._definition_ref(
                payload.instructions_ref,
                actor=actor,
                label="instructions",
            ),
            skill_refs=self._normalized_refs(
                payload.skill_refs,
                actor=actor,
            ),
            access=payload.access,
            authority_role_id=payload.authority_role_id,
            authority_definition_ref=self._authority_definition_ref(
                payload.authority_role_id,
                payload.authority_definition_ref,
                actor=actor,
            ),
            runtime_policy=payload.runtime_policy,
            model_policy=payload.model_policy,
            execution_profile_id=self._execution_profile(
                payload.execution_profile_id,
                actor=actor,
            ),
            sandbox_requirement=payload.sandbox_requirement,
            budgets=payload.budgets,
            change_reason=payload.reason,
            created_at=now,
            updated_at=now,
        )
        return self.store.append(record)

    def update(
        self,
        profile_id: str,
        payload: AgentProfileUpdate,
        *,
        actor: AuthenticationActor,
    ) -> AgentProfileRevision:
        current = self._latest(profile_id, actor=actor)
        self._require_admin_or_owner(actor, current)

        fields = payload.model_fields_set
        updates: dict[str, Any] = {}
        for field_name in (
            "name",
            "avatar_ref",
            "description",
            "owner_identity_id",
            "access",
            "runtime_policy",
            "model_policy",
            "sandbox_requirement",
            "budgets",
        ):
            if field_name in fields:
                updates[field_name] = getattr(
                    payload,
                    field_name,
                )

        if (
            "role_id" in fields
            or "role_definition_ref" in fields
        ):
            role_id = (
                payload.role_id
                if "role_id" in fields
                else current.role_id
            )
            supplied = (
                payload.role_definition_ref
                if "role_definition_ref" in fields
                else None
            )
            updates["role_id"] = role_id
            updates["role_definition_ref"] = (
                self._role_definition_ref(
                    role_id,
                    supplied,
                    actor=actor,
                )
            )

        if "instructions_ref" in fields:
            updates["instructions_ref"] = self._definition_ref(
                payload.instructions_ref,
                actor=actor,
                label="instructions",
            )

        if "skill_refs" in fields:
            updates["skill_refs"] = self._normalized_refs(
                payload.skill_refs or (),
                actor=actor,
            )

        if (
            "authority_role_id" in fields
            or "authority_definition_ref" in fields
        ):
            authority_role_id = (
                payload.authority_role_id
                if "authority_role_id" in fields
                else current.authority_role_id
            )
            supplied = (
                payload.authority_definition_ref
                if "authority_definition_ref" in fields
                else None
            )
            updates["authority_role_id"] = authority_role_id
            updates["authority_definition_ref"] = (
                self._authority_definition_ref(
                    authority_role_id,
                    supplied,
                    actor=actor,
                )
            )

        if "execution_profile_id" in fields:
            updates["execution_profile_id"] = (
                self._execution_profile(
                    payload.execution_profile_id,
                    actor=actor,
                )
            )

        owner = updates.get(
            "owner_identity_id",
            current.owner_identity_id,
        )
        if owner != current.owner_identity_id:
            self._require_admin_or_owner(actor, current)
            if not self._is_admin(actor):
                raise AuthorizationError(
                    "only tenant administrators may transfer profile ownership"
                )

        return self._append_revision(
            current,
            actor=actor,
            updates=updates,
            reason=payload.reason,
        )

    def lifecycle(
        self,
        profile_id: str,
        lifecycle: AgentProfileLifecycle,
        payload: AgentProfileLifecycleChange,
        *,
        actor: AuthenticationActor,
    ) -> AgentProfileRevision:
        current = self._latest(profile_id, actor=actor)
        self._require_admin_or_owner(actor, current)
        if current.lifecycle == lifecycle:
            return current
        return self._append_revision(
            current,
            actor=actor,
            updates={"lifecycle": lifecycle},
            reason=payload.reason,
        )

    def resolve_for_execution(
        self,
        profile_id: str,
        *,
        actor: AuthenticationActor,
        project_id: str,
        revision: int | None = None,
    ) -> tuple[
        AgentProfileRevision,
        AgentProfileAccessDecision,
    ]:
        # Current lifecycle/access governs whether new work may start. An
        # explicit older configuration revision can be replayed only while the
        # logical profile itself remains active and invokable.
        current = self._latest(profile_id, actor=actor)
        decision = self.access_decision(
            current,
            actor=actor,
            project_id=project_id,
        )
        if not decision.allowed:
            raise AgentProfileAccessDenied(
                "agent profile cannot be invoked",
                decision=decision,
            )
        profile = (
            self.get(
                profile_id,
                actor=actor,
                revision=revision,
                require_visible=False,
            )
            if revision is not None
            else current
        )
        return profile, decision

    def execution_history(
        self,
        profile_id: str,
        *,
        actor: AuthenticationActor,
        limit: int = 20,
    ) -> dict[str, Any]:
        current = self._latest(profile_id, actor=actor)
        if not self.can_view(current, actor=actor):
            raise AgentProfileNotFound(
                "agent profile not found"
            )
        if self.assignment_history is None:
            return {
                "items": [],
                "count": 0,
                "activeCount": 0,
                "available": False,
            }
        assignments = [
            item
            for item in self.assignment_history(actor)
            if getattr(item, "agent_profile", None) is not None
            and item.agent_profile.profile_id == profile_id
        ]
        assignments.sort(
            key=lambda item: (
                float(getattr(item, "updated_at", 0.0)),
                str(getattr(item, "id", "")),
            ),
            reverse=True,
        )
        bounded = assignments[: max(1, min(int(limit), 100))]
        active_statuses = {"pending", "claimed", "running"}
        return {
            "items": [
                {
                    "assignmentId": item.id,
                    "executionId": item.execution_id,
                    "projectId": item.project_id,
                    "status": item.status.value,
                    "profileRevision": (
                        item.agent_profile.profile_revision
                    ),
                    "providerId": (
                        item.agent_profile.selected_provider_id
                    ),
                    "runtimeId": (
                        item.agent_profile.selected_runtime_id
                    ),
                    "workerId": item.assigned_worker_id,
                    "createdAt": item.created_at,
                    "updatedAt": item.updated_at,
                    "completedAt": item.completed_at,
                }
                for item in bounded
            ],
            "count": len(assignments),
            "activeCount": sum(
                item.status.value in active_statuses
                for item in assignments
            ),
            "available": True,
        }

    @staticmethod
    def binding_for(
        profile: AgentProfileRevision,
        *,
        selected_provider_id: str | None = None,
        selected_runtime_id: str | None = None,
        selected_provider_revision: int | None = None,
        selected_runtime_capability_revision: int | None = None,
        selected_worker_id: str | None = None,
        model_provider_id: str | None = None,
        model_id: str | None = None,
    ) -> AgentProfileExecutionBinding:
        return AgentProfileExecutionBinding(
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            profile_record_id=profile.record_id,
            instructions_ref=profile.instructions_ref,
            skill_refs=profile.skill_refs,
            role_id=profile.role_id,
            role_definition_ref=profile.role_definition_ref,
            authority_role_id=profile.authority_role_id,
            authority_definition_ref=profile.authority_definition_ref,
            execution_profile_id=profile.execution_profile_id,
            sandbox_requirement=profile.sandbox_requirement,
            selected_provider_id=selected_provider_id,
            selected_runtime_id=selected_runtime_id,
            selected_provider_revision=selected_provider_revision,
            selected_runtime_capability_revision=(
                selected_runtime_capability_revision
            ),
            selected_worker_id=selected_worker_id,
            model_provider_id=model_provider_id,
            model_id=model_id,
        )
