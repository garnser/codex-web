"""A bounded content format for curated UX knowledge, stored in canonical Memory."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.organizational_memory import KnowledgeContextSource, KnowledgeContextSelection
from codex_web.identity import PrincipalKind

UX_BRIEF_TAG = "ux-context-brief"
UX_BRIEF_MAX_CHARACTERS = 14000
UX_BRIEF_MAX_SOURCES = 16


class UxBriefSectionName(StrEnum):
    TARGET_USERS = "target_users"
    OBSERVED_NEEDS = "observed_needs"
    JOURNEYS = "journeys"
    DESIGN_CONSTRAINTS = "design_constraints"
    APPROVED_COPY = "approved_copy"
    UNCERTAINTIES = "uncertainties"
    EVALUATION_CRITERIA = "evaluation_criteria"


class UxBriefSource(KnowledgeContextSource):
    """Exact canonical source identity and the content fingerprint inspected."""


class UxBriefSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    text: str = Field(default="", max_length=2000)
    sources: tuple[UxBriefSource, ...] = Field(default=(), max_length=UX_BRIEF_MAX_SOURCES)


class UxBrief(BaseModel):
    """Product observations remain knowledge; task instructions remain Skills."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["ux-context-brief/1.0"] = "ux-context-brief/1.0"
    sections: dict[UxBriefSectionName, UxBriefSection] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def bounded(self):
        if not any(section.text for section in self.sections.values()):
            raise ValueError("provide at least one nonempty brief section")
        sources = {source for section in self.sections.values() for source in section.sources}
        if len(sources) > UX_BRIEF_MAX_SOURCES:
            raise ValueError("brief supports at most 16 distinct sources")
        # Include source attribution in the context budget, not just prose.
        if len(self.model_dump_json()) > UX_BRIEF_MAX_CHARACTERS:
            raise ValueError("brief text and source references exceed 14000 characters")
        return self


class UxBriefSelection(KnowledgeContextSelection):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sections: tuple[UxBriefSectionName, ...] = Field(min_length=1, max_length=7)
    max_context_tokens: int = Field(default=2000, ge=1, le=4000)

    @model_validator(mode="after")
    def unique_sections(self):
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("select each brief section only once")
        return self


class UxBriefTurnBinding(BaseModel):
    """Server-owned admission pins; contains no credentials or copied source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    selection: UxBriefSelection
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    identity_id: str = Field(min_length=1)
    principal_kind: PrincipalKind
    service_token_id: str | None = None
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
