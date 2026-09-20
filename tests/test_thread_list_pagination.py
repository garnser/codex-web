from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.models import IndexedThread
from codex_web.services.threads import ThreadService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_index import ThreadIndexRepository


class _Runtime:
    def __init__(self, rows=None) -> None:
        self.rows = list(rows or [])
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict):
        self.calls.append((method, dict(params)))
        if method == "thread/list":
            return {"data": [dict(row) for row in self.rows]}
        raise AssertionError(f"unexpected runtime request: {method}")


class _Projects:
    def __init__(self) -> None:
        self.rows = {
            "home": SimpleNamespace(
                id="home",
                path="/repo/home",
                model=None,
            ),
            "other": SimpleNamespace(
                id="other",
                path="/repo/other",
                model=None,
            ),
        }

    def get(self, project_id: str):
        return self.rows[project_id]


def _thread(
    index: int,
    *,
    project_id: str = "home",
    archived: bool = False,
    name: str | None = None,
) -> IndexedThread:
    path = f"/repo/{project_id}"
    return IndexedThread(
        id=f"thread-{index:04d}",
        name=name or f"Thread {index:04d}",
        cwd=path,
        path=f"{path}/.codex/{index}",
        updatedAt=float(index),
        project_id=project_id,
        archived=archived,
        preview=f"preview {index}",
    )


class ThreadListPaginationTests(unittest.IsolatedAsyncioTestCase):
    def _repository(self, root: Path) -> ThreadIndexRepository:
        return ThreadIndexRepository(
            SQLiteStateStore(root / "state.sqlite3"),
            root / "thread_index.json",
        )

    def _service(
        self,
        runtime: _Runtime,
        repository: ThreadIndexRepository,
        *,
        active=None,
    ) -> ThreadService:
        active = dict(active or {})
        return ThreadService(
            runtime_transport=runtime,
            runtime_request_for_thread=runtime.request,
            event_sink=lambda _event: None,
            project_runtime=_Projects(),
            thread_index=repository,
            active_turn_getter=active.get,
        )

    async def test_first_page_is_bounded_and_never_reads_each_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            repository.save([_thread(index) for index in range(1000)])
            runtime = _Runtime()
            service = self._service(runtime, repository)

            response = await service.list(
                "home",
                limit=25,
            )

            self.assertEqual(len(response["data"]), 25)
            self.assertTrue(response["nextCursor"])
            self.assertEqual(response["pageSize"], 25)
            self.assertEqual(
                [method for method, _params in runtime.calls],
                ["thread/list"],
            )
            self.assertFalse(
                any("turns" in row for row in response["data"])
            )
            self.assertEqual(
                response["data"][0]["id"],
                "thread-0999",
            )

    async def test_second_page_uses_index_only_and_has_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            repository.save([_thread(index) for index in range(120)])
            runtime = _Runtime()
            service = self._service(runtime, repository)

            first = await service.list("home", limit=50)
            second = await service.list(
                "home",
                limit=50,
                cursor=first["nextCursor"],
            )

            first_ids = {row["id"] for row in first["data"]}
            second_ids = {row["id"] for row in second["data"]}
            self.assertEqual(len(first_ids), 50)
            self.assertEqual(len(second_ids), 50)
            self.assertFalse(first_ids & second_ids)
            self.assertEqual(
                [method for method, _params in runtime.calls],
                ["thread/list"],
            )

    async def test_stale_cursor_fails_closed_after_index_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            repository.save([_thread(index) for index in range(10)])
            service = self._service(_Runtime(), repository)

            first = await service.list("home", limit=5)
            repository.upsert(_thread(99))

            with self.assertRaises(HTTPException) as raised:
                await service.list(
                    "home",
                    limit=5,
                    cursor=first["nextCursor"],
                )
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(
                raised.exception.detail["code"],
                "thread_list_cursor_stale",
            )

    async def test_project_and_archive_scope_are_applied_before_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            repository.save(
                [
                    *[_thread(index) for index in range(30)],
                    *[
                        _thread(index + 100, project_id="other")
                        for index in range(30)
                    ],
                    *[
                        _thread(index + 200, archived=True)
                        for index in range(8)
                    ],
                ]
            )
            service = self._service(_Runtime(), repository)

            active = await service.list("home", limit=100)
            archived = await service.list(
                "home",
                archived=True,
                limit=100,
            )

            self.assertEqual(len(active["data"]), 30)
            self.assertTrue(
                all(row["projectId"] == "home" for row in active["data"])
            )
            self.assertEqual(len(archived["data"]), 8)
            self.assertTrue(
                all(row["archived"] for row in archived["data"])
            )

    async def test_search_is_bounded_and_cursor_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            rows = [
                _thread(
                    index,
                    name=(
                        f"Match {index}"
                        if index % 10 == 0
                        else f"Other {index}"
                    ),
                )
                for index in range(300)
            ]
            repository.save(rows)
            service = self._service(_Runtime(), repository)

            first = await service.list(
                "home",
                search="match",
                limit=10,
            )

            self.assertEqual(len(first["data"]), 10)
            self.assertTrue(
                all(
                    "match" in row["name"].casefold()
                    for row in first["data"]
                )
            )
            with self.assertRaises(HTTPException) as raised:
                await service.list(
                    "home",
                    search="different",
                    limit=10,
                    cursor=first["nextCursor"],
                )
            self.assertEqual(raised.exception.status_code, 400)

    async def test_runtime_history_is_not_copied_into_list_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            runtime = _Runtime(
                [
                    {
                        "id": "runtime-1",
                        "name": "Runtime row",
                        "cwd": "/repo/home",
                        "updatedAt": 42.0,
                        "status": {"type": "idle"},
                        "turns": [
                            {
                                "items": [
                                    {"type": "agentMessage", "text": "large"}
                                ]
                            }
                        ],
                    }
                ]
            )
            service = self._service(runtime, repository)

            response = await service.list("home", limit=10)

            self.assertEqual(len(response["data"]), 1)
            row = response["data"][0]
            self.assertEqual(row["id"], "runtime-1")
            self.assertNotIn("turns", row)
            self.assertTrue(row["runtimeLoaded"])
            self.assertFalse(row["needsRefresh"])

    async def test_page_size_is_capped_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            repository.save([_thread(index) for index in range(200)])
            service = self._service(_Runtime(), repository)

            response = await service.list("home", limit=5000)

            self.assertEqual(response["pageSize"], 100)
            self.assertEqual(len(response["data"]), 100)


class ThreadIndexMigrationTests(unittest.TestCase):
    def test_partial_keyed_migration_repairs_missing_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStateStore(root / "state.sqlite3")
            legacy = root / "thread_index.json"
            store.put(
                "thread_index",
                [_thread(1).model_dump(mode="json")],
            )
            repository = ThreadIndexRepository(store, legacy)

            # Simulate an interrupted migration after only the global
            # projection was written.
            thread = _thread(1)
            store.record_replace(
                repository.GLOBAL_NAMESPACE,
                {
                    repository._global_key(thread): (
                        thread.model_dump(mode="json")
                    )
                },
            )

            page, cursor, truncated = repository.page(
                project_path="/repo/home",
                archived=False,
                search=None,
                after=None,
                limit=10,
            )

            self.assertEqual([item.id for item in page], ["thread-0001"])
            self.assertIsNone(cursor)
            self.assertFalse(truncated)
            self.assertTrue(
                store.record_collection_exists(
                    repository.PROJECT_NAMESPACE
                )
            )
            self.assertTrue(
                store.record_collection_exists(
                    repository.LOOKUP_NAMESPACE
                )
            )


if __name__ == "__main__":
    unittest.main()
