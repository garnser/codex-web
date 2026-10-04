from __future__ import annotations

import re
from dataclasses import dataclass

from codex_web.skill_catalog import (
    SkillCatalogEntry,
    SkillSourceTransport,
    UiSkillsEntry,
)
from codex_web.skills import SkillCreate

UI_SKILLS_LOCATION = "https://github.com/ibelick/ui-skills"


@dataclass(frozen=True, slots=True)
class UiSkillsTransportPlan:
    """Declarative provider plan; the control plane never executes this plan."""

    transport: SkillSourceTransport
    operations: tuple[tuple[str, ...], ...]


class UiSkillsAdapter:
    adapter_id = "ui-skills"

    @staticmethod
    def transport_plan(transport: SkillSourceTransport) -> UiSkillsTransportPlan:
        if transport == SkillSourceTransport.MCP:
            return UiSkillsTransportPlan(
                transport, (("list_skills",), ("get_skill", "<skill-id>"))
            )
        if transport == SkillSourceTransport.CLI:
            return UiSkillsTransportPlan(
                transport,
                (
                    ("npx", "--yes", "ui-skills@<approved-version>", "categories"),
                    (
                        "npx",
                        "--yes",
                        "ui-skills@<approved-version>",
                        "list",
                        "--category",
                        "<category>",
                    ),
                    (
                        "npx",
                        "--yes",
                        "ui-skills@<approved-version>",
                        "get",
                        "<skill-id>",
                    ),
                ),
            )
        if transport == SkillSourceTransport.REPOSITORY:
            return UiSkillsTransportPlan(
                transport, (("read_repository_snapshot", UI_SKILLS_LOCATION),)
            )
        raise ValueError("ui-skills transport must be MCP, CLI, or repository")

    @staticmethod
    def canonical_skill_id(upstream_id: str) -> str:
        slug = re.sub(r"[^a-z0-9._-]+", "-", upstream_id.casefold()).strip("-._")
        if not slug:
            raise ValueError(
                "ui-skills upstream identifier cannot normalize to an empty Skill ID"
            )
        return f"ui-skills.{slug}"[:160]

    @classmethod
    def normalize(cls, item: UiSkillsEntry) -> SkillCatalogEntry:
        upstream_categories = tuple(dict.fromkeys(item.categories))
        local_categories = tuple(
            dict.fromkeys(("Product / UX / Design Engineering", *upstream_categories))
        )
        return SkillCatalogEntry(
            upstream_id=item.upstream_id,
            manifest=SkillCreate(
                skill_id=cls.canonical_skill_id(item.upstream_id),
                name=item.name,
                description=item.description,
                instructions=item.instructions,
                categories=local_categories,
                applicability_tags=tuple(dict.fromkeys(("ui-skills", *item.tags))),
                assets=item.assets,
            ),
        )
