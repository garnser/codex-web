from __future__ import annotations

import unittest
from itertools import permutations
from types import SimpleNamespace

from codex_web.models import BotReplyTarget, QueuedTurn
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
            _work_item_state=lambda _ref: None,
        )
        self.policy = WorkItemWakeupQueuePolicy(self.host)

    @staticmethod
    def queued(
        queued_id: str,
        message: str,
        *,
        thread_id: str = "thread-1",
        source: str = "web",
        work_item_ref: str | None = None,
        created_at: float = 1.0,
    ) -> QueuedTurn:
        return QueuedTurn(
            id=queued_id,
            thread_id=thread_id,
            project_id="project-1",
            message=message,
            created_at=created_at,
            source=source,
            work_item_ref=work_item_ref,
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

    def test_coalesce_keeps_only_latest_gitlab_update_per_work_item(self) -> None:
        first = self.queued(
            "first",
            "old issue snapshot",
            source="gitlab",
            work_item_ref="group/project#12",
        )
        other = self.queued(
            "other",
            "another issue",
            source="gitlab",
            work_item_ref="group/project#13",
        )
        latest = self.queued(
            "latest",
            "new issue snapshot",
            source="gitlab",
            work_item_ref="group/project#12",
        )

        compacted, changed = self.policy.coalesce([first, other, latest])

        self.assertTrue(changed)
        self.assertEqual([item.id for item in compacted], ["other", "latest"])

    def test_coalesce_preserves_unrelated_gitlab_events(self) -> None:
        items = [
            self.queued("pipeline", "pipeline event", source="gitlab"),
            self.queued(
                "issue",
                "issue event",
                source="gitlab",
                work_item_ref="group/project#12",
            ),
        ]

        compacted, changed = self.policy.coalesce(items)

        self.assertIs(compacted, items)
        self.assertFalse(changed)

    def test_front_requeue_and_restore_order_cannot_replace_newer_snapshot(self) -> None:
        updates = [self.queued(name, "snapshot " + name, source="gitlab",
                              work_item_ref="group/project#12", created_at=at)
                   for name, at in (("newest", 3.0), ("old", 1.0), ("middle", 2.0))]
        for ordering in permutations(updates):
            with self.subTest(order=[item.id for item in ordering]):
                compacted, changed = self.policy.coalesce(list(ordering))
                self.assertTrue(changed)
                self.assertEqual([item.id for item in compacted], ["newest"])
                self.assertEqual(compacted[0].message, "snapshot newest")

    def test_equal_admission_time_has_stable_id_tie_break(self) -> None:
        updates = [self.queued(name, name, source="gitlab", work_item_ref="group/project#12")
                   for name in ("a", "z")]
        for ordering in permutations(updates):
            self.assertEqual([item.id for item in self.policy.coalesce(list(ordering))[0]], ["z"])

    def test_same_reference_on_other_thread_is_not_coalesced(self) -> None:
        items = [self.queued(name, name, source="gitlab", work_item_ref="group/project#12",
                             thread_id=name) for name in ("thread-1", "thread-2")]
        compacted, changed = self.policy.coalesce(items)
        self.assertIs(compacted, items)
        self.assertFalse(changed)

    def test_reply_bearing_gitlab_notifications_survive_compaction_and_terminal_cleanup(self) -> None:
        reply = self.queued("reply", "reply notification", source="gitlab", work_item_ref="group/project#12")
        reply.reply_target = BotReplyTarget(thread_id="thread-1", provider="slack",
                                           external_conversation_id="channel", updated_at=1.0)
        ordinary = self.queued("ordinary", "latest snapshot", source="gitlab",
                               work_item_ref="group/project#12", created_at=2.0)
        compacted, changed = self.policy.coalesce([reply, ordinary])
        self.assertFalse(changed)
        self.assertEqual([item.id for item in compacted], ["reply", "ordinary"])
        self.host._work_item_state = lambda _ref: SimpleNamespace(current_stage="closed")
        compacted, changed = self.policy.coalesce([reply, ordinary])
        self.assertTrue(changed)
        self.assertEqual([item.id for item in compacted], ["reply"])

    def test_compaction_provenance_follows_successful_save_and_contains_no_message(self) -> None:
        order = []
        self.queues = {"thread-1": [
            self.queued("new", "private newest body", source="gitlab",
                        work_item_ref="group/project#12", created_at=2.0),
            self.queued("old", "private old body", source="gitlab", work_item_ref="group/project#12"),
        ]}
        self.host._save_turn_queues = lambda _queues: order.append("saved")
        self.host._append_bot_event = lambda event: order.append(event)
        self.policy.compact_queues()
        self.assertEqual(order[0], "saved")
        self.assertEqual(order[1]["replacements"], [{"removed_id": "old", "retained_id": "new",
                                                   "work_item_ref": "group/project#12", "project_id": "project-1"}])
        self.assertNotIn("private", str(order))
        self.policy.compact_queues()
        self.assertEqual(len(order), 2)

    def test_failed_queue_save_does_not_emit_compaction_receipt(self) -> None:
        self.queues = {"thread-1": [self.queued(name, name, source="gitlab",
                                               work_item_ref="group/project#12") for name in ("a", "z")]}
        events = []
        self.host._append_bot_event = events.append
        def fail(_queues):
            raise RuntimeError("queue save failed")
        self.host._save_turn_queues = fail
        with self.assertRaises(RuntimeError):
            self.policy.compact_queues()
        self.assertEqual(events, [])

    def test_coalesce_discards_gitlab_notification_for_terminal_item(self) -> None:
        self.host._work_item_state = lambda ref: (
            SimpleNamespace(
                current_stage="closed",
                closed_at=2.0,
                terminal_outcome="completed",
            )
            if ref == "group/project#12"
            else SimpleNamespace(
                current_stage="implementation_active",
                closed_at=None,
                terminal_outcome=None,
            )
        )
        terminal = self.queued(
            "terminal",
            "closed issue update",
            source="gitlab",
            work_item_ref="group/project#12",
        )
        actionable = self.queued(
            "actionable",
            "active issue update",
            source="gitlab",
            work_item_ref="group/project#13",
        )

        compacted, changed = self.policy.coalesce([terminal, actionable])

        self.assertTrue(changed)
        self.assertEqual([item.id for item in compacted], ["actionable"])

    def test_coalesce_keeps_terminal_handoffs_and_unresolved_notifications(self) -> None:
        self.host._work_item_state = lambda _ref: SimpleNamespace(
            current_stage="closed",
            closed_at=2.0,
            terminal_outcome="completed",
        )
        handoff = self.queued(
            "handoff",
            "explicit handoff",
            source="work-item-handoff",
            work_item_ref="group/project#12",
        )
        unresolved = self.queued(
            "unresolved",
            "unknown issue update",
            source="gitlab",
        )

        items = [handoff, unresolved]
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

    def test_compact_queues_removes_empty_queue_records(self) -> None:
        self.queues = {
            "empty": [],
            "thread-1": [self.queued("ordinary", "ordinary message")],
        }

        self.policy.compact_queues()

        self.assertEqual(list(self.queues), ["thread-1"])
        self.assertEqual(len(self.saved), 1)

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
