from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.services.work_items import WorkItemService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_item_list_index import WorkItemListIndex


class WorkItemAsyncReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.scope = TenantScope()
        self.states = {
            f"group/app#{i}": WorkItemState(
                ref=f"group/app#{i}", project_id="project-a", title=f"Item {i}",
                current_owner="james", current_stage="implementation_active",
                updated_at=float(i + 1), created_at=1.0,
                last_meaningful_update_at=float(i + 1),
            ) for i in range(4)
        }
        service = object.__new__(WorkItemService)
        service.work_items = SimpleNamespace(
            get_state=self.states.get, load_states=lambda: self.states,
        )
        service.state_machine = SimpleNamespace(
            _coerce_owner=lambda value: value,
            _normalize_work_item_stage=lambda value, fallback: value,
            _work_item_split_brain_findings=lambda state: [],
            _work_item_state=self.states.__getitem__,
            _work_item_state_public=lambda state: {"ref": state.ref},
        )
        service._project_for_scope = lambda project, scope: None
        service.work_item_list_index = WorkItemListIndex(
            SQLiteStateStore(Path(temp.name) / "state.sqlite3")
        )
        service.work_item_list_index.rebuild(self.states)
        self.service = service

    async def _assert_blocked_read_yields(self, operation, install):
        context = ContextVar("work_item_read_authority")
        marker = object()
        entered, release = threading.Event(), threading.Event()
        observations, timeouts = [], []
        loop_thread = threading.get_ident()

        def barrier():
            observations.append((threading.get_ident(), context.get()))
            entered.set()
            if not release.wait(2):
                timeouts.append(True)

        install(barrier)
        token = context.set(marker)
        task = asyncio.create_task(operation())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(timeouts, [])
        finally:
            release.set()
            result = await task
            context.reset(token)
        self.assertTrue(observations)
        for thread, value in observations:
            self.assertNotEqual(thread, loop_thread)
            self.assertIs(value, marker)
        return result

    async def _list(self, **kwargs):
        return await self.service.list(
            project_id="project-a", owner="james", stage="implementation_active",
            release_gate=None, scope=self.scope, limit=1, **kwargs,
        )

    async def test_indexed_page_storage_yields_and_preserves_authority_context(self):
        def install(barrier):
            def get(ref):
                barrier()
                return self.states.get(ref)
            self.service.work_items.get_state = get
        result = await self._assert_blocked_read_yields(self._list, install)
        self.assertEqual([row["ref"] for row in result["items"]], ["group/app#3"])
        self.assertTrue(result["nextCursor"])

    async def test_batch_page_storage_yields_and_preserves_authority_context(self):
        def install(barrier):
            def get_many(keys):
                barrier()
                return {key:self.states[key] for key in keys if key in self.states}
            self.service.work_items.get_states = get_many
            self.service.work_items.get_state = lambda _: self.fail("point read")
        result = await self._assert_blocked_read_yields(self._list, install)
        self.assertEqual([row["ref"] for row in result["items"]], ["group/app#3"])
        self.assertTrue(result["nextCursor"])

    async def test_compatibility_read_remains_bounded_off_loop(self):
        self.service.work_item_list_index = None
        def install(barrier):
            def load():
                barrier()
                return self.states
            self.service.work_items.load_states = load
        result = await self._assert_blocked_read_yields(self._list, install)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["compatibilityFallback"])

    async def test_exact_item_read_yields_and_preserves_context(self):
        def install(barrier):
            def get(ref):
                barrier()
                return self.states[ref]
            self.service.state_machine._work_item_state = get
        result = await self._assert_blocked_read_yields(
            lambda: self.service.get("group/app#1"), install,
        )
        self.assertEqual(result, {"ref": "group/app#1"})

    async def test_cursor_filter_revision_and_tenant_checks_are_unchanged(self):
        first = await self._list()
        second = await self._list(cursor=first["nextCursor"])
        self.assertEqual(second["items"][0]["ref"], "group/app#2")
        with self.assertRaises(HTTPException) as mismatch:
            await self._list(cursor=first["nextCursor"], q="different")
        self.assertEqual(mismatch.exception.status_code, 400)
        changed = self.states["group/app#1"].model_copy(update={"updated_at": 50.0})
        self.service.work_item_list_index.upsert(changed)
        with self.assertRaises(HTTPException) as stale:
            await self._list(cursor=first["nextCursor"])
        self.assertEqual(stale.exception.status_code, 409)
        def deny(project, scope):
            raise HTTPException(status_code=404, detail="Project not found")
        self.service._project_for_scope = deny
        self.service.work_items.get_state = lambda ref: self.fail("denied scope read state")
        with self.assertRaises(HTTPException) as denied:
            await self._list()
        self.assertEqual(denied.exception.status_code, 404)
