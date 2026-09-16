from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_web.storage.sqlite_state import SQLiteStateStore


class SQLiteHardeningTests(unittest.TestCase):
    def test_schema_version_and_integrity_status_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / "state.db")
            store.put("example", {"value": 1})

            status = store.status()

            self.assertTrue(status["ok"])
            self.assertEqual(status["integrity"].lower(), "ok")
            self.assertEqual(status["schemaVersion"], SQLiteStateStore.SCHEMA_VERSION)
            self.assertEqual(status["supportedSchemaVersion"], SQLiteStateStore.SCHEMA_VERSION)
            self.assertEqual(status["journalMode"].lower(), "wal")
            self.assertEqual(status["documents"], 1)
            self.assertIsNotNone(status["lastDocumentUpdateAt"])

    def test_newer_schema_is_rejected_instead_of_silently_opened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            SQLiteStateStore(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE state_metadata SET value = ? WHERE key = 'schema_version'",
                    (str(SQLiteStateStore.SCHEMA_VERSION + 1),),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                SQLiteStateStore(path)

    def test_backup_is_transactionally_readable_and_preserves_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStateStore(root / "state.db")
            store.put("one", {"value": 1})
            store.put("two", [1, 2, 3])

            backup_path = store.backup_to(root / "backups" / "state-backup.db")
            backup = SQLiteStateStore(backup_path)

            self.assertEqual(backup.get("one"), {"value": 1})
            self.assertEqual(backup.get("two"), [1, 2, 3])
            self.assertTrue(backup.status()["ok"])

    def test_wal_checkpoint_returns_structured_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / "state.db")
            for index in range(10):
                store.put(f"doc-{index}", {"index": index})

            result = store.checkpoint(truncate=True)

            self.assertEqual(set(result), {"busy", "logFrames", "checkpointedFrames"})
            self.assertGreaterEqual(result["busy"], 0)
            self.assertGreaterEqual(result["logFrames"], 0)
            self.assertGreaterEqual(result["checkpointedFrames"], 0)


if __name__ == "__main__":
    unittest.main()
