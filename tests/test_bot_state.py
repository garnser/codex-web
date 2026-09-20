from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import BotBinding, BotConnection
from codex_web.storage.bot_state import (
    BotStateRepositories,
    IndexedBotBindingRepository,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class BotStateRepositoriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / "state.sqlite3")
        self.state = BotStateRepositories(
            self.store,
            connections_file=root / "bot_connections.json",
            bindings_file=root / "bot_bindings.json",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_round_trip_and_json_mirror(self) -> None:
        connection = BotConnection(
            id="conn-1",
            provider="slack",
            name="Slack",
            project_id="home",
            created_at=1.0,
            updated_at=1.0,
        )
        binding = BotBinding(
            id="binding-1",
            connection_id=connection.id,
            provider="slack",
            external_conversation_id="C123",
            thread_id="thread-1",
            project_id="home",
            created_at=1.0,
            updated_at=1.0,
        )
        self.state.connections.save([connection])
        self.state.bindings.save([binding])

        self.assertEqual(self.state.connections.load(), [connection])
        self.assertEqual(self.state.bindings.load(), [binding])
        self.assertTrue(self.state.connections.legacy_path.exists())
        self.assertTrue(self.state.bindings.legacy_path.exists())

    def test_indexed_bindings_build_once_and_cover_routing_dimensions(self) -> None:
        bindings = [
            BotBinding(
                id=f"binding-{index}",
                provider="slack" if index % 2 == 0 else "telegram",
                external_conversation_id=f"C{index % 25}",
                thread_id=f"thread-{index % 100}",
                project_id=f"project-{index % 10}",
                is_master=index % 97 == 0,
                created_at=float(index),
                updated_at=float(index),
            )
            for index in range(1000)
        ]
        self.state.bindings.save(bindings)
        indexed = IndexedBotBindingRepository(self.state.bindings)

        original_raw = self.state.bindings._raw
        reads = 0

        def counted_raw():
            nonlocal reads
            reads += 1
            return original_raw()

        self.state.bindings._raw = counted_raw

        self.assertEqual(indexed.by_id("binding-500").id, "binding-500")
        self.assertTrue(indexed.for_thread("thread-1"))
        self.assertTrue(indexed.for_project("slack", "project-0"))
        self.assertTrue(indexed.for_connection("slack", "C0"))
        indexed.masters("project-0")
        self.assertEqual(reads, 1)

        # Repeated routing lookups validate the revision without rebuilding the
        # thousand-binding index.
        indexed.for_thread("thread-1")
        indexed.for_project("slack", "project-0")
        self.assertEqual(reads, 1)

    def test_indexed_bindings_invalidate_after_local_and_external_mutation(self) -> None:
        first = BotBinding(
            id="binding-a",
            provider="slack",
            external_conversation_id="C1",
            thread_id="thread-a",
            project_id="p1",
            created_at=1.0,
            updated_at=1.0,
        )
        self.state.bindings.save([first])
        indexed = IndexedBotBindingRepository(self.state.bindings)
        self.assertEqual(indexed.for_thread("thread-a")[0].id, "binding-a")

        local = indexed.load()
        local.append(
            BotBinding(
                id="binding-b",
                provider="slack",
                external_conversation_id="C2",
                thread_id="thread-b",
                project_id="p1",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        indexed.save(local)
        self.assertEqual(indexed.for_thread("thread-b")[0].id, "binding-b")

        # Simulate another process writing the canonical namespace without
        # touching this process's in-memory index.
        external = self.state.bindings.load()
        external.append(
            BotBinding(
                id="binding-c",
                provider="telegram",
                external_conversation_id="chat-c",
                thread_id="thread-c",
                project_id="p2",
                created_at=3.0,
                updated_at=3.0,
            )
        )
        self.state.bindings.save(external)

        self.assertEqual(indexed.for_thread("thread-c")[0].id, "binding-c")

    def test_concurrent_binding_updates_merge_by_id(self) -> None:
        first = BotBinding(
            id="binding-a",
            provider="slack",
            external_conversation_id="C1",
            thread_id="thread-a",
            created_at=1.0,
            updated_at=1.0,
        )
        second = BotBinding(
            id="binding-b",
            provider="telegram",
            external_conversation_id="chat-2",
            thread_id="thread-b",
            created_at=1.0,
            updated_at=1.0,
        )
        self.state.bindings.save([first])

        left = self.state.bindings.load()
        right = self.state.bindings.load()
        left.append(second)
        self.state.bindings.save(left)

        right[0] = right[0].model_copy(
            update={"thread_name": "renamed", "updated_at": 2.0}
        )
        self.state.bindings.save(right)

        rows = {item.id: item for item in self.state.bindings.load()}
        self.assertEqual(set(rows), {"binding-a", "binding-b"})
        self.assertEqual(rows["binding-a"].thread_name, "renamed")


if __name__ == "__main__":
    unittest.main()
