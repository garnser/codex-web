from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.models import BotThreadDetail
from codex_web.storage.auxiliary_state import (
    JsonMapRepository,
    NestedModelListMapRepository,
    ServiceDeskStateRepository,
    _normalize_semantic_events,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class AuxiliaryStateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStateStore(self.root / "state.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def detail(thread_id: str, text: str) -> BotThreadDetail:
        return BotThreadDetail(
            thread_id=thread_id,
            item_type="commandExecution",
            title="Command details",
            text=text,
            created_at=1.0,
        )

    def test_model_list_maps_merge_independent_thread_updates(self) -> None:
        legacy = self.root / "bot_details.json"
        first = NestedModelListMapRepository(
            self.store,
            namespace="bot_details",
            legacy_path=legacy,
            model=BotThreadDetail,
        )
        second = NestedModelListMapRepository(
            self.store,
            namespace="bot_details",
            legacy_path=legacy,
            model=BotThreadDetail,
        )

        first_state = first.load()
        second_state = second.load()
        first_state["thread-a"] = [self.detail("thread-a", "a")]
        second_state["thread-b"] = [self.detail("thread-b", "b")]
        first.save(first_state)
        second.save(second_state)

        loaded = NestedModelListMapRepository(
            self.store,
            namespace="bot_details",
            legacy_path=legacy,
            model=BotThreadDetail,
        ).load()
        self.assertEqual(set(loaded), {"thread-a", "thread-b"})
        self.assertEqual(set(json.loads(legacy.read_text())), {"thread-a", "thread-b"})

    def test_servicedesk_merges_tickets_by_ticket_id(self) -> None:
        legacy = self.root / "servicedesk.json"
        first = ServiceDeskStateRepository(self.store, legacy)
        second = ServiceDeskStateRepository(self.store, legacy)

        first_state = first.load()
        second_state = second.load()
        first_state["tickets"]["project#1"] = {"status": "open", "owner": "james"}
        first_state["last_sweep_at"] = 1.0
        second_state["tickets"]["project#2"] = {"status": "open", "owner": "dana"}
        second_state["last_sweep_at"] = 2.0
        first.save(first_state)
        second.save(second_state)

        loaded = ServiceDeskStateRepository(self.store, legacy).load()
        self.assertEqual(set(loaded["tickets"]), {"project#1", "project#2"})
        self.assertEqual(loaded["last_sweep_at"], 2.0)

    def test_semantic_event_repository_imports_and_normalizes_legacy_json(self) -> None:
        legacy = self.root / "semantic.json"
        legacy.write_text(json.dumps({"one": "1.5", "bad": "not-a-number"}))
        repository = JsonMapRepository(
            self.store,
            namespace="gitlab_semantic_events",
            legacy_path=legacy,
            normalize=_normalize_semantic_events,
        )

        self.assertEqual(repository.load(), {"one": 1.5})
        self.assertEqual(self.store.get("gitlab_semantic_events"), {"one": 1.5})

    def test_servicedesk_imports_legacy_state_once_then_uses_sqlite(self) -> None:
        legacy = self.root / "servicedesk.json"
        legacy.write_text(json.dumps({"tickets": {"project#7": {"status": "open"}}, "last_sweep_at": 4.0}))
        repository = ServiceDeskStateRepository(self.store, legacy)
        self.assertIn("project#7", repository.load()["tickets"])

        legacy.write_text(json.dumps({"tickets": {"different": {}}, "last_sweep_at": 9.0}))
        loaded_again = ServiceDeskStateRepository(self.store, legacy).load()
        self.assertIn("project#7", loaded_again["tickets"])
        self.assertNotIn("different", loaded_again["tickets"])


if __name__ == "__main__":
    unittest.main()
