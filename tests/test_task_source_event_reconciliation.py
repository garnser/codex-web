from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.gitlab_task_source_events import GitLabWebhookTaskSource
from codex_web.services.task_source_events import TaskSourceWorkItemEventReconciler
from codex_web.services.task_source_reconciliation import TaskSourceReconciliationOutcome
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.work_item_state import WorkItemStateMachine


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


def _payload(*, updated_at: str, owner: str = "james", state: str = "opened") -> dict:
    return {
        "object_kind": "issue",
        "project": {"path_with_namespace": "group/project"},
        "object_attributes": {
            "iid": 42,
            "title": "Issue",
            "state": state,
            "action": "update",
            "updated_at": updated_at,
            "url": "https://gitlab.example/group/project/-/issues/42",
        },
        "labels": [
            {"title": f"owner::{owner}"},
            {"title": "status::in progress"},
        ],
    }


class TaskSourceEventReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.host = _Host(Path(self.tempdir.name))
        self.machine = WorkItemStateMachine(self.host)
        self.projector = TaskSourceWorkItemProjector(self.host, self.machine)
        self.reconciler = TaskSourceWorkItemEventReconciler(self.host, self.projector)
        self.source = GitLabWebhookTaskSource("https://gitlab.example/api/v4")

    def test_issue_event_applies_through_normalized_projection_and_persists_cursor(self) -> None:
        event = self.source.normalize_event_sync(
            _payload(updated_at="2026-09-17T20:00:00Z")
        )
        self.assertIsNotNone(event)

        result = self.reconciler.reconcile(
            self.source,
            event,
            project_id="home",
            event_cursor="evt-42",
        )

        self.assertEqual(result.decision.outcome, TaskSourceReconciliationOutcome.APPLY)
        self.assertIsNotNone(result.state)
        self.assertEqual(result.state.current_owner, "james")
        self.assertEqual(result.state.source_identity.event_cursor, "evt-42")
        self.assertEqual(result.state.ref, "group/project#42")

    def test_stale_event_is_ignored_without_overwriting_state(self) -> None:
        newer = self.source.normalize_event_sync(
            _payload(updated_at="2026-09-17T20:00:00Z", owner="james")
        )
        self.reconciler.reconcile(self.source, newer, project_id="home")

        older = self.source.normalize_event_sync(
            _payload(updated_at="2026-09-17T19:00:00Z", owner="quinn")
        )
        result = self.reconciler.reconcile(self.source, older, project_id="home")

        self.assertEqual(result.decision.outcome, TaskSourceReconciliationOutcome.STALE)
        self.assertEqual(result.state.current_owner, "james")
        self.assertEqual(self.host.states["group/project#42"].current_owner, "james")

    def test_conflicting_authority_is_reported_without_mutation(self) -> None:
        self.host.states["group/project#42"] = WorkItemState(
            ref="group/project#42",
            project_id="home",
            source_identity=TaskSourceIdentity(
                source_type="jira",
                source_instance="company",
                external_id="group/project#42",
            ),
            current_owner="existing",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        event = self.source.normalize_event_sync(
            _payload(updated_at="2026-09-17T20:00:00Z", owner="james")
        )

        result = self.reconciler.reconcile(self.source, event, project_id="home")

        self.assertEqual(result.decision.outcome, TaskSourceReconciliationOutcome.CONFLICT)
        self.assertEqual(result.state.current_owner, "existing")
        self.assertEqual(result.state.source_identity.source_type, "jira")

    def test_non_issue_webhook_is_not_normalized_as_task_event(self) -> None:
        self.assertIsNone(self.source.normalize_event_sync({"object_kind": "pipeline"}))


if __name__ == "__main__":
    unittest.main()
