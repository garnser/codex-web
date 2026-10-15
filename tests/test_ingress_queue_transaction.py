from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from codex_web.conversation_channels import ConversationChannelState, ConversationProjectionReceipt, ConversationProjectionOutcome
from codex_web.models import QueuedTurn
from codex_web.storage.conversation_channels import ConversationChannelStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import state_record_storage_key
from codex_web.storage.turn_queue import TurnQueueRepository


class _MeasuredStore(SQLiteStateStore):
    def reset(self):
        self.decoded_queues = []
        self.upserted = []
        self.selected = []

    def _decode(self, row, default=None):
        result = super()._decode(row, default)
        if hasattr(self, "decoded_queues") and isinstance(result, list) and result and "thread_id" in result[0]:
            self.decoded_queues.append(result[0]["thread_id"])
        return result

    def _upsert(self, connection, namespace, payload):
        if hasattr(self, "upserted"):
            self.upserted.append(namespace)
        return super()._upsert(connection, namespace, payload)

    def record_mutate_many(self, selected, updater):
        self.selected.append(selected)
        return super().record_mutate_many(selected, updater)

    def update_many(self, *args, **kwargs):
        raise AssertionError("queue admission must not replace whole collections")

    def record_items(self, *args, **kwargs):
        raise AssertionError("queue admission must not inspect unrelated rows")


class IngressQueueTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ingress-queue-transaction-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "state.sqlite3"
        plain = SQLiteStateStore(self.database)
        self.store = _MeasuredStore(self.database)
        self.queue = TurnQueueRepository(self.store, self.root / "unused-mirror.json")
        # Thousands of foreign-Project queues make a global scan observable.
        plain.record_replace("turn_queues", {f"unrelated-{index}": [self.item(f"unrelated-{index}", f"foreign-{index}").model_dump(mode="json")]
            for index in range(3000)})
        self.queue.put("target", [self.item("target", "target-project")])
        self.conversations = ConversationChannelStore(plain)
        self.conversations.update(lambda state: self.with_receipts(state))
        self.store.reset()

    @staticmethod
    def item(thread, project, ingress=None):
        return QueuedTurn(id=f"item-{thread}-{ingress or 'prior'}", thread_id=thread, project_id=project,
            message="original request", canonical_ingress_id=ingress, source="slack", created_at=1)

    @staticmethod
    def with_receipts(state):
        for key, org, project in (("ingress", "org-a", "target-project"), ("foreign-receipt", "org-b", "foreign-project")):
            state.receipts[key] = ConversationProjectionReceipt(canonical_event_id=key, organization_id=org,
                workspace_id="workspace", project_id=project, message_key="slack::instance::channel::thread::message",
                outcome=ConversationProjectionOutcome.ROUTING, ingest_actor_id=f"actor-{org}")
        return state

    def raw_rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return dict(connection.execute("SELECT namespace, payload || '|' || updated_at FROM state_documents"))

    def admit(self, hold=None):
        def append(items):
            items.append(self.item("target", "target-project", "ingress"))
            if hold:
                hold[0].set()
                if not hold[1].wait(3):
                    raise TimeoutError("test admission gate not released")
            return items
        return self.queue.update_with_ingress("target", "ingress", append)

    def test_exact_queue_row_atomic_receipt_and_unrelated_rows_untouched(self):
        before = self.raw_rows()
        foreign = self.conversations.load().receipts["foreign-receipt"]
        admitted = self.admit()
        self.assertEqual(len(admitted), 2)
        self.assertEqual(self.store.selected, [{"turn_queues": ("target",), "conversation_channels": (None,)}])
        self.assertEqual(self.store.decoded_queues, ["target"])
        self.assertEqual(set(self.store.upserted) - {"conversation_channels"},
            {state_record_storage_key("turn_queues", "target")})
        after = self.raw_rows()
        for index in range(3000):
            key = state_record_storage_key("turn_queues", f"unrelated-{index}")
            self.assertEqual(after[key], before[key])
        state = self.conversations.load()
        self.assertEqual(state.receipts["foreign-receipt"], foreign)
        self.assertEqual(state.receipts["ingress"].outcome, ConversationProjectionOutcome.QUEUE_ADMITTED)
        self.assertEqual(state.receipts["ingress"].queued_id, admitted[-1].id)
        self.assertFalse(self.store.record_collection_exists("conversation_channels"))

    def test_concurrent_unrelated_append_preserved_and_uncommitted_receipt_not_visible(self):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        other = TurnQueueRepository(SQLiteStateStore(self.database), self.root / "unused-other.json")
        def append_unrelated():
            attempted.set()
            return other.update("unrelated-1499", lambda items: [*items, self.item("unrelated-1499", "foreign-1499", "independent")])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(self.admit, (entered, release))
            try:
                self.assertTrue(entered.wait(2))
                unrelated = pool.submit(append_unrelated)
                self.assertTrue(attempted.wait(2))
                self.assertEqual(self.conversations.load().receipts["ingress"].outcome, ConversationProjectionOutcome.ROUTING)
                self.assertEqual(len(other.get("target")), 1)
                release.set()
                self.assertEqual(len(admission.result(3)), 2)
                self.assertEqual(len(unrelated.result(3)), 2)
            finally:
                release.set()
        self.assertEqual(len(other.get("unrelated-1499")), 2)
        self.assertEqual(self.store.decoded_queues, ["target"])

    def test_invalid_claim_and_scope_roll_back_both_canonical_rows(self):
        before = self.raw_rows()
        with self.assertRaises(ValueError):
            self.queue.update_with_ingress("target", "ingress", lambda items: [*items, self.item("target", "other-project", "ingress")])
        self.assertEqual(before, self.raw_rows())
        self.admit()
        before = self.raw_rows()
        with self.assertRaises(ValueError):
            self.admit()
        self.assertEqual(before, self.raw_rows())

    def test_document_selector_cannot_migrate_collection_or_write_unselected_document(self):
        with self.assertRaises(ValueError):
            self.store.record_mutate_many({"turn_queues": (None,)}, lambda rows: {})
        with self.assertRaises(ValueError):
            self.store.record_mutate_many({"conversation_channels": (None,)}, lambda rows: {"conversation_channels": {"another": {}}})


if __name__ == "__main__":
    unittest.main()
