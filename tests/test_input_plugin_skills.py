from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.input_plugin_skills import (
    SkillInputPlugin,
    SkillInputPluginError,
    load_skill_document,
    parse_skill_document,
    register_skill_plugins_from_root,
)
from codex_web.input_plugins import (
    InputEnvelope,
    InputFailurePolicy,
    InputMessage,
    InputPhase,
    InputPluginBudgetError,
    InputPluginContext,
    InputPluginPipeline,
    InputPluginRegistration,
    InputPluginSecurityError,
)
from codex_web.services.input_plugin_definitions import (
    InputPluginCatalog,
    InputPluginDefinitionError,
    default_input_plugin_catalog,
)


PROMPT_MASTER_SHAPED_SKILL = """---
name: prompt-master
version: 1.8.0
description: Synthetic Prompt Master compatibility fixture for adapter tests.
---
# Prompt composition

Optimize only when explicitly selected by the input-pipeline definition.
Keep the resulting request concise and verify the requested output contract.
"""


class SkillInputPluginTests(unittest.IsolatedAsyncioTestCase):
    def _envelope(self) -> InputEnvelope:
        return InputEnvelope(
            request_id="req-1",
            organization_id="org-a",
            workspace_id="ws-a",
            actor_id="actor-a",
            model_class="primary-coding",
            messages=(InputMessage(role="user", content="Improve this prompt"),),
            purpose="prompt-engineering",
            authority_refs=("authority-1",),
            policy_refs=("policy-1",),
        )

    def _context(self, **overrides) -> InputPluginContext:
        payload = {
            "organization_id": "org-a",
            "workspace_id": "ws-a",
            "actor_id": "actor-a",
            "request_id": "req-1",
            "purpose": "prompt-engineering",
            "max_patch_bytes": 32768,
            "max_added_characters": 16000,
            "settings": {},
        }
        payload.update(overrides)
        return InputPluginContext(**payload)

    def test_parser_requires_bounded_frontmatter_and_semver(self) -> None:
        document = parse_skill_document(PROMPT_MASTER_SHAPED_SKILL)
        self.assertEqual(document.name, "prompt-master")
        self.assertEqual(document.version, "1.8.0")
        self.assertEqual(len(document.sha256), 64)
        self.assertIn("Prompt composition", document.instructions)

        with self.assertRaisesRegex(
            SkillInputPluginError,
            "semantic versioning",
        ):
            parse_skill_document(
                PROMPT_MASTER_SHAPED_SKILL.replace(
                    "version: 1.8.0",
                    "version: latest",
                )
            )

        with self.assertRaisesRegex(
            SkillInputPluginError,
            "frontmatter",
        ):
            parse_skill_document("# no metadata\nInstructions")

    async def test_skill_participates_as_untrusted_context_without_loading_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_dir = root / "prompt-master"
            refs = skill_dir / "references"
            refs.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                PROMPT_MASTER_SHAPED_SKILL,
                encoding="utf-8",
            )
            (refs / "templates.md").write_text(
                "REFERENCE_FILE_MUST_NOT_BE_AUTO_LOADED",
                encoding="utf-8",
            )

            catalog = InputPluginCatalog()
            loaded = register_skill_plugins_from_root(catalog, root)
            self.assertEqual(len(loaded), 1)
            plugin = catalog.get("prompt-master", "1.8.0")

            result = await InputPluginPipeline(
                [
                    InputPluginRegistration(
                        plugin=plugin,
                        phase=InputPhase.COMPOSE,
                        failure_policy=InputFailurePolicy.FAIL_CLOSED,
                        max_added_characters=16000,
                    )
                ]
            ).execute(self._envelope())

            self.assertEqual(len(result.provenance), 1)
            provenance = result.provenance[0]
            self.assertEqual(provenance.plugin_id, "prompt-master")
            self.assertEqual(provenance.plugin_version, "1.8.0")
            self.assertEqual(provenance.transport, "skill")
            self.assertEqual(provenance.applied_fields, ("context_blocks",))

            block = result.envelope.context_blocks[-1]
            self.assertEqual(block.classification, "untrusted")
            self.assertEqual(block.source, "skill:prompt-master")
            self.assertIn("Prompt composition", block.content)
            self.assertNotIn("REFERENCE_FILE_MUST_NOT_BE_AUTO_LOADED", block.content)
            self.assertEqual(
                result.envelope.authority_refs,
                ("authority-1",),
            )
            self.assertEqual(
                result.envelope.policy_refs,
                ("policy-1",),
            )

    async def test_skill_budget_fails_closed_without_truncating_instructions(self) -> None:
        plugin = SkillInputPlugin(
            parse_skill_document(PROMPT_MASTER_SHAPED_SKILL)
        )

        with self.assertRaisesRegex(
            InputPluginBudgetError,
            "added-context budget",
        ):
            await plugin.transform(
                self._envelope(),
                self._context(
                    max_added_characters=32,
                    settings={"max_skill_characters": 32},
                ),
            )

    def test_loader_rejects_path_escape_from_explicit_skill_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            skill = outside / "SKILL.md"
            skill.write_text(PROMPT_MASTER_SHAPED_SKILL, encoding="utf-8")

            with self.assertRaisesRegex(
                InputPluginSecurityError,
                "escapes configured skill root",
            ):
                load_skill_document(skill, allowed_root=allowed)

    def test_default_catalog_only_discovers_skills_when_root_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_dir = root / "prompt-master"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                PROMPT_MASTER_SHAPED_SKILL,
                encoding="utf-8",
            )

            default_catalog = default_input_plugin_catalog()
            with self.assertRaisesRegex(
                InputPluginDefinitionError,
                "implementation unavailable",
            ):
                default_catalog.get("prompt-master", "1.8.0")

            configured = default_input_plugin_catalog(skill_root=str(root))
            plugin = configured.get("prompt-master", "1.8.0")
            self.assertEqual(plugin.transport, "skill")


if __name__ == "__main__":
    unittest.main()
