from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import BotBinding, BotConnection
from codex_web.storage.bot_state import BotStateRepositories
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
