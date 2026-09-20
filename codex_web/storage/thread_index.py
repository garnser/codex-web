from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from codex_web.models import IndexedThread
from codex_web.storage.operational_state import ModelListRepository
from codex_web.storage.state_store import StateStore


class ThreadIndexRepository:
    """Bounded, time-ordered thread list projection.

    The legacy list document remains a rollback compatibility mirror while
    normal list reads use keyed rows ordered by an inverted updated timestamp.
    Project-scoped rows are duplicated into a project index so unrelated
    Projects do not need to be scanned to return one page.
    """

    GLOBAL_NAMESPACE = "thread_index_rows"
    PROJECT_NAMESPACE = "thread_index_project_rows"
    LOOKUP_NAMESPACE = "thread_index_lookup"
    MAX_TIMESTAMP_MICROS = 9_999_999_999_999_999
    DEFAULT_SCAN_BUDGET = 5000

    def __init__(self, store: StateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self._legacy_repository = ModelListRepository(
            store,
            namespace="thread_index",
            legacy_path=legacy_path,
            model=IndexedThread,
            private=False,
        )

    @classmethod
    def _scope(cls, cwd: str | None) -> str:
        value = str(cwd or "__unknown__")
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    @classmethod
    def _inverse_timestamp(cls, value: float | None) -> int:
        micros = max(0, int(float(value or 0.0) * 1_000_000))
        return cls.MAX_TIMESTAMP_MICROS - min(
            micros,
            cls.MAX_TIMESTAMP_MICROS,
        )

    @classmethod
    def _global_key(cls, thread: IndexedThread) -> str:
        return (
            f"a/{int(bool(thread.archived))}/"
            f"{cls._inverse_timestamp(thread.updatedAt):016d}/"
            f"{thread.id}"
        )

    @classmethod
    def _project_key(cls, thread: IndexedThread) -> str:
        return (
            f"p/{cls._scope(thread.cwd)}/"
            f"a/{int(bool(thread.archived))}/"
            f"{cls._inverse_timestamp(thread.updatedAt):016d}/"
            f"{thread.id}"
        )

    def _ensure_migrated(self) -> None:
        if self.store.record_collection_exists(self.GLOBAL_NAMESPACE):
            return
        legacy = self._legacy_repository.load()
        global_rows: dict[str, Any] = {}
        project_rows: dict[str, Any] = {}
        lookups: dict[str, Any] = {}
        for thread in legacy:
            global_key = self._global_key(thread)
            project_key = self._project_key(thread)
            payload = thread.model_dump(mode="json")
            global_rows[global_key] = payload
            project_rows[project_key] = payload
            lookups[thread.id] = {
                "globalKey": global_key,
                "projectKey": project_key,
            }
        self.store.record_replace(self.GLOBAL_NAMESPACE, global_rows)
        self.store.record_replace(self.PROJECT_NAMESPACE, project_rows)
        self.store.record_replace(self.LOOKUP_NAMESPACE, lookups)

    def _compatibility_upsert(self, thread: IndexedThread) -> None:
        threads = self._legacy_repository.load()
        for index, existing in enumerate(threads):
            if existing.id == thread.id:
                threads[index] = thread
                self._legacy_repository.save(threads)
                return
        threads.append(thread)
        self._legacy_repository.save(threads)

    def _compatibility_remove(self, thread_id: str) -> None:
        threads = self._legacy_repository.load()
        kept = [thread for thread in threads if thread.id != thread_id]
        if len(kept) != len(threads):
            self._legacy_repository.save(kept)

    def get(self, thread_id: str) -> IndexedThread | None:
        self._ensure_migrated()
        lookup = self.store.record_get(self.LOOKUP_NAMESPACE, thread_id)
        if not isinstance(lookup, dict):
            return None
        global_key = str(lookup.get("globalKey") or "")
        if not global_key:
            return None
        payload = self.store.record_get(
            self.GLOBAL_NAMESPACE,
            global_key,
        )
        return (
            IndexedThread.model_validate(payload)
            if isinstance(payload, dict)
            else None
        )

    def load(self) -> list[IndexedThread]:
        """Compatibility full load; normal list APIs must use page()."""

        self._ensure_migrated()
        after: str | None = None
        result: list[IndexedThread] = []
        while True:
            rows, next_cursor = self.store.record_page(
                self.GLOBAL_NAMESPACE,
                after=after,
                limit=1000,
            )
            result.extend(
                IndexedThread.model_validate(payload)
                for payload in rows.values()
                if isinstance(payload, dict)
            )
            if not next_cursor:
                return result
            after = next_cursor

    def save(self, threads: list[IndexedThread]) -> None:
        global_rows: dict[str, Any] = {}
        project_rows: dict[str, Any] = {}
        lookups: dict[str, Any] = {}
        for thread in threads:
            global_key = self._global_key(thread)
            project_key = self._project_key(thread)
            payload = thread.model_dump(mode="json")
            global_rows[global_key] = payload
            project_rows[project_key] = payload
            lookups[thread.id] = {
                "globalKey": global_key,
                "projectKey": project_key,
            }
        self.store.record_replace(self.GLOBAL_NAMESPACE, global_rows)
        self.store.record_replace(self.PROJECT_NAMESPACE, project_rows)
        self.store.record_replace(self.LOOKUP_NAMESPACE, lookups)
        self._legacy_repository.save(threads)

    def upsert_many(self, threads: list[IndexedThread]) -> None:
        self._ensure_migrated()
        if not threads:
            return

        global_upserts: dict[str, Any] = {}
        project_upserts: dict[str, Any] = {}
        lookup_upserts: dict[str, Any] = {}
        global_deletes: list[str] = []
        project_deletes: list[str] = []

        for thread in threads:
            previous = self.store.record_get(
                self.LOOKUP_NAMESPACE,
                thread.id,
            )
            old_global = (
                str(previous.get("globalKey") or "")
                if isinstance(previous, dict)
                else ""
            )
            old_project = (
                str(previous.get("projectKey") or "")
                if isinstance(previous, dict)
                else ""
            )
            global_key = self._global_key(thread)
            project_key = self._project_key(thread)
            payload = thread.model_dump(mode="json")
            global_upserts[global_key] = payload
            project_upserts[project_key] = payload
            lookup_upserts[thread.id] = {
                "globalKey": global_key,
                "projectKey": project_key,
            }
            if old_global and old_global != global_key:
                global_deletes.append(old_global)
            if old_project and old_project != project_key:
                project_deletes.append(old_project)

        self.store.record_apply(
            self.GLOBAL_NAMESPACE,
            upserts=global_upserts,
            deletes=tuple(global_deletes),
        )
        self.store.record_apply(
            self.PROJECT_NAMESPACE,
            upserts=project_upserts,
            deletes=tuple(project_deletes),
        )
        self.store.record_apply(
            self.LOOKUP_NAMESPACE,
            upserts=lookup_upserts,
        )

        compatibility = {
            thread.id: thread
            for thread in self._legacy_repository.load()
        }
        for thread in threads:
            compatibility[thread.id] = thread
        self._legacy_repository.save(list(compatibility.values()))

    def upsert(self, thread: IndexedThread) -> None:
        self.upsert_many([thread])

    def remove(self, thread_id: str) -> None:
        self._ensure_migrated()
        previous = self.store.record_get(
            self.LOOKUP_NAMESPACE,
            thread_id,
        )
        if not isinstance(previous, dict):
            return
        global_key = str(previous.get("globalKey") or "")
        project_key = str(previous.get("projectKey") or "")
        if global_key:
            self.store.record_apply(
                self.GLOBAL_NAMESPACE,
                upserts={},
                deletes=(global_key,),
            )
        if project_key:
            self.store.record_apply(
                self.PROJECT_NAMESPACE,
                upserts={},
                deletes=(project_key,),
            )
        self.store.record_apply(
            self.LOOKUP_NAMESPACE,
            upserts={},
            deletes=(thread_id,),
        )
        self._compatibility_remove(thread_id)

    def page(
        self,
        *,
        project_path: str | None,
        archived: bool,
        search: str | None,
        after: str | None,
        limit: int,
        scan_budget: int | None = None,
    ) -> tuple[list[IndexedThread], str | None, bool]:
        """Return one stable page without materializing the complete index."""

        self._ensure_migrated()
        page_size = max(1, min(int(limit), 100))
        budget = max(
            page_size,
            min(
                int(scan_budget or self.DEFAULT_SCAN_BUDGET),
                20_000,
            ),
        )
        if project_path:
            namespace = self.PROJECT_NAMESPACE
            prefix = (
                f"p/{self._scope(project_path)}/"
                f"a/{int(bool(archived))}/"
            )
        else:
            namespace = self.GLOBAL_NAMESPACE
            prefix = f"a/{int(bool(archived))}/"

        normalized_search = str(search or "").strip().casefold()
        cursor = after
        scanned = 0
        selected: list[IndexedThread] = []
        batch_size = min(1000, max(100, page_size * 4))

        while scanned < budget:
            rows, backend_next = self.store.record_page(
                namespace,
                key_prefix=prefix,
                after=cursor,
                limit=min(batch_size, budget - scanned),
            )
            if not rows:
                return selected, None, False

            for key, payload in rows.items():
                cursor = key
                scanned += 1
                if not isinstance(payload, dict):
                    continue
                thread = IndexedThread.model_validate(payload)
                if normalized_search and normalized_search not in (
                    f"{thread.name} {thread.preview or ''} {thread.id}"
                ).casefold():
                    if scanned >= budget:
                        break
                    continue
                selected.append(thread)
                if len(selected) >= page_size:
                    return selected, cursor, False
                if scanned >= budget:
                    break

            if scanned >= budget:
                return selected, cursor, bool(backend_next)
            if not backend_next:
                return selected, None, False
            cursor = backend_next

        return selected, cursor, True


def install_thread_index_repository(
    app: Any,
    host: Any,
    *,
    store: StateStore,
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
