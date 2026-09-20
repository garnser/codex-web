from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_web.models import ThreadRunSettings
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


class _TrackingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.closed = True
        self.connection.close()


class _TrackingStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        self.connections: list[_TrackingConnection] = []
        super().__init__(path)

    def _connect(self):
        tracked = _TrackingConnection(super()._connect())
        self.connections.append(tracked)
        return tracked


class _CountingKeyedStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        self.record_items_calls = 0
        super().__init__(path)

    def record_items(self, namespace: str):
        self.record_items_calls += 1
        return super().record_items(namespace)


class SQLiteStateStoreTests(unittest.TestCase):
    def test_imports_legacy_json_once_and_uses_sqlite_as_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "thread_settings.json"
            legacy.write_text(json.dumps({"thread-1": {"sandbox": "read-only"}}))
            store = SQLiteStateStore(root / "codex-web.db")
            repository = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )

            loaded = repository.load()
            self.assertEqual(loaded["thread-1"].sandbox, "read-only")
            self.assertTrue(store.contains("thread_settings"))

            legacy.write_text(json.dumps({"thread-1": {"sandbox": "workspace-write"}}))
            loaded_again = repository.load()
            self.assertEqual(loaded_again["thread-1"].sandbox, "read-only")

    def test_save_is_transactional_and_mirrors_legacy_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "thread_settings.json"
            database = root / "codex-web.db"
            store = SQLiteStateStore(database)
            repository = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )

            repository.save(
                {
                    "thread-2": ThreadRunSettings(
                        sandbox="workspace-write",
                        approval_policy="on-request",
                    )
                }
            )

            db_payload = store.get("thread_settings")
            file_payload = json.loads(legacy.read_text())
            self.assertEqual(db_payload, file_payload)
            self.assertEqual(db_payload["thread-2"]["sandbox"], "workspace-write")

            connection = sqlite3.connect(database)
            try:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(str(journal_mode).lower(), "wal")

    def test_concurrent_snapshots_merge_unrelated_map_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStateStore(root / "codex-web.db")
            legacy = root / "thread_settings.json"
            first = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )
            second = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )
            store.put("thread_settings", {"existing": {"sandbox": "read-only"}})

            first_values = first.load()
            second_values = second.load()
            first_values["first"] = ThreadRunSettings(model="gpt-first")
            second_values["second"] = ThreadRunSettings(model="gpt-second")

            first.save(first_values)
            second.save(second_values)

            final = store.get("thread_settings")
            self.assertEqual(set(final), {"existing", "first", "second"})
            self.assertEqual(final["first"]["model"], "gpt-first")
            self.assertEqual(final["second"]["model"], "gpt-second")
            self.assertEqual(json.loads(legacy.read_text()), final)

    def test_database_and_directory_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            database = root / "codex-web.db"
            store = SQLiteStateStore(database)
            store.put("example", {"value": 1})

            if os.name != "nt":
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(database.stat().st_mode & 0o777, 0o600)
                for suffix in ("-wal", "-shm"):
                    candidate = Path(f"{database}{suffix}")
                    if candidate.exists():
                        self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)

    def test_private_compatibility_mirror_uses_restricted_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "thread_settings.json"
            repository = ModelMapRepository(
                SQLiteStateStore(root / "codex-web.db"),
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )

            repository.save({"thread": ThreadRunSettings(model="gpt-test")})

            self.assertEqual(legacy.stat().st_mode & 0o777, 0o600)

    def test_keyed_repository_migrates_document_and_preserves_logical_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStateStore(root / "codex-web.db")
            legacy = root / "thread_settings.json"
            store.put(
                "thread_settings",
                {
                    "thread-1": {"sandbox": "read-only"},
                    "thread-2": {"model": "gpt-old"},
                },
            )
            repository = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )

            repository.put(
                "thread-2",
                ThreadRunSettings(
                    sandbox="workspace-write",
                    model="gpt-new",
                ),
            )

            self.assertTrue(store.record_collection_exists("thread_settings"))
            self.assertEqual(
                repository.get("thread-2").model,
                "gpt-new",
            )
            self.assertEqual(
                store.get("thread_settings")["thread-1"]["sandbox"],
                "read-only",
            )
            self.assertEqual(
                store.documents()["thread_settings"]["thread-2"]["model"],
                "gpt-new",
            )

            connection = sqlite3.connect(root / "codex-web.db")
            try:
                direct = connection.execute(
                    "SELECT 1 FROM state_documents WHERE namespace = ?",
                    ("thread_settings",),
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNone(direct)

    def test_keyed_hot_path_defers_full_legacy_json_rewrite_until_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "thread_settings.json"
            legacy.write_text(
                json.dumps(
                    {"thread-1": {"sandbox": "read-only"}},
                    sort_keys=True,
                )
            )
            repository = ModelMapRepository(
                SQLiteStateStore(root / "codex-web.db"),
                namespace="thread_settings",
                legacy_path=legacy,
                model=ThreadRunSettings,
            )

            repository.put(
                "thread-1",
                ThreadRunSettings(sandbox="workspace-write"),
            )

            self.assertEqual(
                json.loads(legacy.read_text())["thread-1"]["sandbox"],
                "read-only",
            )
            repository.flush_legacy_mirror()
            self.assertEqual(
                json.loads(legacy.read_text())["thread-1"]["sandbox"],
                "workspace-write",
            )

    def test_status_counts_keyed_collection_as_one_logical_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / "codex-web.db")
            store.record_replace(
                "large-map",
                {
                    "one": {"value": 1},
                    "two": {"value": 2},
                    "three": {"value": 3},
                },
            )

            self.assertEqual(store.status()["documents"], 1)

    def test_update_many_keeps_keyed_namespace_record_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / "codex-web.db")
            store.record_replace(
                "thread_settings",
                {"thread-1": {"sandbox": "read-only"}},
            )
            store.put("other", {"value": 1})

            updated = store.update_many(
                {
                    "thread_settings": {},
                    "other": {},
                },
                lambda current: {
                    "thread_settings": {
                        **current["thread_settings"],
                        "thread-2": {"model": "gpt-test"},
                    },
                    "other": {"value": current["other"]["value"] + 1},
                },
            )

            self.assertEqual(updated["other"]["value"], 2)
            self.assertTrue(store.record_collection_exists("thread_settings"))
            self.assertEqual(
                store.record_get("thread_settings", "thread-2")["model"],
                "gpt-test",
            )
            self.assertEqual(
                store.get("thread_settings")["thread-1"]["sandbox"],
                "read-only",
            )

    def test_keyed_write_cost_does_not_depend_on_registry_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _CountingKeyedStore(root / "codex-web.db")
            store.record_replace(
                "thread_settings",
                {
                    f"thread-{index}": {"model": f"model-{index}"}
                    for index in range(5000)
                },
            )
            repository = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=root / "thread_settings.json",
                model=ThreadRunSettings,
            )
            store.record_items_calls = 0

            repository.put(
                "thread-2500",
                ThreadRunSettings(model="updated"),
            )

            self.assertEqual(store.record_items_calls, 0)
            self.assertEqual(
                repository.get("thread-2500").model,
                "updated",
            )

    def test_keyed_mutation_and_mirror_metrics_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStateStore(root / "codex-web.db")
            repository = ModelMapRepository(
                store,
                namespace="thread_settings",
                legacy_path=root / "thread_settings.json",
                model=ThreadRunSettings,
            )

            repository.put(
                "thread-1",
                ThreadRunSettings(model="gpt-test"),
            )
            keyed = store.status()["keyedMutationMetrics"]
            mirror_before = repository.compatibility_metrics()["checkpoint"]
            self.assertGreaterEqual(keyed["count"], 1)
            self.assertEqual(mirror_before["count"], 0)

            repository.flush_legacy_mirror()
            mirror_after = repository.compatibility_metrics()["checkpoint"]
            self.assertEqual(mirror_after["count"], 1)
            self.assertGreaterEqual(mirror_after["lastSeconds"], 0.0)

    def test_every_store_connection_is_closed_after_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _TrackingStore(Path(tmp) / "codex-web.db")

            store.put("example", {"value": 1})
            self.assertEqual(store.get("example"), {"value": 1})
            self.assertTrue(store.contains("example"))
            store.update("example", lambda payload: {**payload, "value": 2}, default={})

            self.assertGreaterEqual(len(store.connections), 5)
            self.assertTrue(all(connection.closed for connection in store.connections))


if __name__ == "__main__":
    unittest.main()
