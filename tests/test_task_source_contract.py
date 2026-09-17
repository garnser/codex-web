from __future__ import annotations

import unittest

from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceEvent,
    TaskSourceIdentity,
    TaskSourceSnapshot,
    UnsupportedTaskSourceCapability,
)


class _ReferenceTaskSource:
    source_type = "reference"
    source_instance = "local-test"
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.DISCOVERY,
                TaskSourceCapability.READ,
                TaskSourceCapability.EVENTS,
            }
        )
    )

    def __init__(self) -> None:
        self.identity = TaskSourceIdentity(
            source_type=self.source_type,
            source_instance=self.source_instance,
            external_id="TASK-1",
            external_url="https://tasks.example/TASK-1",
            revision="7",
            event_cursor="evt-9",
        )
        self.snapshot = TaskSourceSnapshot(
            identity=self.identity,
            title="Reference task",
            source_state="open",
            owners=("dana",),
            labels=("priority::P1",),
        )

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        self.capabilities.require(TaskSourceCapability.DISCOVERY)
        return [self.snapshot]

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.READ)
        return self.snapshot

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        self.capabilities.require(TaskSourceCapability.EVENTS)
        return TaskSourceEvent(
            identity=self.identity,
            event_type="updated",
            occurred_at=1.0,
            snapshot=self.snapshot,
        )

    async def write_owner(self, identity: TaskSourceIdentity, owner: str | None) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        return self.snapshot

    async def write_state(self, identity: TaskSourceIdentity, state: str) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.STATE_WRITE)
        return self.snapshot

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        self.capabilities.require(TaskSourceCapability.COMMENTS)

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        self.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)


class TaskSourceContractTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_adapter_satisfies_runtime_protocol(self) -> None:
        source = _ReferenceTaskSource()
        self.assertIsInstance(source, TaskSource)

    def test_identity_requires_provider_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_type"):
            TaskSourceIdentity(source_type=" ", source_instance="prod", external_id="1")
        with self.assertRaisesRegex(ValueError, "source_instance"):
            TaskSourceIdentity(source_type="jira", source_instance=" ", external_id="1")
        with self.assertRaisesRegex(ValueError, "external_id"):
            TaskSourceIdentity(source_type="jira", source_instance="prod", external_id=" ")

    def test_capabilities_are_explicit_and_fail_closed(self) -> None:
        source = _ReferenceTaskSource()
        self.assertTrue(source.capabilities.supports(TaskSourceCapability.READ))
        self.assertFalse(source.capabilities.supports(TaskSourceCapability.OWNER_WRITE))
        with self.assertRaises(UnsupportedTaskSourceCapability) as raised:
            source.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        self.assertEqual(raised.exception.capability, TaskSourceCapability.OWNER_WRITE)

    async def test_provider_neutral_snapshot_and_event_cross_boundary(self) -> None:
        source = _ReferenceTaskSource()
        discovered = await source.discover(scope="home")
        snapshot = await source.read(source.identity)
        event = await source.normalize_event({"provider": "specific", "ignored": True})

        self.assertEqual(discovered, [snapshot])
        self.assertEqual(snapshot.identity.source_type, "reference")
        self.assertEqual(snapshot.identity.revision, "7")
        self.assertIsNotNone(event)
        self.assertEqual(event.identity.event_cursor, "evt-9")
        self.assertEqual(event.snapshot, snapshot)

    async def test_unsupported_write_operation_fails_before_provider_call(self) -> None:
        source = _ReferenceTaskSource()
        with self.assertRaises(UnsupportedTaskSourceCapability):
            await source.write_owner(source.identity, "quinn")


if __name__ == "__main__":
    unittest.main()
