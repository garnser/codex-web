from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_web.canonical_events import (
    CanonicalEventInboxReceipt,
    CanonicalEventOutboxRecord,
    CanonicalEventOutboxStatus,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.storage.canonical_events import CanonicalEventState, CanonicalEventStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _event(number: int) -> CanonicalEventEnvelope:
    return CanonicalEventEnvelope(
        event_id=f"evt-{number}",
        event_type="test.event",
        source="scaling-test",
        occurred_at=float(number),
        payload={"number": number},
    )


class _ObservedSQLiteStateStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.calls: dict[str, int] = {}

    def _called(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def get(self, namespace):
        self._called("get")
        return super().get(namespace)

    def record_items(self, namespace):
        self._called("record_items")
        return super().record_items(namespace)

    def record_page(self, namespace, **kwargs):
        self._called("record_page")
        return super().record_page(namespace, **kwargs)

    def record_get(self, namespace, key):
        self._called("record_get")
        return super().record_get(namespace, key)

    def record_mutate(self, namespace, keys, updater):
        self._called("record_mutate")
        return super().record_mutate(namespace, keys, updater)


class CanonicalEventScalingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"

    def test_legacy_document_migrates_and_explicit_checkpoint_supports_rollback(self):
        sqlite = SQLiteStateStore(self.path)
        event = _event(1)
        outbox = CanonicalEventOutboxRecord(event_id=event.event_id, created_at=1.0)
        receipt = CanonicalEventInboxReceipt(
            event_id=event.event_id,
            transport_backend_id="transport-a",
            transport_delivery_id="delivery-a",
            consumer_id="consumer-a",
            acknowledged_at=2.0,
        )
        sqlite.put(
            CanonicalEventStore.namespace,
            CanonicalEventState(
                events=[event],
                idempotency={"legacy-key": event.event_id},
                outbox={event.event_id: outbox},
                inbox=[receipt],
            ).model_dump(mode="json"),
        )

        events = CanonicalEventStore(sqlite)
        self.assertEqual(events.event(event.event_id), event)
        self.assertEqual(events.pending_outbox(now=10.0), [outbox])
        self.assertTrue(events.inbox_seen(event_id=event.event_id, backend_id="transport-a"))
        events.mark_outbox_published(
            event.event_id,
            backend_id="transport-a",
            delivery_id="delivery-a",
            now=3.0,
        )
        events.flush_legacy_mirror()

        checkpoint = CanonicalEventState.model_validate(
            sqlite.get(CanonicalEventStore.namespace)
        )
        self.assertEqual(
            checkpoint.outbox[event.event_id].status,
            CanonicalEventOutboxStatus.PUBLISHED,
        )

    def test_point_reads_and_outbox_mutation_do_not_load_retained_collection(self):
        sqlite = _ObservedSQLiteStateStore(self.path)
        events = CanonicalEventStore(sqlite, max_events=500)
        for number in range(200):
            events.record_if_new(
                _event(number),
                idempotency_key=f"key-{number}",
                enqueue_transport=True,
            )
        sqlite.calls.clear()

        self.assertEqual(events.event("evt-199"), _event(199))
        self.assertEqual(len(events.pending_outbox(now=1_000.0, limit=5)), 5)
        events.mark_outbox_published(
            "evt-199",
            backend_id="transport-a",
            delivery_id="delivery-199",
            now=1_000.0,
        )

        self.assertEqual(sqlite.calls.get("get", 0), 0)
        self.assertEqual(sqlite.calls.get("record_items", 0), 0)
        self.assertEqual(sqlite.calls.get("record_page", 0), 1)
        self.assertEqual(sqlite.calls.get("record_mutate", 0), 1)

    def test_pending_index_orders_retry_time_without_starving_ready_work(self):
        events = CanonicalEventStore(SQLiteStateStore(self.path))
        for number in (1, 2):
            events.record_if_new(
                _event(number),
                idempotency_key=f"key-{number}",
                enqueue_transport=True,
            )
        events.mark_outbox_failed(
            "evt-1",
            error_code="unavailable",
            now=10.0,
            max_attempts=3,
            backoff_seconds=100.0,
        )

        self.assertEqual(
            [item.event_id for item in events.pending_outbox(now=20.0)],
            ["evt-2"],
        )

    def test_concurrent_replay_inserts_one_canonical_event(self):
        event = _event(1)

        def insert() -> bool:
            store = CanonicalEventStore(SQLiteStateStore(self.path))
            return store.record_if_new(
                event,
                idempotency_key="same-key",
                enqueue_transport=True,
            )[1]

        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = list(pool.map(lambda _: insert(), range(2)))

        self.assertEqual(sorted(inserted), [False, True])
        restarted = CanonicalEventStore(SQLiteStateStore(self.path))
        self.assertEqual(restarted.event(event.event_id), event)
        self.assertEqual(restarted.outbox_status()["pending"], 1)

    def test_unknown_keyed_schema_fails_visibly(self):
        sqlite = SQLiteStateStore(self.path)
        sqlite.record_apply(
            CanonicalEventStore.records_namespace,
            upserts={"meta": {"schema_version": "99.0"}},
        )

        with self.assertRaises(ValueError):
            CanonicalEventStore(sqlite).event("missing")


if __name__ == "__main__":
    unittest.main()
