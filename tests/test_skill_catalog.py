from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.definitions import DefinitionReference
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import ThreadRunSettings
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.skill_catalog import SkillCatalogService, SkillSourceConflict
from codex_web.services.skills import SkillService
from codex_web.skill_catalog import (
    SkillCatalogEntry,
    SkillSourceCreate,
    SkillSourceSyncRequest,
    SkillSourceTrust,
)
from codex_web.skills import SkillCreate, SkillPublish, SkillUpdate
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.skill_catalog import SkillSourceStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def admin() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="workspace-a",
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class ThreadSettings:
    def __init__(self) -> None:
        self.items: dict[str, ThreadRunSettings] = {}

    def get(self, thread_id: str) -> ThreadRunSettings:
        return self.items.get(thread_id, ThreadRunSettings())

    def remember(self, thread_id: str, *, skill_refs=(), **_kwargs) -> ThreadRunSettings:
        value = self.get(thread_id).model_copy(update={"skill_refs": tuple(skill_refs)})
        self.items[thread_id] = value
        return value


class SkillCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        definitions = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        self.skills = SkillService(definitions)
        self.catalog = SkillCatalogService(SkillSourceStore(self.state), self.skills, clock=lambda: 100.0)
        self.actor = admin()
        self.settings = ThreadSettings()
        self.skills.bind_thread_settings(self.settings)
        self.catalog.create(
            SkillSourceCreate(
                source_id="engineering",
                name="Engineering catalog",
                location="https://github.com/example/skills",
                trust=SkillSourceTrust.APPROVED,
            ),
            actor=self.actor,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def payload(*, revision: str = "abc123", instructions: str = "Review the change.") -> SkillSourceSyncRequest:
        return SkillSourceSyncRequest(
            source_revision=revision,
            entries=(
                SkillCatalogEntry(
                    upstream_id="review/SKILL.md",
                    manifest=SkillCreate(
                        skill_id="code-review",
                        name="Code review",
                        description="Review a change safely.",
                        instructions=instructions,
                        categories=("Software Engineering", "Testing / QA"),
                        applicability_tags=("review",),
                    ),
                ),
                SkillCatalogEntry(
                    upstream_id="docs/SKILL.md",
                    manifest=SkillCreate(
                        skill_id="documentation",
                        name="Documentation",
                        instructions="Update bounded documentation.",
                        categories=("Documentation",),
                    ),
                ),
            ),
        )

    def test_sync_normalizes_multiple_skills_with_provenance_and_filters(self) -> None:
        result = self.catalog.sync("engineering", self.payload(), actor=self.actor)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["publicationRequired"])
        self.assertEqual({item["status"] for item in result["items"]}, {"created_draft"})
        review = self.skills.get("code-review", actor=self.actor)
        self.assertEqual(review["definitionLifecycle"], "draft")
        self.assertEqual(review["skill"]["provenance"]["source_id"], "engineering")
        self.assertEqual(review["skill"]["provenance"]["source_revision"], "abc123")
        self.assertEqual(review["skill"]["provenance"]["origin"], "imported")
        filtered = self.skills.list(actor=self.actor, category="software engineering", source_id="engineering")
        self.assertEqual([item["skillId"] for item in filtered], ["code-review"])

    def test_sync_is_idempotent_and_refuses_to_overwrite_local_revision(self) -> None:
        self.catalog.sync("engineering", self.payload(), actor=self.actor)
        repeated = self.catalog.sync("engineering", self.payload(), actor=self.actor)
        self.assertEqual({item["status"] for item in repeated["items"]}, {"unchanged"})
        self.skills.update(
            "code-review",
            SkillUpdate(instructions="Intentional local override.", reason="local policy"),
            actor=self.actor,
        )
        with self.assertRaisesRegex(SkillSourceConflict, "local changes"):
            self.catalog.sync(
                "engineering",
                self.payload(revision="def456", instructions="Upstream changed."),
                actor=self.actor,
            )

    def test_thread_assignment_pins_published_revision_and_reports_origin(self) -> None:
        self.catalog.sync("engineering", self.payload(), actor=self.actor)
        draft = self.skills.get("code-review", actor=self.actor)
        published = self.skills.publish(
            "code-review", draft["recordId"], SkillPublish(), actor=self.actor
        )
        ref = DefinitionReference.model_validate(published["definitionReference"])
        result = self.skills.set_thread_assignments("thread-a", (ref,), actor=self.actor)
        self.assertEqual(result["explicit"][0]["assignmentOrigin"], "thread_explicit")
        self.assertEqual(result["effective"][0]["recordId"], published["recordId"])
        self.assertEqual(self.settings.items["thread-a"].skill_refs, (ref,))


if __name__ == "__main__":
    unittest.main()
