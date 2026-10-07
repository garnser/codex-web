from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.definitions import DefinitionReference
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import ThreadRunSettings
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.skill_catalog import SkillCatalogService, SkillSourceConflict
from codex_web.services.skills import SkillService
from codex_web.skill_catalog import (
    SkillCatalogEntry,
    SkillSourceCreate,
    SkillSourceHealthReport,
    SkillSourceSyncRequest,
    SkillSourceTrust,
    SkillSourceTransport,
    SkillSourceType,
    UiSkillsDiscoveryRequest,
    UiSkillsEntry,
    UiSkillsImportMode,
    UiSkillsImportRequest,
)
from codex_web.services.ui_skills import UiSkillsAdapter
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

    def remember(
        self, thread_id: str, *, skill_refs=(), **_kwargs
    ) -> ThreadRunSettings:
        value = self.get(thread_id).model_copy(update={"skill_refs": tuple(skill_refs)})
        self.items[thread_id] = value
        return value


class SkillCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        definitions = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        self.skills = SkillService(definitions)
        self.catalog = SkillCatalogService(
            SkillSourceStore(self.state), self.skills, clock=lambda: 100.0
        )
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
    def payload(
        *, revision: str = "abc123", instructions: str = "Review the change."
    ) -> SkillSourceSyncRequest:
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
        self.assertEqual(
            {item["status"] for item in result["items"]}, {"created_draft"}
        )
        review = self.skills.get("code-review", actor=self.actor)
        self.assertEqual(review["definitionLifecycle"], "draft")
        self.assertEqual(review["skill"]["provenance"]["source_id"], "engineering")
        self.assertEqual(review["skill"]["provenance"]["source_revision"], "abc123")
        self.assertEqual(review["skill"]["provenance"]["origin"], "imported")
        filtered = self.skills.list(
            actor=self.actor, category="software engineering", source_id="engineering"
        )
        self.assertEqual([item["skillId"] for item in filtered], ["code-review"])

    def test_sync_is_idempotent_and_refuses_to_overwrite_local_revision(self) -> None:
        self.catalog.sync("engineering", self.payload(), actor=self.actor)
        repeated = self.catalog.sync("engineering", self.payload(), actor=self.actor)
        self.assertEqual({item["status"] for item in repeated["items"]}, {"unchanged"})
        self.skills.update(
            "code-review",
            SkillUpdate(
                instructions="Intentional local override.", reason="local policy"
            ),
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
        result = self.skills.set_thread_assignments(
            "thread-a", (ref,), actor=self.actor
        )
        self.assertEqual(result["explicit"][0]["assignmentOrigin"], "thread_explicit")
        self.assertEqual(result["effective"][0]["recordId"], published["recordId"])
        self.assertEqual(self.settings.items["thread-a"].skill_refs, (ref,))

    def create_ui_source(self, **updates):
        payload = {
            "source_id": "ui-skills",
            "name": "ui-skills",
            "source_type": SkillSourceType.UI_SKILLS,
            "location": "https://github.com/ibelick/ui-skills",
            "transport": SkillSourceTransport.MCP,
            "trust": SkillSourceTrust.APPROVED,
        }
        payload.update(updates)
        return self.catalog.create(SkillSourceCreate(**payload), actor=self.actor)

    @staticmethod
    def ui_discovery(*, revision="ui-abc", changed=False):
        return UiSkillsDiscoveryRequest(
            source_revision=revision,
            transport=SkillSourceTransport.MCP,
            provider_evidence_id=f"provider-evidence-{revision}",
            entries=(
                UiSkillsEntry(
                    upstream_id="animation",
                    name="Animation",
                    description="Design motion intentionally.",
                    instructions=(
                        "Use deliberate motion."
                        if not changed
                        else "Use deliberate accessible motion."
                    ),
                    categories=("Motion",),
                    tags=("design",),
                    upstream_location="https://github.com/ibelick/ui-skills/tree/main/skills/animation",
                ),
                UiSkillsEntry(
                    upstream_id="typography",
                    name="Typography",
                    instructions="Use a readable type scale.",
                    categories=("Visual Design",),
                ),
            ),
        )

    def test_ui_skills_discovery_and_selected_import_preserve_taxonomy_and_provenance(
        self,
    ) -> None:
        self.create_ui_source()
        discovery = self.catalog.discover_ui_skills(
            "ui-skills", self.ui_discovery(), actor=self.actor
        )
        self.assertEqual(discovery["categories"], ["Motion", "Visual Design"])
        self.assertEqual(discovery["providerEvidenceId"], "provider-evidence-ui-abc")
        source = self.catalog.get("ui-skills", actor=self.actor)
        self.assertEqual(source.health_status, "healthy")
        self.assertEqual(source.discovered_count, 2)

        result = self.catalog.import_ui_skills(
            "ui-skills",
            UiSkillsImportRequest(
                mode=UiSkillsImportMode.SINGLE,
                upstream_ids=("animation",),
            ),
            actor=self.actor,
        )
        self.assertEqual(result["count"], 1)
        imported = self.skills.get("ui-skills.animation", actor=self.actor)
        self.assertEqual(
            imported["skill"]["categories"],
            ["product / ux / design engineering", "motion"],
        )
        provenance = imported["skill"]["provenance"]
        self.assertEqual(provenance["source_type"], "ui_skills")
        self.assertEqual(provenance["source_transport"], "mcp")
        self.assertEqual(provenance["upstream_categories"], ["motion"])
        self.assertEqual(
            provenance["upstream_location"],
            "https://github.com/ibelick/ui-skills/tree/main/skills/animation",
        )
        listed = self.catalog.discovery("ui-skills", actor=self.actor)
        self.assertTrue(listed["items"][0]["imported"])
        self.assertNotIn("instructions", listed["items"][0])

    def test_ui_skills_sync_preserves_local_taxonomy_and_creates_new_inactive_revision(
        self,
    ) -> None:
        self.create_ui_source()
        self.catalog.discover_ui_skills(
            "ui-skills", self.ui_discovery(), actor=self.actor
        )
        self.catalog.import_ui_skills(
            "ui-skills", UiSkillsImportRequest(mode="all"), actor=self.actor
        )
        self.skills.update(
            "ui-skills.animation",
            SkillUpdate(
                categories=("Local Product Design",),
                applicability_tags=("local-curation",),
                reason="curate local taxonomy",
            ),
            actor=self.actor,
        )
        self.catalog.discover_ui_skills(
            "ui-skills",
            self.ui_discovery(revision="ui-def", changed=True),
            actor=self.actor,
        )
        result = self.catalog.import_ui_skills(
            "ui-skills",
            UiSkillsImportRequest(mode="single", upstream_ids=("animation",)),
            actor=self.actor,
        )
        self.assertEqual(result["items"][0]["status"], "updated_draft")
        updated = self.skills.get("ui-skills.animation", actor=self.actor)
        self.assertEqual(updated["definitionLifecycle"], "draft")
        self.assertEqual(updated["skill"]["categories"], ["local product design"])
        self.assertEqual(updated["skill"]["applicability_tags"], ["local-curation"])
        self.assertIn("accessible motion", updated["skill"]["instructions"])

    def test_ui_skills_category_auto_import_and_removal_detection(self) -> None:
        self.create_ui_source(
            automatic_sync=True,
            auto_import_categories=("Motion",),
        )
        result = self.catalog.discover_ui_skills(
            "ui-skills", self.ui_discovery(), actor=self.actor
        )
        self.assertEqual(result["automaticImport"]["count"], 1)
        source = self.catalog.get("ui-skills", actor=self.actor)
        self.assertEqual(source.imports[0].upstream_id, "animation")
        reduced = self.ui_discovery().model_copy(update={"entries": ()})
        self.catalog.discover_ui_skills("ui-skills", reduced, actor=self.actor)
        source = self.catalog.get("ui-skills", actor=self.actor)
        self.assertFalse(source.imports[0].upstream_available)

    def test_ui_skills_transport_plans_are_declarative_and_bounded(self) -> None:
        mcp = UiSkillsAdapter.transport_plan(SkillSourceTransport.MCP)
        self.assertEqual(
            mcp.operations, (("list_skills",), ("get_skill", "<skill-id>"))
        )
        cli = UiSkillsAdapter.transport_plan(SkillSourceTransport.CLI)
        self.assertEqual(
            cli.operations[0],
            ("npx", "--yes", "ui-skills@<approved-version>", "categories"),
        )
        self.assertNotIn(
            "sh", {part for operation in cli.operations for part in operation}
        )
        with self.assertRaisesRegex(ValueError, "canonical repository"):
            UiSkillsEntry(
                upstream_id="bad",
                name="Bad",
                instructions="Bad location.",
                upstream_location="javascript:alert(1)",
            )

    def test_ui_skills_failed_health_is_visible_without_replacing_discovery(
        self,
    ) -> None:
        self.create_ui_source()
        self.catalog.discover_ui_skills(
            "ui-skills", self.ui_discovery(), actor=self.actor
        )
        result = self.catalog.record_health(
            "ui-skills",
            SkillSourceHealthReport(
                status="unavailable",
                error="MCP connection refused",
                provider_evidence_id="worker-failure-1",
            ),
            actor=self.actor,
        )
        self.assertEqual(result["source"]["health_status"], "unavailable")
        self.assertEqual(result["source"]["last_sync_error"], "MCP connection refused")
        self.assertEqual(
            self.catalog.discovery("ui-skills", actor=self.actor)["count"], 2
        )


if __name__ == "__main__":
    unittest.main()
