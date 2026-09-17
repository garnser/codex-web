from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import QueuedTurn
from codex_web.services.work_item_wakeups import (
    WORK_ITEM_WAKEUP_BATCH_HEADER,
    WorkItemWakeupQueuePolicy,
    install_work_item_wakeup_queue_policy,
)


class WorkItemWakeupQueuePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved: list[dict[str, list[QueuedTurn]]] = []
        self.queues: dict[str, list[QueuedTurn]] = {}

        def save(queues: dict[str, list[QueuedTurn]]) -> None:
            self.saved.append(queues)

        self.host = SimpleNamespace(
            _load_turn_queues=lambda: self.queues,
            _save_turn_queues=save,
            _truncate_text=lambda value, limit: value[:limit],
        )
        self.policy = WorkItemWakeupQueuePolicy(self.host)

    @staticmethod
    def queued(queued_id: str, message: str, *, thread_id: str = "thread-1") -> QueuedTurn:
        return QueuedTurn(
            id=queued_id,
            thread_id=thread_id,
            project_id="project-1",
            message=message,
            created_at=1.0,
        )

    @staticmethod
    def legacy_message(ref: str, action: str = "continue implementation") -> str:
        return (
            f"Owned-work wakeup for {ref}. "
            "Change classification: implementation. "
            "Current stage: implementation_active. "
            f"Exact next action: {action} If blocked, state one exact blocker."
        )

    def test_entries_parse_legacy_single_wakeup(self) -> None:
        entries = self.policy.entries(self.legacy_message("group/project#12", "run the focused tests"))

        self.assertEqual(
            entries,
            [
                {
                    "ref": "group/project#12",
                    "classification": "implementation",
                    "stage": "implementation_active",
                    "next_action": "run the focused tests",
                }
            ],
        )
        self.assertEqual(self.policy.entries("ordinary queued message"), [])

    def test_render_batch_deduplicates_by_ref_and_parses_round_trip(self) -> None:
        message = self.policy.render_batch(
            [
                {"ref": "group/project#1", "next_action": "old"},
                {"ref": "group/project#2", "stage": "ready_for_validation"},
                {"ref": "group/project#1", "next_action": "new"},
            ]
        )

        self.assertTrue(message.startswith(WORK_ITEM_WAKEUP_BATCH_HEADER))
        self.assertEqual(
            self.policy.entries(message),
            [
                {"next_action": "new", "ref": "group/project#1"},
                {"ref": "group/project#2", "stage": "ready_for_validation"},
            ],
        )

    def test_render_batch_preserves_legacy_next_action_truncation(self) -> None:
        message = self.policy.render_batch(
            [{"ref": "group/project#1", "next_action": "x" * 800}]
        )
        entries = self.policy.entries(message)
        self.assertEqual(len(entries[0]["next_action"]), 600)

    def test_coalesce_merges_wakeups_and_preserves_other_queue_entries(self) -> None:
        first = self.queued("one", self.legacy_message("group/project#1", "first action"))
        ordinary = self.queued("ordinary", "ordinary message")
        second = self.queued("two", self.legacy_message("group/project#2", "second action"))

        compacted, changed = self.policy.coalesce([first, ordinary, second])

        self.assertTrue(changed)
        self.assertEqual([item.id for item in compacted], ["one", "ordinary"])
        self.assertEqual(
            [entry["ref"] for entry in self.policy.entries(compacted[0].message)],
            ["group/project#1", "group/project#2"],
        )

    def test_coalesce_is_noop_with_fewer_than_two_wakeups(self) -> None:
        items = [
            self.queued("one", self.legacy_message("group/project#1")),
            self.queued("ordinary", "ordinary message"),
        ]
        compacted, changed = self.policy.coalesce(items)
        self.assertIs(compacted, items)
        self.assertFalse(changed)

    def test_compact_queues_saves_only_when_a_queue_changes(self) -> None:
        self.queues = {
            "thread-1": [
                self.queued("one", self.legacy_message("group/project#1")),
                self.queued("two", self.legacy_message("group/project#2")),
            ],
            "thread-2": [self.queued("ordinary", "ordinary message", thread_id="thread-2")],
        }

        self.policy.compact_queues()

        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.queues["thread-1"]), 1)
        self.assertEqual(len(self.queues["thread-2"]), 1)

        self.saved.clear()
        self.policy.compact_queues()
        self.assertEqual(self.saved, [])

    def test_installer_rebinds_historical_wakeup_names(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace(
            _load_turn_queues=lambda: {},
            _save_turn_queues=lambda queues: None,
            _truncate_text=lambda value, limit: value[:limit],
        )

        policy = install_work_item_wakeup_queue_policy(app, host)

        self.assertIs(app.state.work_item_wakeup_queue_policy, policy)
        self.assertEqual(host.WORK_ITEM_WAKEUP_BATCH_HEADER, WORK_ITEM_WAKEUP_BATCH_HEADER)
        self.assertIs(host._work_item_wakeup_entries.__self__, policy)
        self.assertIs(host._render_work_item_wakeup_batch.__self__, policy)
        self.assertIs(host._coalesce_queued_work_item_wakeups.__self__, policy)
        self.assertIs(host._compact_turn_queues.__self__, policy)


if __name__ == "__main__":
    unittest.main()
