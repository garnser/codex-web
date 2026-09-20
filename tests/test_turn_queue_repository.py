from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import QueuedTurn
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class TurnQueueRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = TurnQueueRepository(
            SQLiteStateStore(root / "state.sqlite3"),
            root / "turn_queue.json",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def queued(turn_id: str, thread_id: str) -> QueuedTurn:
        return QueuedTurn(
            id=turn_id,
            thread_id=thread_id,
            project_id="home",
            message=f"message-{turn_id}",
            created_at=1.0,
        )

    def test_round_trip_and_mirror(self) -> None:
        item = self.queued("q1", "thread-a")
        self.repo.save({"thread-a": [item]})
        self.assertEqual(self.repo.load(), {"thread-a": [item]})
        self.assertTrue(self.repo.legacy_path.exists())

    def test_concurrent_threads_merge_independently(self) -> None:
        first = self.queued("q1", "thread-a")
        second = self.queued("q2", "thread-b")
        self.repo.save({"thread-a": [first]})

        left = self.repo.load()
        right = self.repo.load()
        left["thread-b"] = [second]
        self.repo.save(left)

        right["thread-a"].append(self.queued("q3", "thread-a"))
        self.repo.save(right)

        rows = self.repo.load()
        self.assertEqual([item.id for item in rows["thread-a"]], ["q1", "q3"])
        self.assertEqual([item.id for item in rows["thread-b"]], ["q2"])


if __name__ == "__main__":
    unittest.main()
