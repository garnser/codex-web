from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import BotBinding, BotReplyTarget
from codex_web.services.identity import AuthorizationError
from codex_web.services.operational_compaction import (
    OperationalCompactionError,
    OperationalCompactionService,
    OperationalCompactionStale,
)
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


def _actor(*, admin: bool = True) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="operator",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="local",
        workspace_id="default",
        roles=(
            (MembershipRole.ADMIN,)
            if admin
            else (MembershipRole.MEMBER,)
        ),
        assurance=AuthenticationAssurance.MFA,
    )


def _binding() -> BotBinding:
    return BotBinding(
        id="binding-1",
        provider="slack",
        external_conversation_id="C1",
        thread_id="thread-1",
        project_id="home",
        created_at=1.0,
        updated_at=2.0,
    )


def _target(
    external: str,
    *,
    updated_at: float,
    thread_id: str = "thread-1",
) -> BotReplyTarget:
    return BotReplyTarget(
        thread_id=thread_id,
        provider="slack",
        external_conversation_id="C1",
        external_thread_id=external,
        message_id=external,
        updated_at=updated_at,
    )


class OperationalCompactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / "state.sqlite3")
        self.delivery = ModelMapRepository(
            self.store,
            namespace="bot_delivery_targets",
            legacy_path=root / "bot_delivery_targets.json",
            model=BotReplyTarget,
        )
        self.reply = ModelMapRepository(
            self.store,
            namespace="bot_reply_targets",
            legacy_path=root / "bot_reply_targets.json",
            model=BotReplyTarget,
        )
        self.queues = TurnQueueRepository(
            self.store,
            root / "queued_turns.json",
        )
        self.bindings = [_binding()]
        self.events = root / "bot_events.jsonl"
        self.backups = root / "compaction-backups"
        self.service = OperationalCompactionService(
            state_store=self.store,
            delivery_targets=self.delivery,
            reply_targets=self.reply,
            turn_queues=self.queues,
            load_bindings=lambda: list(self.bindings),
            event_journal=self.events,
            backup_directory=self.backups,
            clock=lambda: 1_800_000_000.0,
        )

    def _seed_targets(self) -> None:
        old = _target("100.1", updated_at=1.0)
        latest = _target("200.2", updated_at=2.0)
        orphan = _target(
            "orphan",
            updated_at=3.0,
            thread_id="thread-orphan",
        )
        self.delivery.save(
            {
                "slack:C1:thread-1": old,
                "thread:thread-1:slack:C1": old,
                "slack:C1:external:100.1": old,
                "slack:C1:external:200.2": latest,
                "slack:C1:external:orphan": orphan,
            }
        )

    def test_bounded_inspection_uses_metadata_and_exact_registry_counts(self):
        self.delivery.put(
            "slack:C1:thread-1",
            _target("200.2", updated_at=2.0),
        )
        self.events.touch()
        with self.events.open("r+b") as handle:
            handle.truncate(129 * 1024 * 1024)

        report = self.service.inspect_bounded()
        by_store = {item.store: item for item in report.stores}

        self.assertTrue(by_store["bot_event_journal"].warning)
        self.assertFalse(
            by_store["bot_event_journal"].count_exact
        )
        self.assertEqual(
            by_store["bot_delivery_targets"].count,
            1,
        )
        self.assertEqual(
            by_store["bot_event_journal"].details[
                "full_scan_performed"
            ],
            False,
        )

    def test_plan_only_deletes_superseded_attributed_external_alias(self):
        self._seed_targets()

        plan = self.service.plan_delivery_target_compaction(
            actor=_actor(),
        )

        self.assertEqual(
            plan.delete_keys,
            ("slack:C1:external:100.1",),
        )
        self.assertEqual(plan.removed_count, 1)
        self.assertEqual(plan.preserved_unattributed_count, 1)
        self.assertEqual(
            plan.upserts["slack:C1:thread-1"].external_thread_id,
            "200.2",
        )
        self.assertEqual(
            plan.upserts[
                "thread:thread-1:slack:C1"
            ].external_thread_id,
            "200.2",
        )
        self.assertNotIn(
            "slack:C1:external:orphan",
            plan.delete_keys,
        )

    def test_apply_creates_private_backup_verifies_and_restores(self):
        self._seed_targets()
        actor = _actor()
        plan = self.service.plan_delivery_target_compaction(
            actor=actor,
        )

        execution = self.service.apply_delivery_target_compaction(
            plan,
            actor=actor,
        )

        self.assertEqual(execution.status, "completed")
        backup = Path(
            execution.backup_ref.removeprefix("file://")
        )
        self.assertTrue(backup.exists())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        compacted = self.delivery.load()
        self.assertNotIn(
            "slack:C1:external:100.1",
            compacted,
        )
        self.assertIn(
            "slack:C1:external:orphan",
            compacted,
        )
        self.assertEqual(
            compacted["slack:C1:thread-1"].external_thread_id,
            "200.2",
        )
        mirrored = json.loads(
            self.delivery.legacy_path.read_text()
        )
        self.assertNotIn(
            "slack:C1:external:100.1",
            mirrored,
        )

        restored = self.service.restore_delivery_target_backup(
            execution.backup_ref,
            actor=actor,
        )
        self.assertTrue(restored["restored"])
        self.assertIn(
            "slack:C1:external:100.1",
            self.delivery.load(),
        )

    def test_stale_plan_refuses_mutation_before_backup(self):
        self._seed_targets()
        actor = _actor()
        plan = self.service.plan_delivery_target_compaction(
            actor=actor,
        )
        self.delivery.put(
            "slack:C1:external:300.3",
            _target("300.3", updated_at=3.0),
        )

        with self.assertRaises(OperationalCompactionStale):
            self.service.apply_delivery_target_compaction(
                plan,
                actor=actor,
            )

        self.assertFalse(self.backups.exists())

    def test_crash_after_backup_leaves_original_and_recovery_copy(self):
        self._seed_targets()
        actor = _actor()
        plan = self.service.plan_delivery_target_compaction(
            actor=actor,
        )

        with self.assertRaises(OperationalCompactionError):
            self.service.apply_delivery_target_compaction(
                plan,
                actor=actor,
                fail_at="after_backup",
            )

        self.assertIn(
            "slack:C1:external:100.1",
            self.delivery.load(),
        )
        backups = list(self.backups.glob("*.json"))
        self.assertEqual(len(backups), 1)

    def test_crash_after_replace_is_recoverable_from_recorded_backup(self):
        self._seed_targets()
        actor = _actor()
        plan = self.service.plan_delivery_target_compaction(
            actor=actor,
        )

        with self.assertRaises(OperationalCompactionError):
            self.service.apply_delivery_target_compaction(
                plan,
                actor=actor,
                fail_at="after_replace",
            )

        self.assertNotIn(
            "slack:C1:external:100.1",
            self.delivery.load(),
        )
        execution = self.service.executions(actor=actor)[0]
        restored = self.service.restore_delivery_target_backup(
            execution.backup_ref,
            actor=actor,
        )
        self.assertTrue(restored["restored"])
        self.assertIn(
            "slack:C1:external:100.1",
            self.delivery.load(),
        )

    def test_non_admin_cannot_plan_apply_or_restore(self):
        self._seed_targets()
        member = _actor(admin=False)

        with self.assertRaises(AuthorizationError):
            self.service.plan_delivery_target_compaction(
                actor=member,
            )


if __name__ == "__main__":
    unittest.main()
