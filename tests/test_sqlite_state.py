from __future__ import annotations

import json
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

            # Once migrated, the database is authoritative rather than a stale
            # or manually modified compatibility file.
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

    def test_every_store_connection_is_closed_after_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _TrackingStore(Path(tmp) / "codex-web.db")

            store.put("example", {"value": 1})
            self.assertEqual(store.get("example"), {"value": 1})
            self.assertTrue(store.contains("example"))

            self.assertGreaterEqual(len(store.connections), 4)
            self.assertTrue(all(connection.closed for connection in store.connections))


if __name__ == "__main__":
    unittest.main()
