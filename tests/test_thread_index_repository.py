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

    def test_replacement_survives_restart_with_original_provenance(self):
        repository = ThreadIndexRepository(self.store, self.legacy_path)
        provenance = {"actor_identity_id": "owner", "organization_id": "tenant",
                      "workspace_id": "workspace", "agent_profile_id": "carl",
                      "agent_profile_revision": 6, "repository_resource_id": "platform"}
        repository.remember_replacement("old", "middle", project_id="project", provenance=provenance)
        repository.remember_replacement("middle", "new", project_id="project", provenance=provenance)
        reopened = ThreadIndexRepository(SQLiteStateStore(self.root / "state.db"), self.legacy_path)
        self.assertEqual(reopened.replacement_thread_id("old"), "new")
        self.assertIsNone(reopened.replacement_thread_id("new"))
        edge = reopened.store.get(repository.REPLACEMENTS_NAMESPACE)["edges"]["old"]
        self.assertEqual(edge["new_thread_id"], "middle")
        self.assertEqual(edge["provenance"], provenance)

    def test_conflict_cycle_and_tenant_crossing_preserve_original_edge(self):
        repository = ThreadIndexRepository(self.store, self.legacy_path)
        provenance = {"organization_id": "tenant", "workspace_id": "workspace"}
        repository.remember_replacement("old", "new", project_id="project", provenance=provenance)
        original = self.store.get(repository.REPLACEMENTS_NAMESPACE)
        # Idempotency does not replace the first observation.
        repository.remember_replacement("old", "new", project_id="project", provenance=provenance)
        for old, new, project, scope in [
            ("old", "other", "project", provenance),
            ("new", "old", "project", provenance),
            ("new", "other", "other-project", provenance),
            ("new", "other", "project", {"organization_id": "other-tenant"}),
            ("earlier", "old", "other-project", provenance),
        ]:
            with self.assertRaises(ValueError):
                repository.remember_replacement(old, new, project_id=project, provenance=scope)
            self.assertEqual(self.store.get(repository.REPLACEMENTS_NAMESPACE), original)

    def test_concurrent_opposite_edges_cannot_create_cycle(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)

        def record(old, new):
            repository = ThreadIndexRepository(SQLiteStateStore(self.root / "state.db"), self.legacy_path)
            barrier.wait()
            try:
                repository.remember_replacement(old, new, project_id="project", provenance={})
                return "recorded"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda pair: record(*pair), [("a", "b"), ("b", "a")]))
        self.assertEqual(sorted(results), ["recorded", "rejected"])
        repository = ThreadIndexRepository(self.store, self.legacy_path)
        self.assertNotEqual(repository.replacement_thread_id("a"), "a")
        self.assertNotEqual(repository.replacement_thread_id("b"), "b")

    def test_historical_chain_import_is_atomic_when_later_edge_conflicts(self):
        repository=ThreadIndexRepository(self.store,self.legacy_path)
        repository.remember_replacement('middle','actual',project_id='project',provenance={})
        before=self.store.get(repository.REPLACEMENTS_NAMESPACE)
        with self.assertRaises(ValueError):
            repository.remember_recorded_replacements([
                {'old_thread_id':'old','new_thread_id':'middle','project_id':'project','provenance':{}},
                {'old_thread_id':'middle','new_thread_id':'conflicting','project_id':'project','provenance':{}},
            ])
        self.assertEqual(self.store.get(repository.REPLACEMENTS_NAMESPACE),before)
        self.assertIsNone(repository.replacement_thread_id('old'))

    def test_historical_same_edge_with_conflicting_tenant_preserves_bootstrap_provenance(self):
        repository=ThreadIndexRepository(self.store,self.legacy_path)
        repository.remember_replacement('old','new',project_id='project',provenance={'organization_id':'original'})
        before=self.store.get(repository.REPLACEMENTS_NAMESPACE)
        with self.assertRaises(ValueError):repository.remember_recorded_replacements([
            {'old_thread_id':'old','new_thread_id':'new','project_id':'project',
             'provenance':{'organization_id':'other'}}])
        self.assertEqual(self.store.get(repository.REPLACEMENTS_NAMESPACE),before)

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

    def test_project_index_uses_project_id_not_execution_workspace_path(self) -> None:
        repository = ThreadIndexRepository(self.store, self.legacy_path)
        repository.upsert(
            IndexedThread(
                id="thread-isolated",
                name="VP Engineering",
                cwd="/managed/execution-workspaces/worktree-a",
                project_id="codex-web",
                updatedAt=3.0,
            )
        )

        threads, cursor, truncated = repository.page(
            project_id="codex-web",
            archived=False,
            search=None,
            after=None,
            limit=10,
        )

        self.assertEqual([thread.id for thread in threads], ["thread-isolated"])
        self.assertIsNone(cursor)
        self.assertFalse(truncated)

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
