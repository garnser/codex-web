from __future__ import annotations

from pathlib import Path
from typing import Any

from codex_web.models import IndexedThread
from codex_web.storage.operational_state import ModelListRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


class ThreadIndexRepository:
    """SQLite-primary thread index with rollback-safe JSON mirroring."""

    def __init__(self, store: SQLiteStateStore, legacy_path: Path) -> None:
        self._repository = ModelListRepository(
            store,
            namespace="thread_index",
            legacy_path=legacy_path,
            model=IndexedThread,
            private=False,
        )

    def load(self) -> list[IndexedThread]:
        return self._repository.load()

    def save(self, threads: list[IndexedThread]) -> None:
        self._repository.save(threads)

    def upsert(self, thread: IndexedThread) -> None:
        threads = self.load()
        for index, existing in enumerate(threads):
            if existing.id == thread.id:
                threads[index] = thread
                self.save(threads)
                return
        threads.append(thread)
        self.save(threads)

    def remove(self, thread_id: str) -> None:
        threads = self.load()
        kept = [thread for thread in threads if thread.id != thread_id]
        if len(kept) != len(threads):
            self.save(kept)


def install_thread_index_repository(
    app: Any,
    host: Any,
    *,
    store: SQLiteStateStore,
    legacy_path: Path,
) -> ThreadIndexRepository:
    existing = getattr(app.state, "thread_index_repository", None)
    if isinstance(existing, ThreadIndexRepository):
        repository = existing
    else:
        repository = ThreadIndexRepository(store, legacy_path)
        app.state.thread_index_repository = repository

    host._load_thread_index = repository.load
    host._save_thread_index = repository.save
    host._upsert_indexed_thread = repository.upsert
    host._remove_indexed_thread = repository.remove
    return repository
