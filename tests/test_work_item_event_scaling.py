from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_web.models import WorkItemEvent
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_item_events import WorkItemEventStore


def event(number):
    return WorkItemEvent(ref="group/app#1", event_type="progress_updated",
                         created_at=float(number), payload={"number": number})


class WorkItemEventScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.db")

    def test_append_at_ten_thousand_records_does_not_read_or_rewrite_history(self):
        store = WorkItemEventStore(self.sqlite)
        legacy = [event(i).model_dump(mode="json") for i in range(10000)]
        self.sqlite.put(store.legacy_namespace, legacy)
        store._ensure_records()
        self.assertTrue(self.sqlite.record_collection_exists(store.namespace))
        with patch.object(self.sqlite, "get", side_effect=AssertionError("bulk read")), \
             patch.object(self.sqlite, "record_items", side_effect=AssertionError("history read")), \
             patch.object(self.sqlite, "update", side_effect=AssertionError("document rewrite")), \
             patch.object(self.sqlite, "record_mutate", wraps=self.sqlite.record_mutate) as mutate:
            store.append(event(10000))
        self.assertEqual(mutate.call_count, 1)
        self.assertEqual(mutate.call_args.args[1], ("meta",))
        retained = store.load()
        self.assertEqual(len(retained), 10000)
        self.assertEqual((retained[0].created_at, retained[-1].created_at), (1, 10000))
        # The old document is a checkpoint, refreshed only when requested.
        self.assertEqual(self.sqlite.get(store.legacy_namespace), legacy)
        store.flush_legacy_mirror()
        self.assertEqual(self.sqlite.get(store.legacy_namespace),
                         [item.model_dump(mode="json") for item in retained])

    def test_parallel_appenders_preserve_every_event_and_a_single_sequence(self):
        stores = [WorkItemEventStore(self.sqlite, max_events=100) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: stores[i % 4].append(event(i)), range(80)))
        retained = stores[0].load()
        self.assertEqual(len(retained), 80)
        self.assertEqual({item.created_at for item in retained}, set(range(80)))
        self.assertEqual(self.sqlite.record_get(stores[0].namespace, "meta")["next_sequence"], 80)

    def test_restart_continues_sequence_and_retention(self):
        store = WorkItemEventStore(self.sqlite, max_events=3)
        for i in range(3):
            store.append(event(i))
        restarted = WorkItemEventStore(self.sqlite, max_events=3)
        restarted.append(event(3))
        self.assertEqual([item.created_at for item in restarted.load()], [1, 2, 3])

    def test_unknown_metadata_fails_without_writing_an_event(self):
        store = WorkItemEventStore(self.sqlite)
        self.sqlite.record_apply(store.namespace, upserts={"meta": {"schema_version": "99"}})
        with self.assertRaises(ValueError):
            store.append(event(1))
        self.assertEqual(self.sqlite.record_count(store.namespace), 1)
