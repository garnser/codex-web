from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from codex_web.agent_profiles import (
    AgentProfileCreate,
    AgentProfileExecutionBinding,
)
from codex_web.agent_providers import (
    AgentProviderCapability,
    AgentProviderHealth,
    AgentProviderUpsert,
)
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.agent_runtime import AgentRuntimeHealth
from codex_web.definitions import (
    DefinitionLifecycle,
    DefinitionScope,
    reference_for,
)
from codex_web.execution_workers import WorkerCapability
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.agent_profiles import (
    AgentProfileConflict,
    AgentProfileService,
)
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_routing import AgentRoutingError, AgentRoutingService
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.services.skills import (
    SkillNotFoundError,
    install_skill_definitions,
)
from codex_web.skills import (
    SkillAsset,
    SkillAssetKind,
    SkillAssetSecurity,
    SkillBundle,
    SkillDefinition,
    SkillHelperExecutionPolicy,
    SkillSourceKind,
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


class _Runtime:
    runtime_type = "test-runtime"

    def __init__(
        self,
        provider_id: str,
        runtime_id: str,
        capabilities: tuple[AgentProviderCapability, ...],
    ) -> None:
        self.provider_id = provider_id
        self.runtime_id = runtime_id
        self.capabilities = capabilities

    async def health(self) -> AgentRuntimeHealth:
        return AgentRuntimeHealth.HEALTHY


class SkillServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.registry = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.profile_store = AgentProfileStore(self.sqlite)
        self.profiles = AgentProfileService(
            self.profile_store,
            definitions=self.registry,
        )
        self.skills = install_skill_definitions(
            self.registry,
            profiles=self.profiles,
        )
        self.admin = _actor("admin", admin=True)
        self.member = _actor("member")
        self.foreign = _actor(
            "foreign",
            organization_id="org-b",
            workspace_id="workspace-b",
            admin=True,
        )

        def usage(reference):
            items = []
            for profile in self.profile_store.load().revisions:
                if not any(
                    item.record_id == reference.record_id
                    for item in profile.skill_refs
                ):
                    continue
                items.append(
                    {
                        "object_type": "agent_profile",
                        "object_id": profile.profile_id,
                        "revision": profile.revision,
                        "organization_id": profile.organization_id,
                        "workspace_id": profile.workspace_id,
                    }
                )
            return items

        self.registry.register_usage_provider(usage)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _skill(
        self,
        body: str = "Follow the tested procedure.",
        *,
        name: str = "Deploy safely",
        provider_capabilities: tuple[str, ...] = (),
        worker_capabilities: tuple[str, ...] = (),
        assets: tuple[SkillAsset, ...] = (),
    ) -> SkillDefinition:
        return SkillDefinition(
            name=name,
            description="Reusable verified procedure",
            body=body,
            tags=("release", "safe"),
            applicability=("deployment",),
            assets=assets,
            required_provider_capabilities=provider_capabilities,
            required_worker_capabilities=worker_capabilities,
        )

    def _published(
        self,
        skill_id: str = "deploy.safe",
        *,
        body: str = "Follow the tested procedure.",
        provider_capabilities: tuple[str, ...] = (),
        worker_capabilities: tuple[str, ...] = (),
        assets: tuple[SkillAsset, ...] = (),
    ):
        draft = self.skills.create_draft(
            skill_id=skill_id,
            skill=self._skill(
                body,
                provider_capabilities=provider_capabilities,
                worker_capabilities=worker_capabilities,
                assets=assets,
            ),
            actor=self.admin,
        )
        return self.skills.publish(
            draft.record_id,
            actor=self.admin,
            reason="reviewed",
        )

    async def test_skill_revision_publish_supersede_rollback_and_archive(self) -> None:
        first = self._published(body="version one")
        second_draft = self.skills.revise(
            first.record_id,
            skill=self._skill("version two"),
            actor=self.admin,
            reason="improve procedure",
        )
        second = self.skills.publish(
            second_draft.record_id,
            actor=self.admin,
            reason="reviewed v2",
            expected_active_revision=first.revision,
        )

        history = self.skills.revisions(
            "deploy.safe",
            actor=self.admin,
        )
        old = next(item for item in history if item.record_id == first.record_id)
        self.assertEqual(old.lifecycle, DefinitionLifecycle.SUPERSEDED)
        self.assertEqual(second.revision, 2)

        rollback = self.skills.rollback(
            skill_id="deploy.safe",
            target_revision=1,
            actor=self.admin,
            reason="v2 regression",
        )
        self.assertEqual(rollback.lifecycle, DefinitionLifecycle.PUBLISHED)
        self.assertEqual(rollback.revision, 3)
        self.assertEqual(
            self.skills.definition(rollback).body,
            "version one",
        )

        archived = self.skills.archive(
            rollback.record_id,
            actor=self.admin,
            reason="retired procedure",
        )
        self.assertEqual(archived.lifecycle, DefinitionLifecycle.DEPRECATED)

    async def test_profile_attachment_pins_exact_revision_across_supersession(self) -> None:
        first = self._published(body="stable v1")
        alpha = self.profiles.create(
            AgentProfileCreate(
                profile_id="alpha",
                name="Alpha",
            ),
            actor=self.admin,
        )
        beta = self.profiles.create(
            AgentProfileCreate(
                profile_id="beta",
                name="Beta",
            ),
            actor=self.admin,
        )
        alpha_attached = self.skills.attach(
            alpha.profile_id,
            first.record_id,
            actor=self.admin,
            reason="attach stable procedure",
        )
        beta_attached = self.skills.attach(
            beta.profile_id,
            first.record_id,
            actor=self.admin,
            reason="attach stable procedure",
        )
        alpha_binding_v1 = self.profiles.binding_for(alpha_attached)

        v2_draft = self.skills.revise(
            first.record_id,
            skill=self._skill("stable v2"),
            actor=self.admin,
            reason="new revision",
        )
        second = self.skills.publish(
            v2_draft.record_id,
            actor=self.admin,
            reason="publish v2",
            expected_active_revision=first.revision,
        )

        # Existing profile/binding remains pinned to v1 after supersession.
        self.assertEqual(
            alpha_binding_v1.skill_refs[0].record_id,
            first.record_id,
        )
        context_v1 = self.skills.context_for(
            alpha_binding_v1.skill_refs,
        )
        self.assertEqual(context_v1[0].body, "stable v1")

        alpha_v2 = self.skills.attach(
            alpha.profile_id,
            second.record_id,
            actor=self.admin,
            reason="adopt v2",
        )
        alpha_binding_v2 = self.profiles.binding_for(alpha_v2)
        self.assertEqual(len(alpha_binding_v2.skill_refs), 1)
        self.assertEqual(
            alpha_binding_v2.skill_refs[0].record_id,
            second.record_id,
        )
        self.assertEqual(
            beta_attached.skill_refs[0].record_id,
            first.record_id,
        )

        usage = self.skills.usage(first.record_id, actor=self.admin)
        used_by = {item["object_id"] for item in usage["items"]}
        self.assertEqual(used_by, {"alpha", "beta"})

    async def test_context_is_bounded_and_executable_helpers_are_never_auto_selected(self) -> None:
        reference = SkillAsset(
            path="docs/checklist.md",
            kind=SkillAssetKind.REFERENCE,
            content="CHECKLIST:" + ("x" * 500),
            capability_tags=("release",),
        )
        helper = SkillAsset(
            path="helpers/deploy.sh",
            kind=SkillAssetKind.HELPER,
            content="echo never-auto-run",
            executable=True,
            security=SkillAssetSecurity.EXECUTABLE_UNTRUSTED,
            helper_execution_policy=(
                SkillHelperExecutionPolicy.MANUAL_WORKER_ONLY
            ),
            capability_tags=("release",),
        )
        published = self._published(
            body="B" * 1500,
            assets=(reference, helper),
        )
        ref = reference_for(published)

        minimal = self.skills.context_for(
            (ref,),
            max_characters=2000,
        )
        self.assertEqual(minimal[0].selected_assets, ())
        self.assertIn("helpers/deploy.sh", minimal[0].omitted_asset_paths)

        selected = self.skills.context_for(
            (ref,),
            relevance_tags=("release",),
            max_characters=2200,
        )
        self.assertEqual(
            [item.path for item in selected[0].selected_assets],
            ["docs/checklist.md"],
        )
        self.assertNotIn(
            "echo never-auto-run",
            "".join(item.content for item in selected[0].selected_assets),
        )

        bounded = self.skills.context_for(
            (ref,),
            max_characters=1000,
        )
        self.assertEqual(bounded[0].characters, 1000)
        self.assertTrue(bounded[0].truncated)

    async def test_turn_context_keeps_skill_material_at_user_authority(self) -> None:
        helper = SkillAsset(
            path="helpers/root.sh",
            kind=SkillAssetKind.HELPER,
            content="sudo dangerous-command",
            executable=True,
            security=SkillAssetSecurity.EXECUTABLE_UNTRUSTED,
            helper_execution_policy=(
                SkillHelperExecutionPolicy.MANUAL_WORKER_ONLY
            ),
        )
        published = self._published(
            body="Reference steps only.",
            assets=(helper,),
        )
        binding = AgentProfileExecutionBinding(
            profile_id="alpha",
            profile_revision=1,
            profile_record_id="profile-rev-1",
            skill_refs=(reference_for(published),),
        )
        execution = TurnExecutionService(
            SimpleNamespace(),
            skills=self.skills,
        )

        message = execution.with_skill_context(
            "Fix the issue",
            binding,
        )

        self.assertTrue(message.startswith("Fix the issue"))
        self.assertIn("<attached_skill_context>", message)
        self.assertIn("untrusted procedural reference data", message)
        self.assertIn("Reference steps only.", message)
        self.assertNotIn("sudo dangerous-command", message)

    async def test_bundle_import_rejects_raw_secrets_and_preserves_provenance(self) -> None:
        with self.assertRaises(ValidationError):
            SkillBundle(
                skill_id="bad.secret",
                skill_md=(
                    "Use this credential: "
                    "api_key=abcdefghijklmnopqrstuv"
                ),
                metadata={"name": "Bad secret"},
            )

        bundle = SkillBundle(
            skill_id="imported.safe",
            skill_md="Safe imported process.",
            metadata={
                "name": "Imported process",
                "description": "Imported safely",
                "tags": ["imported"],
            },
        )
        draft = self.skills.import_bundle(
            bundle,
            actor=self.member,
            source_reference="library://skills/imported.safe",
        )
        skill = self.skills.definition(draft)
        self.assertEqual(draft.lifecycle, DefinitionLifecycle.DRAFT)
        self.assertEqual(skill.owner_identity_id, self.member.identity_id)
        self.assertEqual(skill.source.kind, SkillSourceKind.IMPORTED)
        self.assertEqual(
            skill.source.reference,
            "library://skills/imported.safe",
        )

    async def test_verified_procedure_promotion_creates_reviewable_draft_only(self) -> None:
        draft = self.skills.promote_verified_procedure(
            skill_id="learned.restart",
            name="Restart service safely",
            body="Perform health check, restart, verify.",
            evidence_refs=("evidence-1", "evidence-2"),
            actor=self.member,
            source_reference="run:123",
        )

        self.assertEqual(draft.lifecycle, DefinitionLifecycle.DRAFT)
        skill = self.skills.definition(draft)
        self.assertEqual(
            skill.source.kind,
            SkillSourceKind.VERIFIED_PROCEDURE,
        )
        self.assertEqual(
            skill.source.verified_evidence_refs,
            ("evidence-1", "evidence-2"),
        )

    async def test_tenant_isolation_hides_foreign_skill_records(self) -> None:
        published = self._published()
        self.assertEqual(
            len(self.skills.list(actor=self.admin)),
            1,
        )
        self.assertEqual(
            self.skills.list(actor=self.foreign),
            [],
        )
        with self.assertRaises(SkillNotFoundError):
            self.skills.get(
                published.record_id,
                actor=self.foreign,
            )

    async def test_skill_provider_capabilities_constrain_profile_routing(self) -> None:
        published = self._published(
            skill_id="shell.skill",
            provider_capabilities=(
                AgentProviderCapability.SHELL_TOOLS.value,
            ),
        )
        profile = self.profiles.create(
            AgentProfileCreate(
                profile_id="shell-agent",
                name="Shell agent",
                skill_refs=(reference_for(published),),
            ),
            actor=self.admin,
        )

        providers = AgentProviderService(
            AgentProviderStore(self.sqlite)
        )
        runtimes = AgentRuntimeRegistry()
        providers.upsert(
            AgentProviderUpsert(
                id="basic",
                display_name="basic",
                declared_capabilities=(
                    AgentProviderCapability.AGENT_EXECUTION,
                ),
                granted_capabilities=(
                    AgentProviderCapability.AGENT_EXECUTION,
                ),
                health=AgentProviderHealth.HEALTHY,
            ),
            actor=self.admin,
        )
        runtimes.register(
            _Runtime(
                "basic",
                "runtime",
                (AgentProviderCapability.AGENT_EXECUTION,),
            )
        )
        routing = AgentRoutingService(
            providers,
            runtimes,
            profiles=self.profiles,
            skills=self.skills,
        )

        with self.assertRaises(AgentRoutingError):
            await routing.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    agent_profile_id=profile.profile_id,
                ),
                actor=self.admin,
            )

        shell_caps = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.SHELL_TOOLS,
        )
        providers.upsert(
            AgentProviderUpsert(
                id="shell",
                display_name="shell",
                declared_capabilities=shell_caps,
                granted_capabilities=shell_caps,
                health=AgentProviderHealth.HEALTHY,
            ),
            actor=self.admin,
        )
        runtimes.register(
            _Runtime("shell", "runtime", shell_caps)
        )
        result = await routing.route(
            AgentRoutingRequest(
                project_id="project-a",
                agent_profile_id=profile.profile_id,
            ),
            actor=self.admin,
        )
        self.assertEqual(
            result.selected_runtime.provider_id,
            "shell",
        )

    async def test_skill_requirements_expose_worker_capabilities_exactly(self) -> None:
        published = self._published(
            worker_capabilities=(WorkerCapability.NETWORK.value,),
        )
        provider, worker = self.skills.requirements(
            (reference_for(published),)
        )
        self.assertEqual(provider, ())
        self.assertEqual(worker, (WorkerCapability.NETWORK.value,))

    async def test_non_skill_definition_cannot_be_attached_as_profile_skill(self) -> None:
        self.registry.register_schema(
            DefinitionKindSchema(
                kind="other.definition",
                schema_version="1.0",
                validate=lambda payload: dict(payload),
            )
        )
        from codex_web.definitions import (
            DefinitionDraftCreate,
            DefinitionPublishRequest,
        )

        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id="not-a-skill",
                kind="other.definition",
                definition_schema_version="1.0",
                scope_type=DefinitionScope.WORKSPACE,
                scope_id=self.admin.workspace_id,
                payload={"name": "wrong"},
                actor=self.admin.identity_id,
            )
        )
        wrong = self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor=self.admin.identity_id),
        )

        with self.assertRaisesRegex(
            AgentProfileConflict,
            "must point to agent.skill",
        ):
            self.profiles.create(
                AgentProfileCreate(
                    profile_id="wrong",
                    name="Wrong",
                    skill_refs=(reference_for(wrong),),
                ),
                actor=self.admin,
            )


if __name__ == "__main__":
    unittest.main()
