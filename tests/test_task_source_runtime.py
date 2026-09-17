from __future__ import annotations

import unittest

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceResolutionError,
    TaskSourceWritebackService,
)
from codex_web.services.task_sources import TaskSourceCapability, TaskSourceSnapshot


class _Host:
    def __init__(self, state: WorkItemState) -> None:
        self.states = {state.ref: state}

    def _load_work_item_states(self):
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}


class TaskSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _source_and_state(self):
        identity = TaskSourceIdentity(
            source_type="reference",
            source_instance="runtime-test",
            external_id="TASK-42",
            revision="1",
        )
        snapshot = TaskSourceSnapshot(
            identity=identity,
            title="Portable work",
            source_state="open",
            owners=("james",),
            labels=("priority::P1",),
        )
        source = ReferenceTaskSource("runtime-test", snapshots=[snapshot])
        state = WorkItemState(
            ref="canonical-42",
            project_id="home",
            source_identity=identity,
            current_owner="quinn",
            current_stage="validation_running",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        return source, state

    async def test_runtime_reference_adapter_passes_shared_conformance(self) -> None:
        source, _ = self._source_and_state()
        suite = TaskSourceConformanceSuite()
        suite.validate_adapter(source)
        discovered = await source.discover(scope="home")
        snapshot = await source.read(discovered[0].identity)
        projection = source.project(snapshot)
        event = await source.normalize_event(
            {"external_id": "TASK-42", "event_type": "updated", "occurred_at": 2.0}
        )

        suite.validate_snapshot(source, snapshot)
        suite.validate_projection(source, snapshot, projection)
        self.assertIsNotNone(event)
        suite.validate_event(source, event)
        self.assertTrue(source.capabilities.supports(TaskSourceCapability.COMMENTS))
        self.assertTrue(source.capabilities.supports(TaskSourceCapability.ARTIFACT_LINKS))

    async def test_writeback_updates_reference_provider_without_provider_branching(self) -> None:
        source, state = self._source_and_state()
        host = _Host(state)
        registry = TaskSourceRegistry()
        registry.register("reference", lambda item: source)
        service = TaskSourceWritebackService(host, registry)

        updated = await service.sync(state)
        external = await source.read(state.source_identity)

        self.assertEqual(external.owners, ("quinn",))
        self.assertEqual(external.source_state, "open")
        self.assertEqual(updated.source_identity.source_type, "reference")

        await service.add_comment(updated, "Validated through generic runtime")
        await service.attach_artifact(updated, "https://artifacts.example/42")
        self.assertEqual(source.comments["TASK-42"], ["Validated through generic runtime"])
        external = await source.read(updated.source_identity)
        self.assertEqual(external.artifact_links, ("https://artifacts.example/42",))

    async def test_writeback_projects_closed_state(self) -> None:
        source, state = self._source_and_state()
        state.current_stage = "closed"
        state.current_owner = None
        host = _Host(state)
        registry = TaskSourceRegistry()
        registry.register("reference", lambda item: source)
        service = TaskSourceWritebackService(host, registry)

        await service.sync(state)
        external = await source.read(state.source_identity)
        self.assertEqual(external.source_state, "closed")
        self.assertEqual(external.owners, ())

    def test_resolution_rejects_wrong_instance(self) -> None:
        source, state = self._source_and_state()
        registry = TaskSourceRegistry()
        other = ReferenceTaskSource("other-instance")
        registry.register("reference", lambda item: other)

        with self.assertRaises(TaskSourceResolutionError):
            registry.resolve(state, required=True)


if __name__ == "__main__":
    unittest.main()
