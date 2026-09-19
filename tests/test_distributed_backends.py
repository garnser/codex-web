from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.coordination import CoordinationFenceError
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.event_transport import RedisStreamsEventTransport
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import StateStoreMigrator


POSTGRES_DSN = os.environ.get("CODEX_WEB_TEST_POSTGRES_DSN")
REDIS_URL = os.environ.get("CODEX_WEB_TEST_REDIS_URL")


@unittest.skipUnless(
    POSTGRES_DSN and REDIS_URL,
    "real distributed backends are only exercised when CI supplies PostgreSQL and Redis",
)
class RealDistributedBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        assert POSTGRES_DSN is not None
        self.postgres = PostgresStateStore(POSTGRES_DSN)
        self._clear_postgres_documents()

    def _clear_postgres_documents(self) -> None:
        for namespace in tuple(self.postgres.documents()):
            self.postgres.delete(namespace)

    async def asyncSetUp(self) -> None:
        import redis.asyncio as redis_asyncio  # type: ignore

        assert REDIS_URL is not None
        self.redis = redis_asyncio.from_url(
            REDIS_URL,
            decode_responses=True,
        )
        await self.redis.flushdb()

    async def asyncTearDown(self) -> None:
        await self.redis.aclose()

    async def test_postgres_state_migration_and_shared_fencing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = SQLiteStateStore(Path(root) / "source.sqlite3")
            source.put("alpha", {"value": 1})
            source.put("beta", {"value": ["a", "b"]})

            migrated = StateStoreMigrator().migrate(
                source,
                self.postgres,
            )
            self.assertTrue(migrated.verified)
            self.assertEqual(migrated.copied_documents, 2)
            rerun = StateStoreMigrator().migrate(
                source,
                self.postgres,
            )
            self.assertTrue(rerun.verified)
            self.assertEqual(rerun.copied_documents, 0)
            self.assertEqual(rerun.existing_equal_documents, 2)

        # Reset canonical documents before exercising coordination.
        self._clear_postgres_documents()
        first_store = PostgresStateStore(POSTGRES_DSN)
        second_store = PostgresStateStore(POSTGRES_DSN)
        first = StateStoreCoordinationBackend(first_store)
        second = StateStoreCoordinationBackend(second_store)

        lease_a = first.acquire(
            "scheduler-owner",
            "instance-a",
            lease_seconds=10,
            now=100.0,
        )
        self.assertIsNotNone(lease_a)
        assert lease_a is not None
        self.assertTrue(first.shared)
        self.assertIsNone(
            second.acquire(
                "scheduler-owner",
                "instance-b",
                lease_seconds=10,
                now=100.0,
            )
        )

        first_store.put("protected", {"value": 1})
        first.fenced_update(
            "protected",
            {"value": 0},
            lambda current: {"value": current["value"] + 1},
            lease_key="scheduler-owner",
            owner_id="instance-a",
            fencing_token=lease_a.fencing_token,
            now=100.0,
        )
        lease_b = second.acquire(
            "scheduler-owner",
            "instance-b",
            lease_seconds=10,
            now=111.0,
        )
        self.assertIsNotNone(lease_b)
        assert lease_b is not None
        self.assertGreater(lease_b.fencing_token, lease_a.fencing_token)
        with self.assertRaises(CoordinationFenceError):
            first.fenced_update(
                "protected",
                {"value": 0},
                lambda current: {"value": 999},
                lease_key="scheduler-owner",
                owner_id="instance-a",
                fencing_token=lease_a.fencing_token,
                now=111.0,
            )
        self.assertEqual(first_store.get("protected"), {"value": 2})

    async def test_redis_pending_delivery_takeover_and_ack(self) -> None:
        stream = f"codex-web:test:{uuid.uuid4().hex}"
        group = f"group:{uuid.uuid4().hex}"
        first = RedisStreamsEventTransport(
            self.redis,
            stream=stream,
            group=group,
            claim_idle_ms=20,
        )
        second = RedisStreamsEventTransport(
            self.redis,
            stream=stream,
            group=group,
            claim_idle_ms=20,
        )
        event = CanonicalEventEnvelope(
            event_id="evt-pending-takeover",
            event_type="test.event",
            occurred_at=1.0,
            source="test",
            tenant_id="org-a",
            workspace_id="ws-a",
            payload={"value": 1},
        )

        await first.publish(event)
        initial = await first.consume(
            "consumer-a",
            limit=1,
            timeout_seconds=0.01,
        )
        self.assertEqual(len(initial), 1)
        # Deliberately do not acknowledge. Another replica should reclaim it.
        await asyncio.sleep(0.05)
        reclaimed = await second.consume(
            "consumer-b",
            limit=1,
            timeout_seconds=0.01,
        )
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(
            reclaimed[0].canonical_event_id,
            event.event_id,
        )
        self.assertEqual(
            reclaimed[0].transport_message_id,
            initial[0].transport_message_id,
        )
        acknowledged = await second.acknowledge(
            reclaimed[0],
            consumer_id="consumer-b",
        )
        self.assertIsNotNone(acknowledged.acknowledged_at)

    async def test_postgres_plus_redis_dispatches_canonical_event_once_across_replicas(self) -> None:
        self._clear_postgres_documents()
        stream = f"codex-web:test:{uuid.uuid4().hex}"
        group = f"group:{uuid.uuid4().hex}"
        producer_transport = RedisStreamsEventTransport(
            self.redis,
            stream=stream,
            group=group,
            claim_idle_ms=20,
        )
        consumer_transport = RedisStreamsEventTransport(
            self.redis,
            stream=stream,
            group=group,
            claim_idle_ms=20,
        )
        producer_store = CanonicalEventStore(
            PostgresStateStore(POSTGRES_DSN)
        )
        consumer_store = CanonicalEventStore(
            PostgresStateStore(POSTGRES_DSN)
        )
        producer_bus = CanonicalEventBus(
            producer_store,
            transport=producer_transport,
            instance_id="instance-a",
        )
        consumer_bus = CanonicalEventBus(
            consumer_store,
            transport=consumer_transport,
            instance_id="instance-b",
        )
        ingestion = CanonicalEventIngestionService(producer_bus)
        handled: list[str] = []
        consumer_bus.subscribe(lambda event: handled.append(event.event_id))

        delivery = await ingestion.ingest(
            event_type="test.event",
            source="integration-test",
            idempotency_key="event-1",
            payload={"value": 1},
            tenant_id="org-a",
            workspace_id="ws-a",
        )
        self.assertEqual(delivery.dispatched, 0)
        consumed = await consumer_bus.consume_transport_once(
            "instance-b",
            limit=10,
            timeout_seconds=0.01,
        )
        self.assertEqual(consumed["dispatched"], 1)
        self.assertEqual(handled, [delivery.event.event_id])

        # Publish a second broker message with a new Redis stream ID but the
        # exact same canonical event. Canonical inbox identity must suppress it.
        replay = producer_store.event(delivery.event.event_id)
        assert replay is not None
        await producer_transport.publish(replay)
        replayed = await consumer_bus.consume_transport_once(
            "instance-c",
            limit=10,
            timeout_seconds=0.01,
        )
        self.assertEqual(replayed["received"], 1)
        self.assertEqual(replayed["dispatched"], 0)
        self.assertEqual(handled, [delivery.event.event_id])

        health = await consumer_transport.health()
        self.assertTrue(health.healthy)
        self.assertTrue(
            consumer_transport.capabilities.consumer_groups
        )
        self.assertEqual(
            consumer_store.outbox_status()["published"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
