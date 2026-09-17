from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.models import IndexedThread
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_index import ThreadIndexRepository, install_thread_index_repository


class ThreadIndexRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteStateStore(self.root / "state.db")
        self.legacy_path = self.root / "thread_index.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_imports_legacy_json_then_keeps_sqlite_authoritative(self) -> None:
        self.legacy_path.write_text(
            json.dumps(
                [
                    {
                        "id": "thread-1",
                        "name": "Original",
                        "cwd": "/tmp/project",
                        "path": None,
                        "updatedAt": 1.0,
                    }
                ]
            )
        )
        repository = ThreadIndexRepository(self.store, self.legacy_path)
        self.assertEqual([thread.name for thread in repository.load()], ["Original"])

        self.legacy_path.write_text("[]")
        self.assertEqual([thread.name for thread in repository.load()], ["Original"])

    def test_upsert_updates_by_thread_id_and_remove_is_scoped(self) -> None:
        repository = ThreadIndexRepository(self.store, self.legacy_path)
        repository.upsert(IndexedThread(id="thread-1", name="One", updatedAt=1.0))
        repository.upsert(IndexedThread(id="thread-2", name="Two", updatedAt=2.0))
        repository.upsert(IndexedThread(id="thread-1", name="One updated", updatedAt=3.0))

        threads = repository.load()
        self.assertEqual([thread.id for thread in threads], ["thread-1", "thread-2"])
        self.assertEqual(threads[0].name, "One updated")

        repository.remove("thread-1")
        self.assertEqual([thread.id for thread in repository.load()], ["thread-2"])

    def test_installer_restores_all_historical_thread_index_seams(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace()
        repository = install_thread_index_repository(
            app,
            host,
            store=self.store,
            legacy_path=self.legacy_path,
        )

        self.assertIs(app.state.thread_index_repository, repository)
        self.assertIs(host._load_thread_index.__self__, repository)
        self.assertIs(host._save_thread_index.__self__, repository)
        self.assertIs(host._upsert_indexed_thread.__self__, repository)
        self.assertIs(host._remove_indexed_thread.__self__, repository)

    def test_application_source_composes_thread_index_repository(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "codex_web" / "application.py").read_text()
        self.assertIn("install_thread_index_repository", source)
        self.assertIn("THREAD_INDEX_FILE", source)


if __name__ == "__main__":
    unittest.main()
