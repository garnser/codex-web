from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from codex_web.action_providers import ActionProviderBinding, ActionRequest
from codex_web.input_plugins import (
    InputEnvelope,
    InputFailurePolicy,
    InputMessage,
    InputPhase,
    InputPluginPipeline,
    InputPluginRegistration,
)
from codex_web.models import TaskSourceIdentity
from codex_web.services.task_sources import (
    TaskSourceCapability,
    TaskSourceSnapshot,
    UnsupportedTaskSourceCapability,
)


ROOT = Path(__file__).resolve().parents[1]


def load_example(relative: str, name: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load example: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RepositoryExampleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_task_source_is_read_only_and_projects_state(self):
        module = load_example(
            "examples/task_sources/reference_task_source/adapter.py",
            "example_task_source_adapter",
        )
        identity = TaskSourceIdentity(
            source_type="example-ticket",
            source_instance="demo",
            external_id="T-1",
        )
        source = module.ExampleTicketSource(
            snapshots=(
                TaskSourceSnapshot(
                    identity=identity,
                    title="Example",
                    source_state="review",
                    owners=("maya",),
                ),
            )
        )

        rows = await source.discover(scope="team")
        self.assertEqual(len(rows), 1)
        projection = source.project(rows[0])
        self.assertEqual(projection.stage, "ready_for_validation")
        self.assertEqual(projection.owner, "maya")
        self.assertFalse(
            source.capabilities.supports(TaskSourceCapability.OWNER_WRITE)
        )
        with self.assertRaises(UnsupportedTaskSourceCapability):
            await source.write_owner(identity, "other")

    async def test_reference_action_provider_is_idempotent_verifiable_and_reversible(self):
        module = load_example(
            "examples/action_providers/reference_action_provider/provider.py",
            "example_action_provider",
        )
        provider = module.ExampleFeatureFlagProvider()
        binding = ActionProviderBinding(
            organization_id="local",
            workspace_id="default",
            provider_type=provider.provider_type,
            provider_instance=provider.provider_instance,
        )
        request = ActionRequest(
            action_id="example.feature-flag.set",
            organization_id="local",
            workspace_id="default",
            parameters={"name": "demo", "enabled": True},
            idempotency_key="example-1",
        )

        result = await provider.execute(request, binding=binding)
        repeated = await provider.execute(request, binding=binding)
        self.assertEqual(result.execution_id, repeated.execution_id)
        self.assertTrue((await provider.verify(result, binding=binding)).verified)
        rolled_back = await provider.rollback(result, binding=binding)
        self.assertEqual(rolled_back.status, "rolled_back")
        self.assertNotIn("demo", provider.flags)

    async def test_context_enricher_only_changes_composable_context(self):
        module = load_example(
            "examples/input_plugins/context_enricher/plugin.py",
            "example_context_enricher",
        )
        plugin = module.RepositoryContextPlugin()
        pipeline = InputPluginPipeline(
            (
                InputPluginRegistration(
                    plugin=plugin,
                    phase=InputPhase.ENRICH,
                    failure_policy=InputFailurePolicy.FAIL_CLOSED,
                    settings={"repository_summary": "Bounded example context."},
                ),
            )
        )
        envelope = InputEnvelope(
            request_id="example",
            organization_id="org-a",
            workspace_id="ws-a",
            actor_id="actor-a",
            model_class="primary-coding",
            purpose="test",
            messages=(InputMessage(role="user", content="hello"),),
        )

        result = await pipeline.execute(envelope)
        self.assertEqual(result.envelope.organization_id, "org-a")
        self.assertEqual(result.envelope.actor_id, "actor-a")
        self.assertEqual(len(result.envelope.context_blocks), 1)
        self.assertEqual(
            result.envelope.context_blocks[0].id,
            "example.repository-summary",
        )
        self.assertEqual(
            result.provenance[0].applied_fields,
            ("context_blocks",),
        )


if __name__ == "__main__":
    unittest.main()
