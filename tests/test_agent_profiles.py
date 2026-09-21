from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.agent_profiles import (
    AgentProfileAccessMode,
    AgentProfileAccessPolicy,
    AgentProfileCreate,
    AgentProfileLifecycle,
    AgentProfileLifecycleChange,
    AgentProfileModelPolicy,
    AgentProfileRuntimePolicy,
    AgentProfileUpdate,
)
from codex_web.agent_providers import (
    AgentProviderCapability,
    AgentProviderHealth,
    AgentProviderUpsert,
)
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    reference_for,
)
from codex_web.agent_runtime import AgentRuntimeHealth
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
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_routing import (
    AgentRoutingBlockedError,
    AgentRoutingService,
)
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor(
    identity_id: str,
    *,
    organization_id: str = "org-a",
    workspace_id: str = "workspace-a",
    admin: bool = False,
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id=identity_id,
        principal_kind=PrincipalKind.HUMAN,
        organization_id=organization_id,
        workspace_id=workspace_id,
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


class _ExecutionProfiles:
    def resolve(
        self,
        profile_id,
        *,
        organization_id=None,
        workspace_id=None,
        project_id=None,
    ):
        del organization_id, workspace_id, project_id
        if profile_id != "repository-write":
            raise ValueError("unknown execution profile")
        return object(), object()


class _Runtime:
    runtime_type = "test-runtime"

    def __init__(
        self,
        provider_id: str,
        runtime_id: str,
        capabilities: tuple[AgentProviderCapability, ...],
        *,
        health: AgentRuntimeHealth = AgentRuntimeHealth.HEALTHY,
    ) -> None:
        self.provider_id = provider_id
        self.runtime_id = runtime_id
        self.capabilities = capabilities
        self._health = health

    async def health(self) -> AgentRuntimeHealth:
        return self._health


class AgentProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.definitions = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        for kind in (
            "agent.instructions",
            "agent.skill",
        ):
            self.definitions.register_schema(
                DefinitionKindSchema(
                    kind=kind,
                    schema_version="1.0",
                    validate=lambda payload: dict(payload),
                )
            )
        self.authority = _Authority()
        self.profiles = AgentProfileService(
            AgentProfileStore(self.sqlite),
            definitions=self.definitions,
            authority=self.authority,
            execution_profiles=_ExecutionProfiles(),
        )
        self.admin = _actor("admin-a", admin=True)
        self.member = _actor("member-a")
        self.other = _actor("member-b")
        self.foreign = _actor(
            "foreign",
            organization_id="org-b",
            workspace_id="workspace-b",
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _publish_definition(
        self,
        *,
        definition_id: str,
        kind: str,
        payload: dict,
        expected_active_revision: int | None = None,
    ):
        draft = self.definitions.create_draft(
            DefinitionDraftCreate(
                definition_id=definition_id,
                kind=kind,
                definition_schema_version="1.0",
                payload=payload,
                actor=self.admin.identity_id,
            )
        )
        return self.definitions.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=self.admin.identity_id,
                expected_active_revision=expected_active_revision,
            ),
        )

    def _create(
        self,
        profile_id: str = "coder",
        **updates,
    ):
        data = {
            "profile_id": profile_id,
            "name": "Coder",
            "description": "Stable coding collaborator",
            "execution_profile_id": "repository-write",
        }
        data.update(updates)
        return self.profiles.create(
            AgentProfileCreate(**data),
            actor=self.admin,
        )

    async def test_revision_changes_keep_stable_logical_identity(self) -> None:
        first = self._create()
        second = self.profiles.update(
            "coder",
            AgentProfileUpdate(
                name="Senior Coder",
                runtime_policy=AgentProfileRuntimePolicy(
                    preferred_provider_ids=("provider-b",),
                ),
                reason="prefer alternate runtime",
            ),
            actor=self.admin,
        )

        self.assertEqual(first.profile_id, second.profile_id)
        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(first.record_id, second.record_id)
        revisions = self.profiles.revisions(
            "coder",
            actor=self.admin,
        )
        self.assertEqual(
            [item.revision for item in revisions],
            [1, 2],
        )
        self.assertEqual(revisions[0].name, "Coder")
        self.assertEqual(revisions[1].name, "Senior Coder")

    async def test_profile_access_is_independent_and_fail_closed(self) -> None:
        self._create(
            access=AgentProfileAccessPolicy(
                mode=AgentProfileAccessMode.ALLOWLIST,
                identity_ids=("member-a",),
            )
        )

        profile, decision = self.profiles.resolve_for_execution(
            "coder",
            actor=self.member,
            project_id="project-a",
        )
        self.assertEqual(profile.profile_id, "coder")
        self.assertTrue(decision.allowed)

        with self.assertRaises(AgentProfileAccessDenied) as denied:
            self.profiles.resolve_for_execution(
                "coder",
                actor=self.other,
                project_id="project-a",
            )
        self.assertFalse(denied.exception.decision.allowed)
        self.assertIn(
            "not_in_profile_allowlist",
            denied.exception.decision.reasons,
        )

    async def test_authority_ceiling_reference_never_grants_missing_role(self) -> None:
        # The profile access policy permits the tenant, but canonical authority
        # is a separate requirement. The profile cannot synthesize that Role.
        self.authority.roles[self.member.identity_id] = ()
        self._create(
            authority_role_id=None,
        )
        current = self.profiles._latest(
            "coder",
            actor=self.admin,
        )
        # Model-copy a persisted revision to isolate the access rule without
        # needing a full authority Definition fixture in this unit test.
        restricted = current.model_copy(
            update={"authority_role_id": "deploy-role"}
        )
        decision = self.profiles.access_decision(
            restricted,
            actor=self.member,
            project_id="project-a",
        )
        self.assertFalse(decision.allowed)
        self.assertIn(
            "required_authority_role_missing",
            decision.reasons,
        )

        self.authority.roles[self.member.identity_id] = (
            "deploy-role",
        )
        allowed = self.profiles.access_decision(
            restricted,
            actor=self.member,
            project_id="project-a",
        )
        self.assertTrue(allowed)

    async def test_archive_blocks_new_execution_even_with_old_revision(self) -> None:
        first = self._create()
        archived = self.profiles.lifecycle(
            "coder",
            lifecycle=__import__(
                "codex_web.agent_profiles",
                fromlist=["AgentProfileLifecycle"],
            ).AgentProfileLifecycle.ARCHIVED,
            payload=AgentProfileLifecycleChange(
                reason="retired collaborator",
            ),
            actor=self.admin,
        )
        self.assertEqual(archived.revision, 2)

        with self.assertRaises(AgentProfileAccessDenied):
            self.profiles.resolve_for_execution(
                "coder",
                actor=self.member,
                project_id="project-a",
                revision=first.revision,
            )

        restored = self.profiles.lifecycle(
            "coder",
            lifecycle=__import__(
                "codex_web.agent_profiles",
                fromlist=["AgentProfileLifecycle"],
            ).AgentProfileLifecycle.ACTIVE,
            payload=AgentProfileLifecycleChange(
                reason="needed again",
            ),
            actor=self.admin,
        )
        self.assertEqual(restored.revision, 3)
        replay, _ = self.profiles.resolve_for_execution(
            "coder",
            actor=self.member,
            project_id="project-a",
            revision=1,
        )
        self.assertEqual(replay.revision, 1)

    async def test_tenant_isolation_hides_foreign_profiles(self) -> None:
        self._create()

        self.assertEqual(
            self.profiles.list(actor=self.foreign),
            [],
        )
        with self.assertRaises(AgentProfileNotFound):
            self.profiles.get(
                "coder",
                actor=self.foreign,
            )

    async def test_owner_can_modify_own_profile_but_not_another(self) -> None:
        owned = self.profiles.create(
            AgentProfileCreate(
                profile_id="member-agent",
                name="Member Agent",
            ),
            actor=self.member,
        )
        updated = self.profiles.update(
            owned.profile_id,
            AgentProfileUpdate(
                description="owner update",
                reason="clarify purpose",
            ),
            actor=self.member,
        )
        self.assertEqual(updated.description, "owner update")

        with self.assertRaises(Exception):
            self.profiles.update(
                owned.profile_id,
                AgentProfileUpdate(
                    description="other update",
                    reason="should fail",
                ),
                actor=self.other,
            )

    async def test_profile_routing_fallback_preserves_identity_and_revision(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
        )
        provider_service = AgentProviderService(
            AgentProviderStore(self.sqlite)
        )
        runtimes = AgentRuntimeRegistry()
        for provider_id in ("provider-a", "provider-b"):
            provider_service.upsert(
                AgentProviderUpsert(
                    id=provider_id,
                    display_name=provider_id,
                    declared_capabilities=capabilities,
                    granted_capabilities=capabilities,
                    health=AgentProviderHealth.HEALTHY,
                ),
                actor=self.admin,
            )
        runtimes.register(
            _Runtime(
                "provider-a",
                "runtime-a",
                capabilities,
                health=AgentRuntimeHealth.UNAVAILABLE,
            )
        )
        runtimes.register(
            _Runtime(
                "provider-b",
                "runtime-b",
                capabilities,
            )
        )
        first = self._create(
            runtime_policy=AgentProfileRuntimePolicy(
                preferred_provider_ids=(
                    "provider-a",
                    "provider-b",
                ),
                allow_fallback=True,
            ),
            model_policy=AgentProfileModelPolicy(
                model_class="coding",
            ),
        )
        routing = AgentRoutingService(
            provider_service,
            runtimes,
            profiles=self.profiles,
        )

        result = await routing.route(
            AgentRoutingRequest(
                project_id="project-a",
                agent_profile_id="coder",
            ),
            actor=self.member,
        )

        self.assertEqual(
            result.selected_runtime.provider_id,
            "provider-b",
        )
        self.assertIsNotNone(result.agent_profile)
        self.assertEqual(
            result.agent_profile.profile_id,
            first.profile_id,
        )
        self.assertEqual(
            result.agent_profile.profile_revision,
            1,
        )
        self.assertEqual(
            result.agent_profile.selected_provider_id,
            "provider-b",
        )
        self.assertEqual(
            result.agent_profile.selected_runtime_id,
            "runtime-b",
        )

        second = self.profiles.update(
            "coder",
            AgentProfileUpdate(
                runtime_policy=AgentProfileRuntimePolicy(
                    preferred_provider_ids=("provider-b",),
                ),
                reason="change runtime preference",
            ),
            actor=self.admin,
        )
        next_result = await routing.route(
            AgentRoutingRequest(
                project_id="project-a",
                agent_profile_id="coder",
            ),
            actor=self.member,
        )
        self.assertEqual(
            next_result.agent_profile.profile_id,
            first.profile_id,
        )
        self.assertEqual(
            next_result.agent_profile.profile_revision,
            second.revision,
        )
        # The old route result remains immutable historical attribution.
        self.assertEqual(result.agent_profile.profile_revision, 1)

    async def test_instruction_and_skill_revisions_are_pinned_in_execution_binding(self) -> None:
        instructions_v1 = self._publish_definition(
            definition_id="coder.instructions",
            kind="agent.instructions",
            payload={"text": "Use the repository contract."},
        )
        skill_v1 = self._publish_definition(
            definition_id="python",
            kind="agent.skill",
            payload={"name": "Python"},
        )
        first = self._create(
            instructions_ref=reference_for(instructions_v1),
            skill_refs=(reference_for(skill_v1),),
        )
        binding = self.profiles.binding_for(
            first,
            selected_provider_id="provider-a",
            selected_runtime_id="runtime-a",
            selected_provider_revision=2,
            selected_runtime_capability_revision=5,
        )

        instructions_v2 = self._publish_definition(
            definition_id="coder.instructions",
            kind="agent.instructions",
            payload={"text": "New instructions for future profiles."},
            expected_active_revision=instructions_v1.revision,
        )
        second = self.profiles.update(
            "coder",
            AgentProfileUpdate(
                description="metadata-only profile revision",
                reason="clarify collaborator purpose",
            ),
            actor=self.admin,
        )

        self.assertNotEqual(
            instructions_v1.record_id,
            instructions_v2.record_id,
        )
        self.assertEqual(
            first.instructions_ref.record_id,
            instructions_v1.record_id,
        )
        self.assertEqual(
            second.instructions_ref.record_id,
            instructions_v1.record_id,
        )
        self.assertEqual(
            binding.instructions_ref.record_id,
            instructions_v1.record_id,
        )
        self.assertEqual(
            binding.skill_refs[0].record_id,
            skill_v1.record_id,
        )
        self.assertEqual(binding.profile_revision, first.revision)

    async def test_disabled_profile_cannot_receive_new_execution(self) -> None:
        self._create()
        disabled = self.profiles.lifecycle(
            "coder",
            lifecycle=AgentProfileLifecycle.DISABLED,
            payload=AgentProfileLifecycleChange(
                reason="temporarily unavailable",
            ),
            actor=self.admin,
        )
        self.assertEqual(
            disabled.lifecycle,
            AgentProfileLifecycle.DISABLED,
        )

        with self.assertRaises(AgentProfileAccessDenied) as denied:
            self.profiles.resolve_for_execution(
                "coder",
                actor=self.member,
                project_id="project-a",
            )
        self.assertIn(
            "profile_disabled",
            denied.exception.decision.reasons,
        )

    async def test_unavailable_profile_runtime_returns_structured_blocker(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
        )
        provider_service = AgentProviderService(
            AgentProviderStore(self.sqlite)
        )
        provider_service.upsert(
            AgentProviderUpsert(
                id="provider-a",
                display_name="provider-a",
                declared_capabilities=capabilities,
                granted_capabilities=capabilities,
                health=AgentProviderHealth.HEALTHY,
            ),
            actor=self.admin,
        )
        self._create(
            runtime_policy=AgentProfileRuntimePolicy(
                allowed_provider_ids=("provider-a",),
                allowed_runtime_ids=("runtime-missing",),
            ),
        )
        routing = AgentRoutingService(
            provider_service,
            AgentRuntimeRegistry(),
            profiles=self.profiles,
        )

        with self.assertRaises(AgentRoutingBlockedError) as blocked:
            await routing.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    agent_profile_id="coder",
                ),
                actor=self.member,
            )

        detail = blocked.exception.public()
        self.assertEqual(
            detail["code"],
            "agent_runtime_unavailable",
        )
        self.assertEqual(
            detail["target_type"],
            "agent_profile",
        )
        self.assertEqual(detail["target_id"], "coder")
        self.assertFalse(detail["retryable"])
        self.assertEqual(
            detail["remediation_route"],
            "/api/agent-profiles",
        )

    async def test_profile_allowlist_cannot_be_broadened_by_route_request(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
        )
        provider_service = AgentProviderService(
            AgentProviderStore(self.sqlite)
        )
        runtimes = AgentRuntimeRegistry()
        for provider_id in ("provider-a", "provider-b"):
            provider_service.upsert(
                AgentProviderUpsert(
                    id=provider_id,
                    display_name=provider_id,
                    declared_capabilities=capabilities,
                    granted_capabilities=capabilities,
                ),
                actor=self.admin,
            )
            runtimes.register(
                _Runtime(
                    provider_id,
                    f"runtime-{provider_id[-1]}",
                    capabilities,
                )
            )
        self._create(
            runtime_policy=AgentProfileRuntimePolicy(
                allowed_provider_ids=("provider-a",),
            ),
        )
        routing = AgentRoutingService(
            provider_service,
            runtimes,
            profiles=self.profiles,
        )

        result = await routing.route(
            AgentRoutingRequest(
                project_id="project-a",
                agent_profile_id="coder",
                allowed_provider_ids=(
                    "provider-a",
                    "provider-b",
                ),
            ),
            actor=self.member,
        )
        self.assertEqual(
            result.selected_runtime.provider_id,
            "provider-a",
        )


if __name__ == "__main__":
    unittest.main()
