from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_item_source_identity_index import (
    WorkItemSourceIdentityConflict,
    WorkItemSourceIdentityIndex,
)


def _identity(
    external_id: str,
    *,
    source_instance: str = "https://gitlab.example/api/v4/",
) -> TaskSourceIdentity:
    return TaskSourceIdentity(
        source_type="GitLab",
        source_instance=source_instance,
        external_id=external_id,
    )


def _state(ref: str, identity: TaskSourceIdentity) -> WorkItemState:
    return WorkItemState(
        ref=ref,
        project_id="home",
        source_identity=identity,
        current_stage="implementation_active",
        last_meaningful_update_at=1.0,
        updated_at=1.0,
        created_at=1.0,
    )


class WorkItemSourceIdentityIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.index = WorkItemSourceIdentityIndex(self.store)

    def test_lookup_preserves_same_task_source_identity_normalization(self):
        state = _state(
            "canonical-1",
            _identity("group/project#42"),
        )
        self.index.upsert(state)

        lookup = _identity(
            "group/project#42",
            source_instance="https://gitlab.example/api/v4",
        )
        self.assertEqual(
            self.index.ref_for_identity(lookup),
            "canonical-1",
        )

    def test_rekey_removes_old_identity_alias(self):
        original = _state(
            "canonical-1",
            _identity("group/project#42"),
        )
        self.index.upsert(original)

        changed = original.model_copy(
            update={
                "source_identity": _identity("group/project#43"),
            }
        )
        self.index.upsert(changed)

        self.assertIsNone(
            self.index.ref_for_identity(_identity("group/project#42"))
        )
        self.assertEqual(
            self.index.ref_for_identity(_identity("group/project#43")),
            "canonical-1",
        )
        self.assertEqual(self.index.metrics()["rekeys"], 1)

    def test_duplicate_identity_fails_deterministically(self):
        identity = _identity("group/project#42")
        self.index.upsert(_state("canonical-1", identity))

        with self.assertRaises(WorkItemSourceIdentityConflict):
            self.index.upsert(_state("canonical-2", identity))

        self.assertEqual(
            self.index.ref_for_identity(identity),
            "canonical-1",
        )

    def test_rebuild_populates_existing_states_and_rejects_duplicates(self):
        first = _state(
            "canonical-1",
            _identity("group/project#42"),
        )
        second = _state(
            "canonical-2",
            _identity("group/project#43"),
        )
        self.index.rebuild(
            {
                first.ref: first,
                second.ref: second,
            }
        )
        self.assertEqual(
            self.index.ref_for_identity(first.source_identity),
            first.ref,
        )
        self.assertEqual(
            self.index.ref_for_identity(second.source_identity),
            second.ref,
        )

        duplicate = _state(
            "canonical-3",
            first.source_identity,
        )
        with self.assertRaises(WorkItemSourceIdentityConflict):
            self.index.rebuild(
                {
                    first.ref: first,
                    duplicate.ref: duplicate,
                }
            )

    def test_metrics_expose_index_operations(self):
        state = _state(
            "canonical-1",
            _identity("group/project#42"),
        )
        self.index.upsert(state)
        self.index.ref_for_identity(state.source_identity)
        self.index.ref_for_identity(_identity("group/project#404"))

        metrics = self.index.metrics()
        self.assertEqual(metrics["upserts"], 1)
        self.assertEqual(metrics["lookups"], 2)
        self.assertEqual(metrics["hits"], 1)
        self.assertEqual(metrics["misses"], 1)


if __name__ == "__main__":
    unittest.main()
