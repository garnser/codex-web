from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.services.work_item_state import WorkItemStateMachine


class _Client:
    pass


class _Host:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {"quinn", "release manager"}

    def __init__(self, root: Path) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states: dict[str, WorkItemState] = {}

    def _load_work_item_states(self):
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}

    def _leading_owner_cue_in_action(self, action):
        return None


class TaskSourceWorkItemProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.host = _Host(Path(self.tempdir.name))
        self.machine = WorkItemStateMachine(self.host)
        self.projector = TaskSourceWorkItemProjector(self.host, self.machine)
        self.source = GitLabTaskSource(
            "https://gitlab.example/api/v4",
            "token",
            client=_Client(),
        )

    def snapshot(self, *, revision: str = "2026-09-17T19:00:00Z") -> TaskSourceSnapshot:
        return TaskSourceSnapshot(
            identity=TaskSourceIdentity(
                source_type="gitlab",
                source_instance="https://gitlab.example/api/v4",
                external_id="group/project#42",
                external_url="https://gitlab.example/group/project/-/issues/42",
                revision=revision,
            ),
            title="External issue",
            source_state="opened",
            labels=("owner::carl", "status::in progress", "priority::P1"),
        )

    def test_new_snapshot_creates_canonical_state_without_provider_payload(self) -> None:
        state = self.projector.upsert(self.source, self.snapshot(), project_id="home")

        self.assertEqual(state.ref, "group/project#42")
        self.assertEqual(state.source_identity.external_id, "group/project#42")
        self.assertEqual(state.current_owner, "carl")
        self.assertEqual(state.current_stage, "implementation_active")
        self.assertEqual(state.priority, "priority::P1")
        self.assertTrue(state.release_gate)
        self.assertEqual(self.host.states[state.ref].source_identity, state.source_identity)

    def test_existing_canonical_ref_is_preserved_by_source_identity(self) -> None:
        identity = self.snapshot().identity
        existing = WorkItemState(
            ref="canonical-work-123",
            project_id="home",
            project_path="group/project",
            source_identity=identity,
            current_owner="carl",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            last_gitlab_event_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host.states[existing.ref] = existing

        state = self.projector.upsert(self.source, self.snapshot(), project_id="home")

        self.assertEqual(state.ref, "canonical-work-123")
        self.assertIn("canonical-work-123", self.host.states)
        self.assertNotIn("group/project#42", self.host.states)
        self.assertEqual(state.source_identity.external_id, "group/project#42")

    def test_older_snapshot_cannot_overwrite_newer_canonical_projection(self) -> None:
        newer = self.snapshot(revision="2026-09-17T20:00:00Z")
        state = self.projector.upsert(self.source, newer, project_id="home")
        newer_timestamp = state.last_gitlab_event_at

        older = TaskSourceSnapshot(
            identity=self.snapshot(revision="2026-09-17T19:00:00Z").identity,
            title="Stale title",
            source_state="opened",
            labels=("owner::someone-else", "status::blocked"),
        )
        result = self.projector.upsert(self.source, older, project_id="home")

        self.assertEqual(result.current_owner, "carl")
        self.assertEqual(result.current_stage, "implementation_active")
        self.assertEqual(result.last_gitlab_event_at, newer_timestamp)


if __name__ == "__main__":
    unittest.main()
