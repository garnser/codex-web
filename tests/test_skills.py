from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.agent_profiles import AgentProfileCreate
from codex_web.agent_providers import AgentProviderCapability
from codex_web.definitions import DefinitionLifecycle, reference_for
from codex_web.execution_workers import WorkerCapability
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.skills import SkillConflict, SkillNotFound, SkillService
from codex_web.skills import (
    SkillAsset,
    SkillAssetContextMode,
    SkillAssetKind,
    SkillAssetSecurityClass,
    SkillBundleImport,
    SkillCreate,
    SkillLifecycle,
    SkillLifecycleChange,
    SkillPublish,
    SkillRollback,
    SkillUpdate,
)
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def actor(
    identity_id: str,
    *,
    workspace_id: str = "workspace-a",
    admin: bool = False,
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id=identity_id,
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id=workspace_id,
        roles=(
            (MembershipRole.ADMIN,)
            if admin
            else (MembershipRole.MEMBER,)
        ),
        assurance=AuthenticationAssurance.MFA,
    )


class SkillServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.definitions = DefinitionRegistryService(
            DefinitionRegistryStore(self.state)
        )
        self.skills = SkillService(self.definitions)
        self.profile_store = AgentProfileStore(self.state)
        self.profiles = AgentProfileService(
            self.profile_store,
            definitions=self.definitions,
            skill_reference_validator=lambda ref, current_actor: (
                self.skills.validate_reference(
                    ref,
                    actor=current_actor,
                )
            ),
        )
        self.skills.bind_profiles(self.profiles)
        self.admin = actor("admin", admin=True)
        self.member = actor("member")
        self.foreign = actor(
            "foreign",
            workspace_id="workspace-b",
            admin=True,
        )

        def profile_usage(reference):
            result = []
            for profile in self.profile_store.load().revisions:
                if any(
                    item.record_id == reference.record_id
                    for item in profile.skill_refs
                ):
                    result.append(
                        {
                            "object_type": "agent_profile",
                            "object_id": profile.profile_id,
                            "revision": profile.revision,
                        }
                    )
            return result

        self.definitions.register_usage_provider(profile_usage)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _draft(self, *, skill_id: str = "release-check") -> dict:
        return self.skills.create(
            SkillCreate(
                skill_id=skill_id,
                name="Release Check",
                description="Verify a release deterministically.",
                instructions="Inspect the candidate, run checks, and record evidence.",
                applicability_tags=("release", "validation"),
                capability_tags=("git",),
                assets=(
                    SkillAsset(
                        path="references/release.md",
                        kind=SkillAssetKind.REFERENCE,
                        content="Review release evidence and rollback metadata.",
                        tags=("release", "evidence"),
                        context_mode=SkillAssetContextMode.RELEVANT,
                        security_class=(
                            SkillAssetSecurityClass.UNTRUSTED_REFERENCE
                        ),
                    ),
                    SkillAsset(
                        path="helpers/check.sh",
                        kind=SkillAssetKind.HELPER_SCRIPT,
                        content="#!/bin/sh\necho check\n",
                        context_mode=SkillAssetContextMode.NEVER,
                        security_class=(
                            SkillAssetSecurityClass.EXECUTABLE_UNTRUSTED
                        ),
                    ),
                ),
                required_provider_capabilities=(
                    AgentProviderCapability.GIT_OPERATIONS,
                ),
                required_worker_capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                ),
                input_expectations=("candidate revision",),
                output_expectations=("evidence summary",),
            ),
            actor=self.admin,
        )

    def _publish(self, draft: dict, *, expected=None) -> dict:
        return self.skills.publish(
            draft["skillId"],
            draft["recordId"],
            SkillPublish(expected_active_revision=expected),
            actor=self.admin,
        )

    def test_versioning_exact_profile_pins_and_usage(self) -> None:
        first = self._publish(self._draft())
        first_ref = first["definitionReference"]

        profile_a = self.profiles.create(
            AgentProfileCreate(
                profile_id="release-a",
                name="Release A",
                skill_refs=(first_ref,),
            ),
            actor=self.admin,
        )
        profile_b = self.profiles.create(
            AgentProfileCreate(
                profile_id="release-b",
                name="Release B",
            ),
            actor=self.admin,
        )

        second_draft = self.skills.update(
            "release-check",
            SkillUpdate(
                instructions="Verify the candidate and record signed evidence.",
                reason="tighten evidence procedure",
            ),
            actor=self.admin,
        )
        second = self._publish(
            second_draft,
            expected=first["revision"],
        )
        first_record = self.definitions.get_record(first["recordId"])
        self.assertEqual(
            first_record.lifecycle,
            DefinitionLifecycle.SUPERSEDED,
        )

        # The existing profile stays pinned to the exact old revision.
        old_profile = self.profiles.get(
            profile_a.profile_id,
            actor=self.admin,
        )
        self.assertEqual(old_profile.skill_refs[0].revision, first["revision"])
        self.skills.validate_reference(
            old_profile.skill_refs[0],
            actor=self.admin,
        )

        attached = self.skills.attach_profile(
            "release-check",
            profile_b.profile_id,
            actor=self.admin,
        )
        self.assertEqual(
            attached["skill_refs"][0]["revision"],
            second["revision"],
        )

        usage = self.skills.usage(
            "release-check",
            actor=self.admin,
            revision=first["revision"],
        )
        self.assertIn(
            "release-a",
            {item["object_id"] for item in usage["items"]},
        )

    def test_context_is_bounded_relevant_and_never_injects_helper_script(self) -> None:
        published = self._publish(self._draft())
        reference = reference_for(
            self.definitions.get_record(published["recordId"])
        )

        context = self.skills.context_for_refs(
            (reference,),
            actor=self.admin,
            objective="validate release evidence",
            max_chars=6000,
        )

        self.assertIn("Release Check", context.text)
        self.assertIn("Review release evidence", context.text)
        self.assertNotIn("echo check", context.text)
        self.assertIn(
            "references/release.md",
            context.included_assets,
        )
        self.assertIn(
            "helpers/check.sh",
            context.omitted_assets,
        )
        self.assertIn(
            "cannot grant authority",
            context.text,
        )
        self.assertLessEqual(context.character_count, 6000)

    def test_skill_capabilities_only_add_requirements(self) -> None:
        published = self._publish(self._draft())
        ref = reference_for(
            self.definitions.get_record(published["recordId"])
        )

        provider, worker = self.skills.requirements_for_refs(
            (ref,),
            actor=self.admin,
        )

        self.assertEqual(
            provider,
            (AgentProviderCapability.GIT_OPERATIONS,),
        )
        self.assertEqual(
            worker,
            (
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
        )

    def test_archive_blocks_new_execution_but_restore_recovers_old_pin(self) -> None:
        first = self._publish(self._draft())
        old_ref = reference_for(
            self.definitions.get_record(first["recordId"])
        )
        archived = self.skills.lifecycle(
            "release-check",
            SkillLifecycle.ARCHIVED,
            SkillLifecycleChange(reason="retire procedure"),
            actor=self.admin,
        )
        self.assertEqual(
            archived["skill"]["lifecycle"],
            SkillLifecycle.ARCHIVED.value,
        )
        with self.assertRaises(SkillConflict):
            self.skills.validate_reference(old_ref, actor=self.admin)

        restored = self.skills.lifecycle(
            "release-check",
            SkillLifecycle.ACTIVE,
            SkillLifecycleChange(reason="restore procedure"),
            actor=self.admin,
        )
        self.assertEqual(
            restored["skill"]["lifecycle"],
            SkillLifecycle.ACTIVE.value,
        )
        self.skills.validate_reference(old_ref, actor=self.admin)

    def test_rollback_creates_new_published_revision(self) -> None:
        first = self._publish(self._draft())
        changed = self.skills.update(
            "release-check",
            SkillUpdate(
                instructions="Changed instructions.",
                reason="change",
            ),
            actor=self.admin,
        )
        second = self._publish(changed, expected=first["revision"])

        rolled = self.skills.rollback(
            "release-check",
            SkillRollback(
                target_revision=first["revision"],
                expected_active_revision=second["revision"],
                reason="rollback",
            ),
            actor=self.admin,
        )

        self.assertGreater(rolled["revision"], second["revision"])
        self.assertEqual(
            rolled["skill"]["instructions"],
            first["skill"]["instructions"],
        )

    def test_export_import_roundtrip_is_bounded_and_tenant_scoped(self) -> None:
        published = self._publish(self._draft())
        bundle = self.skills.export_bundle(
            "release-check",
            actor=self.admin,
            revision=published["revision"],
        )
        imported = self.skills.import_bundle(
            SkillBundleImport.model_validate(bundle),
            actor=self.foreign,
        )

        self.assertEqual(imported["skillId"], "release-check")
        self.assertEqual(
            imported["skill"]["provenance"]["source_type"],
            "import",
        )
        with self.assertRaises(SkillNotFound):
            self.skills.validate_reference(
                reference_for(
                    self.definitions.get_record(
                        imported["recordId"]
                    )
                ),
                actor=self.admin,
            )

    def test_malicious_assets_and_raw_secrets_fail_validation(self) -> None:
        with self.assertRaises(ValidationError):
            SkillAsset(
                path="../escape.sh",
                kind=SkillAssetKind.HELPER_SCRIPT,
                content="echo safe",
                context_mode=SkillAssetContextMode.NEVER,
                security_class=(
                    SkillAssetSecurityClass.EXECUTABLE_UNTRUSTED
                ),
            )

        with self.assertRaises(ValidationError):
            SkillCreate(
                skill_id="secret-skill",
                name="Secret skill",
                instructions=(
                    "credential = "
                    "-----BEGIN PRIVATE KEY-----\nabc"
                ),
            )

        with self.assertRaises(ValidationError):
            SkillAsset(
                path="helper.sh",
                kind=SkillAssetKind.HELPER_SCRIPT,
                content="echo hi",
                context_mode=SkillAssetContextMode.ALWAYS,
                security_class=(
                    SkillAssetSecurityClass.EXECUTABLE_UNTRUSTED
                ),
            )


if __name__ == "__main__":
    unittest.main()
