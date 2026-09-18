from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from codex_web.input_plugins import (
    InputContextBlock,
    InputEnvelope,
    InputPatch,
    InputPhase,
    InputPluginBudgetError,
    InputPluginSecurityError,
)


MAX_SKILL_BYTES = 128 * 1024
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class SkillInputPluginError(ValueError):
    pass


class SkillDocument(BaseModel):
    """Validated SKILL.md metadata and instructions; executable code is never loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4096)
    instructions: str = Field(min_length=1)
    source_ref: str
    sha256: str = Field(min_length=64, max_length=64)
    metadata: dict[str, str] = Field(default_factory=dict)


class _PluginCatalog(Protocol):
    def register(self, plugin: Any) -> None: ...


def _unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def parse_skill_document(
    text: str,
    *,
    source_ref: str = "inline:SKILL.md",
) -> SkillDocument:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SKILL_BYTES:
        raise SkillInputPluginError(
            f"SKILL.md exceeds {MAX_SKILL_BYTES} byte limit"
        )

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillInputPluginError("SKILL.md must start with YAML-style frontmatter")

    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise SkillInputPluginError("SKILL.md frontmatter is not terminated")

    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise SkillInputPluginError(
                "SKILL.md frontmatter supports scalar key: value metadata only"
            )
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = _unquote(raw_value)
        if not key or not value:
            raise SkillInputPluginError("SKILL.md frontmatter keys and values cannot be empty")
        if key in metadata:
            raise SkillInputPluginError(f"duplicate SKILL.md frontmatter key: {key}")
        if len(key) > 128 or len(value) > 4096:
            raise SkillInputPluginError("SKILL.md frontmatter scalar exceeds size limit")
        metadata[key] = value

    name = metadata.get("name", "").strip()
    version = metadata.get("version", "").strip()
    description = metadata.get("description", "").strip()
    if not _SKILL_NAME_RE.fullmatch(name):
        raise SkillInputPluginError(
            "SKILL.md name must be a stable alphanumeric/dot/dash/underscore identifier"
        )
    if not _SEMVER_RE.fullmatch(version):
        raise SkillInputPluginError("SKILL.md version must be semantic versioning")
    if not description:
        raise SkillInputPluginError("SKILL.md description is required")

    instructions = "\n".join(lines[closing + 1 :]).strip()
    if not instructions:
        raise SkillInputPluginError("SKILL.md instructions cannot be empty")

    return SkillDocument(
        name=name,
        version=version,
        description=description,
        instructions=instructions,
        source_ref=source_ref,
        sha256=hashlib.sha256(encoded).hexdigest(),
        metadata=metadata,
    )


def load_skill_document(
    path: str | Path,
    *,
    allowed_root: str | Path | None = None,
) -> SkillDocument:
    candidate = Path(path)
    resolved = candidate.resolve(strict=True)
    if resolved.name != "SKILL.md":
        raise SkillInputPluginError("skill input plugin source must be named SKILL.md")

    if allowed_root is not None:
        root = Path(allowed_root).resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise InputPluginSecurityError(
                "SKILL.md path escapes configured skill root"
            ) from exc

    if not resolved.is_file():
        raise SkillInputPluginError("SKILL.md source must be a regular file")
    if resolved.stat().st_size > MAX_SKILL_BYTES:
        raise SkillInputPluginError(
            f"SKILL.md exceeds {MAX_SKILL_BYTES} byte limit"
        )
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillInputPluginError("SKILL.md must be UTF-8") from exc

    return parse_skill_document(text, source_ref=str(resolved))


class SkillInputPlugin:
    """Treat declarative skill instructions as bounded untrusted model context."""

    transport = "skill"

    def __init__(self, document: SkillDocument) -> None:
        self.document = document
        self.id = document.name
        self.version = document.version

    async def transform(
        self,
        envelope: InputEnvelope,
        context,
    ) -> InputPatch:
        configured_limit = context.settings.get("max_skill_characters")
        if configured_limit is None:
            max_characters = context.max_added_characters
        elif isinstance(configured_limit, int) and not isinstance(configured_limit, bool):
            max_characters = configured_limit
        else:
            raise InputPluginSecurityError(
                "max_skill_characters setting must be an integer"
            )

        max_characters = min(max(0, max_characters), context.max_added_characters)
        if len(self.document.instructions) > max_characters:
            raise InputPluginBudgetError(
                "SKILL.md instructions exceed configured added-context budget"
            )

        block_id = (
            f"input-skill:{self.document.name}@{self.document.version}:"
            f"{self.document.sha256[:12]}"
        )
        if any(block.id == block_id for block in envelope.context_blocks):
            context_blocks = envelope.context_blocks
        else:
            context_blocks = (
                *envelope.context_blocks,
                InputContextBlock(
                    id=block_id,
                    source=f"skill:{self.document.name}",
                    content=self.document.instructions,
                    classification="untrusted",
                    source_ref=self.document.source_ref,
                ),
            )

        return InputPatch(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=InputPhase.COMPOSE,
            changes={"context_blocks": context_blocks},
            metadata={
                "skill_sha256": self.document.sha256,
                "skill_source": self.document.source_ref,
            },
        )


def load_skill_plugin(
    path: str | Path,
    *,
    allowed_root: str | Path | None = None,
) -> SkillInputPlugin:
    return SkillInputPlugin(
        load_skill_document(path, allowed_root=allowed_root)
    )


def register_skill_plugins_from_root(
    catalog: _PluginCatalog,
    root: str | Path,
) -> tuple[SkillInputPlugin, ...]:
    """Register root/SKILL.md and immediate child SKILL.md files deterministically."""

    resolved_root = Path(root).resolve(strict=True)
    if not resolved_root.is_dir():
        raise SkillInputPluginError("configured skill root must be a directory")

    candidates: list[Path] = []
    direct = resolved_root / "SKILL.md"
    if direct.exists():
        candidates.append(direct)
    candidates.extend(
        sorted(
            path
            for path in resolved_root.glob("*/SKILL.md")
            if path.is_file()
        )
    )

    loaded: list[SkillInputPlugin] = []
    for candidate in candidates:
        plugin = load_skill_plugin(candidate, allowed_root=resolved_root)
        catalog.register(plugin)
        loaded.append(plugin)
    return tuple(loaded)
