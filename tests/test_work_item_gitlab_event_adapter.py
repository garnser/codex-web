from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.services.task_source_events import TaskSourceEventReconciliationResult
from codex_web.services.task_source_reconciliation import (
    TaskSourceReconciliationDecision,
    TaskSourceReconciliationOutcome,
)
from codex_web.services.work_items import WorkItemService


class _StateMachine:
    pass


class _Projector:
    pass


class _Reconciler:
    def __init__(self) -> None:
        self.calls = []

    def reconcile(self, source, event, *, project_id, event_cursor=None):
        self.calls.append((source.source_type, event.event_type, project_id))
        return TaskSourceEventReconciliationResult(
            state=SimpleNamespace(ref=event.identity.external_id),
            decision=TaskSourceReconciliationDecision(
                outcome=TaskSourceReconciliationOutcome.APPLY,
                event_key="key",
                reason="test",
            ),
        )


class _Host:
    GITLAB_API_BASE = "https://gitlab.example/api/v4"

    def __init__(self) -> None:
        self.legacy_calls = 0
        self._upsert_work_item_state_from_gitlab_event = self.legacy

    def legacy(self, payload, *, project_id):
        self.legacy_calls += 1
        return SimpleNamespace(ref="legacy")


class GitLabEventAdapterRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = _Host()
        self.reconciler = _Reconciler()
        self.service = WorkItemService(
            self.host,
            state_machine=_StateMachine(),
            task_source_projector=_Projector(),
            task_source_event_reconciler=self.reconciler,
        )

    def test_issue_event_uses_provider_neutral_reconciler(self) -> None:
        state = self.service.project_gitlab_event_compat(
            {
                "object_kind": "issue",
                "project": {"path_with_namespace": "group/project"},
                "object_attributes": {
                    "iid": 7,
                    "state": "opened",
                    "action": "update",
                    "updated_at": "2026-09-17T20:00:00Z",
                },
                "labels": [{"title": "owner::james"}],
            },
            project_id="home",
        )

        self.assertEqual(state.ref, "group/project#7")
        self.assertEqual(self.reconciler.calls, [("gitlab", "issue.update", "home")])
        self.assertEqual(self.host.legacy_calls, 0)

    def test_non_issue_event_temporarily_uses_captured_legacy_projector(self) -> None:
        state = self.service.project_gitlab_event_compat(
            {"object_kind": "pipeline"},
            project_id="home",
        )

        self.assertEqual(state.ref, "legacy")
        self.assertEqual(self.host.legacy_calls, 1)
        self.assertEqual(self.reconciler.calls, [])


if __name__ == "__main__":
    unittest.main()
