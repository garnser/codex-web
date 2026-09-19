from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.models import Project, WorkItemEvent
from codex_web.coordination import CoordinationFenceError
from codex_web.event_transport import (
    EventTransportCapabilities,
    EventTransportError,
    EventTransportHealth,
    TransportDelivery,
    TransportDeliveryStatus,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.event_transport import (
    InProcessEventTransport,
    RedisStreamsEventTransport,
)
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import StateStoreMigrator


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class StateStoreMigrationTests(unittest.TestCase):
    def test_sqlite_migration_is_verified_idempotent_and_rollback_safe(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = SQLiteStateStore(Path(root) / "source.sqlite3")
            target = SQLiteStateStore(Path(root) / "target.sqlite3")
            source.put("a", {"value": 1})
            source.put("b", {"nested": ["x", "y"]})

            migrator = StateStoreMigrator()
            first = migrator.migrate(source, target)
            self.assertTrue(first.verified)
            self.assertEqual(first.copied_documents, 2)
            self.assertEqual(first.existing_equal_documents, 0)
            self.assertEqual(source.documents(), target.documents())

            second = migrator.migrate(source, target)
            self.assertTrue(second.verified)
            self.assertEqual(second.copied_documents, 0)
            self.assertEqual(second.existing_equal_documents, 2)

            removed = migrator.rollback_destination(source, target, first)
            self.assertEqual(set(removed), {"a", "b"})
            self.assertEqual(target.documents(), {})


class SharedCanonicalStateTests(unittest.TestCase):
    def test_project_registry_uses_shared_store_as_authority(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            store = SQLiteStateStore(path / "shared.sqlite3")
            legacy = path / "projects.json"
            repository = ProjectRepository(legacy, store=store)
            repository.save(
                [
                    Project(
                        id="alpha",
                        name="Alpha",
                        path=str(path / "alpha"),
                    )
                ]
            )
            self.assertTrue(legacy.exists())
            legacy.unlink()

            restarted = ProjectRepository(legacy, store=store)
            loaded = restarted.load()
            self.assertEqual([item.id for item in loaded], ["alpha"])
            self.assertEqual(store.get("projects")[0]["id"], "alpha")

    def test_work_item_event_journal_is_shared_store_primary_with_jsonl_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            store = SQLiteStateStore(path / "shared.sqlite3")
            host = SimpleNamespace(
                DATA_DIR=path,
                WORK_ITEM_EVENTS_FILE=path / "work-item-events.jsonl",
            )
            machine = WorkItemStateMachine(host, store=store)
            event = WorkItemEvent(
                ref="work-1",
                event_type="progress",
                created_at=100.0,
                actor="agent-a",
                payload={"stage": "implementation_active"},
            )
            machine._append_work_item_event(event)

            rows = store.get("work_item_events")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ref"], "work-1")
            self.assertTrue(host.WORK_ITEM_EVENTS_FILE.exists())


class CoordinationFailoverTests(unittest.TestCase):
    def test_two_instances_take_over_with_monotonic_fence_and_stale_owner_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "shared.sqlite3"
            store_a = SQLiteStateStore(path)
            store_b = SQLiteStateStore(path)
            clock = _Clock(100.0)
            backend_a = StateStoreCoordinationBackend(store_a, clock=clock)
            backend_b = StateStoreCoordinationBackend(store_b, clock=clock)

            lease_a = backend_a.acquire(
                "worker:critical",
                "instance-a",
                lease_seconds=10,
            )
            self.assertIsNotNone(lease_a)
            assert lease_a is not None
            self.assertIsNone(
                backend_b.acquire(
                    "worker:critical",
                    "instance-b",
                    lease_seconds=10,
                )
            )

            store_a.put("protected", {"value": 0})
            updated = backend_a.fenced_update(
                "protected",
                {"value": 0},
                lambda current: {"value": current["value"] + 1},
                lease_key="worker:critical",
                owner_id="instance-a",
                fencing_token=lease_a.fencing_token,
            )
            self.assertEqual(updated["value"], 1)

            clock.value = 111.0
            lease_b = backend_b.acquire(
                "worker:critical",
                "instance-b",
                lease_seconds=10,
            )
            self.assertIsNotNone(lease_b)
            assert lease_b is not None
            self.assertGreater(lease_b.fencing_token, lease_a.fencing_token)

            with self.assertRaises(CoordinationFenceError):
                backend_a.fenced_update(
                    "protected",
                    {"value": 0},
                    lambda current: {"value": 999},
                    lease_key="worker:critical",
                    owner_id="instance-a",
                    fencing_token=lease_a.fencing_token,
                )
            self.assertEqual(store_a.get("protected"), {"value": 1})

            backend_b.fenced_update(
                "protected",
                {"value": 0},
                lambda current: {"value": current["value"] + 1},
                lease_key="worker:critical",
                owner_id="instance-b",
                fencing_token=lease_b.fencing_token,
            )
            self.assertEqual(store_b.get("protected"), {"value": 2})

    def test_replicated_ownership_transfers_responsibility_after_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "shared.sqlite3"
            clock = _Clock(100.0)
            first = ReplicatedOwnershipService(
                StateStoreCoordinationBackend(
                    SQLiteStateStore(path),
                    clock=clock,
                ),
                instance_id="a",
                lease_seconds=9,
                clock=clock,
            )
            second = ReplicatedOwnershipService(
                StateStoreCoordinationBackend(
                    SQLiteStateStore(path),
                    clock=clock,
                ),
                instance_id="b",
                lease_seconds=9,
                clock=clock,
            )
            self.assertTrue(first.owns("release-gate"))
            self.assertFalse(second.owns("release-gate"))
            first_token = first.require_fence("release-gate").fencing_token

            clock.value = 110.0
            self.assertTrue(second.owns("release-gate"))
            second_token = second.require_fence("release-gate").fencing_token
            self.assertGreater(second_token, first_token)
            with self.assertRaises(CoordinationFenceError):
                first.require_fence("release-gate")




class ReplicatedExclusiveExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_replica_executes_same_singleton_responsibility(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "shared.sqlite3"
            first = ReplicatedOwnershipService(
                StateStoreCoordinationBackend(SQLiteStateStore(path)),
                instance_id="instance-a",
                lease_seconds=30,
            )
            second = ReplicatedOwnershipService(
                StateStoreCoordinationBackend(SQLiteStateStore(path)),
                instance_id="instance-b",
                lease_seconds=30,
            )
            started = asyncio.Event()
            finish = asyncio.Event()
            executions: list[str] = []

            async def first_operation() -> None:
                executions.append("a")
                started.set()
                await finish.wait()

            first_task = asyncio.create_task(
                first.run_exclusive("critical-loop", first_operation)
            )
            await started.wait()

            ran_second, _ = await second.run_exclusive(
                "critical-loop",
                lambda: asyncio.sleep(0),
            )
            self.assertFalse(ran_second)
            self.assertEqual(executions, ["a"])

            finish.set()
            ran_first, _ = await first_task
            self.assertTrue(ran_first)
            self.assertEqual(executions, ["a"])

class _FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.acked: list[tuple[str, str, str]] = []
        self.groups: set[tuple[str, str]] = set()
        self.counter = 0

    async def xgroup_create(self, stream, group, id="0", mkstream=False):
        key = (stream, group)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add(key)
        if mkstream:
            self.streams.setdefault(stream, [])
        return True

    async def xadd(self, stream, fields):
        self.counter += 1
        message_id = f"{self.counter}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xreadgroup(self, group, consumer, streams, count=10, block=0):
        del group, consumer, block
        stream = next(iter(streams))
        rows = self.streams.get(stream, [])[:count]
        self.streams[stream] = self.streams.get(stream, [])[len(rows):]
        return [(stream, rows)] if rows else []

    async def xack(self, stream, group, message_id):
        self.acked.append((stream, group, message_id))
        return 1

    async def ping(self):
        return True


class EventTransportConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise(self, transport) -> None:
        event = CanonicalEventEnvelope(
            event_id="evt-transport",
            event_type="test.event",
            occurred_at=1.0,
            source="test",
            correlation_id="corr",
            tenant_id="org",
            workspace_id="ws",
            payload={"value": 1},
        )
        published = await transport.publish(event)
        self.assertEqual(published.canonical_event_id, event.event_id)
        rows = await transport.consume(
            "consumer-a",
            limit=10,
            timeout_seconds=0.01,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event.event_id, event.event_id)
        acknowledged = await transport.acknowledge(
            rows[0],
            consumer_id="consumer-a",
        )
        self.assertEqual(
            acknowledged.status,
            TransportDeliveryStatus.ACKNOWLEDGED,
        )
        health = await transport.health()
        self.assertTrue(health.healthy)

    async def test_in_process_transport_conformance(self) -> None:
        await self._exercise(InProcessEventTransport())

    async def test_redis_streams_transport_conformance(self) -> None:
        transport = RedisStreamsEventTransport(_FakeRedis())
        await self._exercise(transport)
        self.assertTrue(transport.capabilities.durable)
        self.assertTrue(transport.capabilities.consumer_groups)


class _SwitchTransport:
    backend_id = "switch"
    capabilities = EventTransportCapabilities(
        durable=True,
        acknowledgement=True,
        negative_acknowledgement=True,
        dead_letter=True,
        consumer_groups=True,
        ordering="fifo",
    )

    def __init__(self) -> None:
        self.fail_publish = True
        self.queue: list[TransportDelivery] = []
        self.counter = 0

    async def publish(self, event, *, attempt=1):
        if self.fail_publish:
            raise EventTransportError("transport down")
        self.counter += 1
        item = TransportDelivery(
            backend_id=self.backend_id,
            canonical_event_id=event.event_id,
            transport_message_id=f"msg-{self.counter}",
            attempt=attempt,
            event=event,
        )
        self.queue.append(item)
        return item

    async def consume(self, consumer_id, *, limit=10, timeout_seconds=1.0):
        del consumer_id, timeout_seconds
        rows = self.queue[:limit]
        self.queue = self.queue[len(rows):]
        return tuple(rows)

    async def acknowledge(self, delivery, *, consumer_id):
        del consumer_id
        return delivery.model_copy(
            update={"status": TransportDeliveryStatus.ACKNOWLEDGED}
        )

    async def negative_acknowledge(
        self,
        delivery,
        *,
        consumer_id,
        reason,
        retry=True,
    ):
        del consumer_id, retry
        return delivery.model_copy(
            update={
                "status": TransportDeliveryStatus.DEAD_LETTER,
                "reason": reason,
            }
        )

    async def health(self):
        return EventTransportHealth(
            backend_id=self.backend_id,
            healthy=not self.fail_publish,
            degraded=self.fail_publish,
        )


class DurableOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_before_publish_recovers_after_transport_returns(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteStateStore(Path(root) / "state.sqlite3")
            events = CanonicalEventStore(store)
            transport = _SwitchTransport()
            bus = CanonicalEventBus(
                events,
                transport=transport,
                outbox_backoff_seconds=0,
            )
            ingestion = CanonicalEventIngestionService(bus)
            handled: list[str] = []
            bus.subscribe(lambda event: handled.append(event.event_id))

            delivery = await ingestion.ingest(
                event_type="test.event",
                source="test",
                idempotency_key="one",
                payload={"value": 1},
                tenant_id="org",
                workspace_id="ws",
            )
            self.assertTrue(delivery.inserted)
            self.assertTrue(delivery.transport_pending)
            self.assertEqual(handled, [])
            self.assertEqual(events.outbox_status()["pending"], 1)
            self.assertIsNotNone(events.event(delivery.event.event_id))

            transport.fail_publish = False
            recovered = await bus.dispatch_outbox_once(now=10_000_000_000.0)
            self.assertEqual(recovered["published"], 1)
            self.assertEqual(events.outbox_status()["published"], 1)

            consumed = await bus.consume_transport_once(
                "instance-b",
                timeout_seconds=0,
            )
            self.assertEqual(consumed["received"], 1)
            self.assertEqual(consumed["dispatched"], 1)
            self.assertEqual(handled, [delivery.event.event_id])

    async def test_atomic_state_event_outbox_replay_does_not_repeat_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteStateStore(Path(root) / "state.sqlite3")
            events = CanonicalEventStore(store)
            transport = _SwitchTransport()
            bus = CanonicalEventBus(
                events,
                transport=transport,
                outbox_backoff_seconds=0,
            )
            ingestion = CanonicalEventIngestionService(bus)

            updated, first = await ingestion.ingest_with_mutation(
                namespace="counter",
                default={"value": 0},
                updater=lambda current: {"value": current["value"] + 1},
                event_type="counter.changed",
                source="test",
                idempotency_key="mutation-1",
                payload={"counter": "counter"},
                tenant_id="org",
                workspace_id="ws",
            )
            self.assertEqual(updated, {"value": 1})
            self.assertTrue(first.inserted)
            self.assertEqual(store.get("counter"), {"value": 1})
            self.assertEqual(events.outbox_status()["pending"], 1)

            replayed, second = await ingestion.ingest_with_mutation(
                namespace="counter",
                default={"value": 0},
                updater=lambda current: {"value": current["value"] + 100},
                event_type="counter.changed",
                source="test",
                idempotency_key="mutation-1",
                payload={"counter": "counter"},
                tenant_id="org",
                workspace_id="ws",
            )
            self.assertFalse(second.inserted)
            self.assertEqual(replayed, {"value": 1})
            self.assertEqual(store.get("counter"), {"value": 1})

    async def test_duplicate_transport_delivery_is_inbox_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteStateStore(Path(root) / "state.sqlite3")
            events = CanonicalEventStore(store)
            transport = _SwitchTransport()
            transport.fail_publish = False
            bus = CanonicalEventBus(events, transport=transport)
            ingestion = CanonicalEventIngestionService(bus)
            handled: list[str] = []
            bus.subscribe(lambda event: handled.append(event.event_id))

            delivery = await ingestion.ingest(
                event_type="test.event",
                source="test",
                idempotency_key="duplicate",
                payload={},
                tenant_id="org",
                workspace_id="ws",
            )
            original = transport.queue[0]
            transport.queue.append(original.model_copy())

            first = await bus.consume_transport_once(
                "consumer-a",
                limit=10,
                timeout_seconds=0,
            )
            self.assertEqual(first["received"], 2)
            self.assertEqual(first["dispatched"], 1)
            self.assertEqual(handled, [delivery.event.event_id])


if __name__ == "__main__":
    unittest.main()
