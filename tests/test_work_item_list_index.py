from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_item_list_index import WorkItemListIndex


def _state(
    index: int,
    *,
    project_id: str = "project-a",
    organization_id: str = "local",
    workspace_id: str = "default",
) -> WorkItemState:
    now = float(index + 1)
    return WorkItemState(
        ref=f"group/project#{index:05d}",
        organization_id=organization_id,
        workspace_id=workspace_id,
        project_id=project_id,
        title=f"Item {index}",
        current_owner="carl" if index % 2 == 0 else "dana",
        current_stage=(
            "implementation_active"
            if index % 3
            else "ready_for_validation"
        ),
        last_meaningful_update_at=now,
        updated_at=now,
        created_at=now,
    )


class WorkItemListIndexTests(unittest.TestCase):
    def _index(self, root: Path) -> WorkItemListIndex:
        return WorkItemListIndex(
            SQLiteStateStore(root / "state.sqlite3")
        )

    def test_large_project_pages_without_loading_unrelated_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(Path(tmp))
            states = {
                state.ref: state
                for state in [
                    *[_state(i) for i in range(2500)],
                    *[
                        _state(i + 3000, project_id="project-b")
                        for i in range(500)
                    ],
                ]
            }
            index.rebuild(states)
            scope = TenantScope()
            getter_calls: list[str] = []

            def get_state(ref: str):
                getter_calls.append(ref)
                return states.get(ref)

            page, cursor, truncated = index.page(
                scope=scope,
                project_id="project-a",
                after=None,
                limit=50,
                get_state=get_state,
                predicate=lambda _state: True,
            )

            self.assertEqual(len(page), 50)
            self.assertEqual(len(getter_calls), 50)
            self.assertTrue(cursor)
            self.assertFalse(truncated)
            self.assertTrue(
                all(item.project_id == "project-a" for item in page)
            )
            self.assertEqual(page[0].ref, "group/project#02499")

    def test_cursor_continues_without_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(Path(tmp))
            states = {
                state.ref: state
                for state in [_state(i) for i in range(130)]
            }
            index.rebuild(states)
            scope = TenantScope()

            first, cursor, _ = index.page(
                scope=scope,
                project_id="project-a",
                after=None,
                limit=50,
                get_state=states.get,
                predicate=lambda _state: True,
            )
            second, next_cursor, _ = index.page(
                scope=scope,
                project_id="project-a",
                after=cursor,
                limit=50,
                get_state=states.get,
                predicate=lambda _state: True,
            )

            self.assertEqual(len(first), 50)
            self.assertEqual(len(second), 50)
            self.assertFalse(
                {item.ref for item in first}
                & {item.ref for item in second}
            )
            self.assertTrue(next_cursor)

    def test_filter_scan_is_bounded_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(Path(tmp))
            states = {
                state.ref: state
                for state in [_state(i) for i in range(400)]
            }
            index.rebuild(states)
            scope = TenantScope()

            page, cursor, truncated = index.page(
                scope=scope,
                project_id="project-a",
                after=None,
                limit=10,
                scan_budget=25,
                get_state=states.get,
                predicate=lambda state: state.ref.endswith("00000"),
            )

            self.assertLessEqual(len(page), 10)
            self.assertTrue(cursor)
            self.assertTrue(truncated)

    def test_update_reorders_row_and_changes_project_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(Path(tmp))
            states = {
                state.ref: state
                for state in [_state(1), _state(2)]
            }
            index.rebuild(states)
            scope = TenantScope()
            before = index.revision(
                scope=scope,
                project_id="project-a",
            )

            updated = states["group/project#00001"].model_copy(
                update={"updated_at": 999.0}
            )
            states[updated.ref] = updated
            index.upsert(updated)
            after = index.revision(
                scope=scope,
                project_id="project-a",
            )
            page, _cursor, _ = index.page(
                scope=scope,
                project_id="project-a",
                after=None,
                limit=10,
                get_state=states.get,
                predicate=lambda _state: True,
            )

            self.assertNotEqual(before, after)
            self.assertEqual(page[0].ref, updated.ref)

    def test_cross_tenant_state_is_not_in_project_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(Path(tmp))
            local = _state(1)
            other = _state(
                2,
                organization_id="other",
                workspace_id="tenant",
            )
            states = {local.ref: local, other.ref: other}
            index.rebuild(states)

            page, _cursor, _ = index.page(
                scope=TenantScope(),
                project_id="project-a",
                after=None,
                limit=10,
                get_state=states.get,
                predicate=lambda _state: True,
            )

            self.assertEqual([item.ref for item in page], [local.ref])


if __name__ == "__main__":
    unittest.main()
