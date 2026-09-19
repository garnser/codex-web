from __future__ import annotations

from codex_web.input_plugins import (
    InputContextBlock,
    InputEnvelope,
    InputPatch,
    InputPhase,
    InputPluginContext,
)


class RepositoryContextPlugin:
    """Add a bounded repository summary to composable model context."""

    id = "example.repository-context"
    version = "1.0.0"
    transport = "builtin"

    async def transform(
        self,
        envelope: InputEnvelope,
        context: InputPluginContext,
    ) -> InputPatch:
        summary = str(context.settings.get("repository_summary") or "").strip()
        if not summary:
            return InputPatch(
                plugin_id=self.id,
                plugin_version=self.version,
                phase=InputPhase.ENRICH,
                changes={},
                warnings=("repository_summary setting is empty; no context added",),
            )

        block_id = "example.repository-summary"
        blocks = tuple(
            block
            for block in envelope.context_blocks
            if block.id != block_id
        ) + (
            InputContextBlock(
                id=block_id,
                source="example-input-plugin",
                content=summary,
                classification="internal",
                source_ref=context.work_item_ref,
            ),
        )
        return InputPatch(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=InputPhase.ENRICH,
            changes={"context_blocks": blocks},
            metadata={"context_block_id": block_id},
        )
