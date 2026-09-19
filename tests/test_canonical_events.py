from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.canonical_events import CanonicalEventType
from codex_web.models import TaskSourceIdentity
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.task_sources import TaskSourceEvent, TaskSourceSnapshot
from codex_web.storage.canonical_events import (
    CanonicalEventConflictError,
    CanonicalEventStore,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class CanonicalEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = CanonicalEventStore(SQLiteStateStore(self.path))
        self.bus = CanonicalEventBus(self.store)
        self.ingestion = CanonicalEventIngestionService(self.bus)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_repeated_delivery_is_persisted_and_dispatched_once(self) -> None:
        seen = []
        self.bus.subscribe(seen.append, event_types=[CanonicalEventType.CI_PIPELINE])

        first = await self.ingestion.ingest(
            event_type=CanonicalEventType.CI_PIPELINE,
            source="fixture",
            idempotency_key="delivery-1",
            payload={"status": "success"},
            occurred_at=100.0,
        )
        second = await self.ingestion.ingest(
            event_type=CanonicalEventType.CI_PIPELINE,
            source="fixture",
            idempotency_key="delivery-1",
            payload={"status": "success"},
            occurred_at=200.0,
        )

        self.assertTrue(first.inserted)
        self.assertEqual(first.dispatched, 1)
        self.assertFalse(second.inserted)
        self.assertEqual(second.dispatched, 0)
        self.assertEqual(second.event.event_id, first.event.event_id)
        self.assertEqual(second.event.occurred_at, 100.0)
        self.assertEqual([item.event_id for item in seen], [first.event.event_id])
        self.assertEqual(len(self.store.recent()), 1)

    async def test_idempotency_key_reuse_with_different_payload_fails_closed(self) -> None:
        await self.ingestion.ingest(
            event_type=CanonicalEventType.DEPLOYMENT,
            source="fixture",
            idempotency_key="delivery-2",
            payload={"status": "running"},
            occurred_at=100.0,
        )

        with self.assertRaises(CanonicalEventConflictError):
            await self.ingestion.ingest(
                event_type=CanonicalEventType.DEPLOYMENT,
                source="fixture",
                idempotency_key="delivery-2",
                payload={"status": "failed"},
                occurred_at=101.0,
            )

    async def test_type_and_predicate_filters_are_deterministic(self) -> None:
        seen = []
        self.bus.subscribe(
            seen.append,
            event_types=[CanonicalEventType.FAILURE],
            predicate=lambda event: event.payload.get("severity") == "critical",
        )

        await self.ingestion.ingest(
            event_type=CanonicalEventType.CI_PIPELINE,
            source="fixture",
            idempotency_key="pipeline",
            payload={"severity": "critical"},
            occurred_at=1.0,
        )
        await self.ingestion.ingest(
            event_type=CanonicalEventType.FAILURE,
            source="fixture",
            idempotency_key="warning",
            payload={"severity": "warning"},
            occurred_at=2.0,
        )
        delivered = await self.ingestion.ingest(
            event_type=CanonicalEventType.FAILURE,
            source="fixture",
            idempotency_key="critical",
            payload={"severity": "critical"},
            occurred_at=3.0,
        )

        self.assertEqual(delivered.dispatched, 1)
        self.assertEqual([item.event_id for item in seen], [delivered.event.event_id])

    async def test_restart_preserves_deduplication(self) -> None:
        first = await self.ingestion.ingest(
            event_type=CanonicalEventType.PULL_REQUEST,
            source="fixture",
            idempotency_key="pr-1",
            payload={"action": "open"},
            occurred_at=5.0,
        )

        restarted_store = CanonicalEventStore(SQLiteStateStore(self.path))
        restarted = CanonicalEventIngestionService(CanonicalEventBus(restarted_store))
        duplicate = await restarted.ingest(
            event_type=CanonicalEventType.PULL_REQUEST,
            source="fixture",
            idempotency_key="pr-1",
            payload={"action": "open"},
            occurred_at=99.0,
        )

        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.event.event_id, first.event.event_id)
        self.assertEqual(len(restarted_store.recent()), 1)

    async def test_task_source_event_is_normalized_without_provider_payload(self) -> None:
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#42",
            revision="rev-1",
        )
        source_event = TaskSourceEvent(
            identity=identity,
            event_type="issue.update",
            occurred_at=10.0,
            snapshot=TaskSourceSnapshot(
                identity=identity,
                title="Canonical work",
                source_state="opened",
                owners=("developer",),
                labels=("status::in progress",),
            ),
        )

        delivery = await self.ingestion.ingest_task_source(
            source_event,
            project_id="project-a",
            tenant_id="org-a",
            workspace_id="ws-a",
            event_cursor="gitlab-delivery-1",
        )

        self.assertEqual(delivery.event.event_type, CanonicalEventType.TASK_SOURCE.value)
        self.assertEqual(delivery.event.tenant_id, "org-a")
        self.assertEqual(delivery.event.workspace_id, "ws-a")
        self.assertEqual(delivery.event.payload["provider_event_type"], "issue.update")
        self.assertEqual(
            delivery.event.payload["identity"]["external_id"],
            "group/project#42",
        )
        self.assertNotIn("raw_payload", delivery.event.payload)

    async def test_requested_canonical_event_classes_are_code_owned(self) -> None:
        self.assertEqual(
            {item.value for item in CanonicalEventType},
            {
                "work.transition",
                "code.pull_request",
                "ci.pipeline",
                "deployment.status",
                "incident.status",
                "failure.observed",
                "task_source.event",
                "schedule.due",
                "approval.transition",
                "attention.transition",
            },
        )


if __name__ == "__main__":
    unittest.main()
