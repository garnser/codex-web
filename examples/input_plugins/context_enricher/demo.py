from __future__ import annotations

import asyncio

from plugin import RepositoryContextPlugin

from codex_web.input_plugins import (
    InputEnvelope,
    InputFailurePolicy,
    InputMessage,
    InputPhase,
    InputPluginPipeline,
    InputPluginRegistration,
)


async def main() -> None:
    plugin = RepositoryContextPlugin()
    pipeline = InputPluginPipeline(
        (
            InputPluginRegistration(
                plugin=plugin,
                phase=InputPhase.ENRICH,
                failure_policy=InputFailurePolicy.FAIL_CLOSED,
                max_patch_bytes=8 * 1024,
                max_added_characters=2_000,
                settings={
                    "repository_summary": (
                        "This repository is a local-first AI engineering control plane. "
                        "Prefer canonical services and provider-neutral contracts."
                    )
                },
            ),
        )
    )
    envelope = InputEnvelope(
        request_id="demo-request",
        organization_id="local",
        workspace_id="default",
        actor_id="demo-user",
        model_class="primary-coding",
        purpose="code-review",
        work_item_ref="demo#1",
        messages=(InputMessage(role="user", content="Review this change."),),
    )

    result = await pipeline.execute(envelope)
    print(result.envelope.context_blocks)
    print(result.provenance)


if __name__ == "__main__":
    asyncio.run(main())
