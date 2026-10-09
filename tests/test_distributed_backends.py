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
    def _store(self, **kwargs):
        assert POSTGRES_DSN is not None
        store = PostgresStateStore(POSTGRES_DSN, **kwargs)
        self.addCleanup(store.close)
        return store

    def setUp(self) -> None:
        assert POSTGRES_DSN is not None
        self.postgres = self._store()
        self._clear_postgres_documents()

    async def test_postgres_control_and_work_item_audit_reads_are_bounded(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from codex_web.autonomy import AutonomyState, AutonomyMode
        from codex_web.models import WorkItemEvent
        from codex_web.storage.autonomy import AutonomyStateStore
        from codex_web.storage.work_item_events import WorkItemEventStore

        initial = AutonomyState()
        self.postgres.put("autonomy", initial.model_dump(mode="json"))
        autonomy = AutonomyStateStore(self.postgres)
        self.assertEqual(autonomy.control().mode, AutonomyMode.ACTIVE)
        with patch.object(self.postgres, "get", side_effect=AssertionError("whole autonomy read")), \
             patch.object(self.postgres, "record_items", side_effect=AssertionError("history read")):
            self.assertEqual(autonomy.control(), initial.control)

        def event(number):
            return WorkItemEvent(ref="test/project#1", event_type="progress_updated",
                                 created_at=number, payload={"number": number})
        events = WorkItemEventStore(self.postgres, max_events=30)
        self.postgres.put(events.legacy_namespace, [event(0).model_dump(mode="json")])
        events._ensure_records()
        with patch.object(self.postgres, "get", side_effect=AssertionError("legacy audit read")), \
             patch.object(self.postgres, "record_items", side_effect=AssertionError("bulk audit read")):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i: events.append(event(i)), range(1, 21)))
        self.assertEqual(len(events.load()), 21)
        self.assertEqual({item.created_at for item in events.load()}, set(range(21)))
        events.flush_legacy_mirror()
        self.assertEqual(len(self.postgres.get(events.legacy_namespace)), 21)

    async def test_postgres_batch_reads_are_selected_readonly_and_legacy_compatible(self):
        from unittest.mock import patch
        for keyed in (False, True):
            namespace = "batch-keyed" if keyed else "batch-legacy"
            payload = {"a/%?": {"value": 1}, "unrequested": {"value": 2}}
            if keyed:
                self.postgres.record_replace(namespace, payload)
                # Unrequested malformed JSON/model data must not be read.
                self.postgres.record_apply(namespace, upserts={"invalid": {"bad": True}})
            else:
                self.postgres.put(namespace, payload)
            before = self.postgres.namespace_revision(namespace)
            with patch.object(self.postgres, "_connection", wraps=self.postgres._connection) as connections:
                self.assertEqual(self.postgres.record_get_many(namespace, ("a/%?", "missing", "a/%?")),
                                 {"a/%?": {"value": 1}})
                self.assertEqual(connections.call_count, 1)
            self.assertEqual(self.postgres.namespace_revision(namespace), before)
        with patch.object(self.postgres, "_connection", side_effect=AssertionError("empty batch I/O")):
            self.assertEqual(self.postgres.record_get_many("batch-keyed", ()), {})
            with self.assertRaises(ValueError):
                self.postgres.record_get_many("batch-keyed", ("a",) * 1001)

    async def test_postgres_action_point_updates_and_concurrent_claims(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from tests.test_action_intents import ActionIntentTests
        from codex_web.action_intents import ActionIntentState, ActionIntentClaimRequest, ActionIntentStatus
        from codex_web.storage.action_intents import ActionIntentStore
        from codex_web.services.action_intents import ActionIntentService
        fixture = ActionIntentTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        intent = fixture._create()
        self.postgres.put(ActionIntentStore.namespace, ActionIntentState(intents=[intent]).model_dump(mode="json"))
        store = ActionIntentStore(self.postgres)
        service = ActionIntentService(store, fixture.execution)
        self.assertEqual(store.get(intent.id), intent)
        with patch.object(self.postgres, "get", side_effect=AssertionError("legacy document read")), \
             patch.object(self.postgres, "record_items", side_effect=AssertionError("whole collection read")):
            def claim(number):
                return service.claim(ActionIntentClaimRequest(worker_id=f"worker-{number}"),
                                     actor=fixture.worker_actor, intent_id=intent.id)
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(claim, range(8)))
            self.assertEqual(len([item for item in results if item is not None]), 1)
            current = store.get(intent.id)
            self.assertEqual(current.status, ActionIntentStatus.CLAIMED)
            before = self.postgres.namespace_revision(store.records_namespace)
            self.assertIsNone(claim(9))
            self.assertEqual(self.postgres.namespace_revision(store.records_namespace), before)
            service._set_status(intent.id, ActionIntentStatus.FAILED, error="test")
            self.assertEqual(store.get(intent.id).status, ActionIntentStatus.FAILED)

    async def test_postgres_usage_point_reads_preserve_migration_without_catalog_scan(self) -> None:
        from unittest.mock import patch
        from codex_web.agent_runtime_usage import AgentRuntimeUsage, AGENT_RUNTIME_USAGE_STATE_CONTRACT
        from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
        usage = AgentRuntimeUsageStore(self.postgres)
        record = AgentRuntimeUsage(id="usage-point", organization_id="org", workspace_id="ws",
                                   provider_id="openai", runtime_id="codex", runtime_type="codex-app-server")
        self.postgres.put(usage.namespace, {"schema_version": AGENT_RUNTIME_USAGE_STATE_CONTRACT.current,
                                           "records": [record.model_dump(mode="json")]})
        self.assertEqual(usage.get(record.id), record)
        self.assertIsNone(self.postgres.document_get(usage.namespace))
        with patch.object(self.postgres, "get", side_effect=AssertionError("catalog get")), \
             patch.object(self.postgres, "record_items", side_effect=AssertionError("catalog enumeration")), \
             patch.object(self.postgres, "record_replace", side_effect=AssertionError("collection replacement")):
            self.assertEqual(usage.get(record.id), record)
            usage.upsert(record.model_copy(update={"input_tokens": 11}))
            self.assertEqual(usage.get(record.id).input_tokens, 11)
            self.assertIsNone(usage.get("absent"))
            with self.assertRaisesRegex(RuntimeError, "another tenant"):
                usage.upsert(record.model_copy(update={"workspace_id": "other"}))

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

    async def test_postgres_pool_reuses_connections_and_rolls_back_failed_mutation(self) -> None:
        pooled = self._store(pool_max_size=1)
        backend_ids = []
        for _ in range(4):
            with pooled._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_ids.append(cursor.fetchone()[0])
        self.assertEqual(len(set(backend_ids)), 1)
        short_lived = self._store(pool_max_size=1)
        with short_lived._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.assertEqual(cursor.fetchone()[0], backend_ids[0])
        short_lived.close()
        with pooled._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                self.assertEqual(cursor.fetchone()[0], 1)
        pooled.put("pool-rollback", {"value": "original"})

        def fail_mutation(raw):
            raw["value"] = "must-not-commit"
            raise RuntimeError("intentional rollback")

        with self.assertRaisesRegex(RuntimeError, "intentional rollback"):
            pooled.update("pool-rollback", fail_mutation, default={})
        self.assertEqual(pooled.get("pool-rollback"), {"value": "original"})
        pooled.update("pool-rollback", lambda _raw: {"value": "next"}, default={})
        self.assertEqual(pooled.get("pool-rollback"), {"value": "next"})
        self.assertEqual(pooled.status()["connectionPool"]["maxSize"], 1)

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
        first_store = self._store()
        second_store = self._store()
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
            self._store()
        )
        consumer_store = CanonicalEventStore(
            self._store()
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
