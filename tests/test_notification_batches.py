import json
import asyncio
import unittest
from copy import deepcopy

from pydantic import ValidationError

from codex_web.models import (
    BotReplyTarget,
    QueuedTurn,
    QueuedNativeStartReceipt,
    Project,
)
from codex_web.notification_batches import (
    NotificationBatchPlan,
    select_notification_batch,
)
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.services.notification_batches import NotificationBatchCoordinator
from unittest.mock import patch
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from fastapi import HTTPException
from codex_web.notification_provenance import (
    ProviderNotificationPin,
    verified_notification_ids,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.storage.canonical_events import CanonicalEventStore


class NotificationBatchPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ExecutionPreflightService(
            ExecutionPreflightStore(
                SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
            )
        )
        self.items = [
            QueuedTurn(
                id=f"original-{index}",
                execution_id=f"execution-{index}",
                thread_id="thread-a",
                project_id="project-a",
                message=f"Synthetic original notification {index}",
                work_item_ref=f"group/project#{index}",
                created_at=index,
                source="gitlab",
                execution_profile_id="orchestration-only",
                agent_profile_id="agent-a",
                agent_profile_revision=7,
                agent_profile_actor_id="actor-a",
                reply_target=BotReplyTarget(
                    provider="slack",
                    connection_id="connection-a",
                    external_conversation_id="channel-a",
                    thread_id="thread-a",
                    updated_at=1,
                ),
                repository_resource_id="repo-a",
                writable_repository_resource_ids=("repo-a",),
            )
            for index in range(1, 5)
        ]
        self.attempts = {
            item.id: self.service.retain_queued(
                item, organization_id="local", workspace_id="default"
            )
            for item in self.items
        }

    def select(self, items=None, **changes):
        values = dict(
            anchor_id=self.items[0].id,
            attempts_by_original_id=self.attempts,
            verified_notification_ids=frozenset(item.id for item in self.items),
            organization_id="local",
            authority_scope_sha256_by_original_id={
                item.id: "a" * 64 for item in self.items
            },
            notification_source_origin="gitlab",
            workspace_id="default",
            max_members=3,
            max_context_bytes=65536,
        )
        values.update(changes)
        return select_notification_batch(
            self.items if items is None else items, **values
        )

    def test_complete_originals_and_requested_execution_ids_survive_roundtrip(self):
        before = deepcopy([item.model_dump(mode="json") for item in self.items])
        plan = self.select()
        restored = NotificationBatchPlan.model_validate_json(plan.model_dump_json())
        self.assertEqual(
            [json.loads(value) for value in restored.original_snapshots_json],
            before[:3],
        )
        self.assertEqual(
            [item.execution_id for item in restored.members],
            ["execution-1", "execution-2", "execution-3"],
        )
        self.assertEqual([item.model_dump(mode="json") for item in self.items], before)
        self.assertEqual(len(self.service.store.for_thread("thread-a")), 4)
        self.assertTrue(
            all(
                item.status == "queued"
                for item in self.service.store.for_thread("thread-a")
            )
        )

    def test_reply_metadata_time_is_preserved_without_changing_destination_compatibility(
        self,
    ):
        items = deepcopy(self.items[:2])
        items[1].reply_target.updated_at = 2
        self.attempts[items[1].id] = self.attempts[items[1].id].model_copy(
            update={"queued_turn_snapshot": items[1].model_dump(mode="json")}
        )
        plan = self.select(items)
        self.assertEqual(plan.members[1].reply_target.updated_at, 2)
        self.assertEqual(
            json.loads(plan.original_snapshots_json[1])["reply_target"]["updated_at"], 2
        )

    def test_verified_replacement_preserves_immutable_original_and_current_route(self):
        originals = deepcopy(self.items[:2])
        self.service.replacement_resolver = lambda old, **scope: "replacement-thread"
        moved = []
        for original in originals:
            current = original.model_copy(deep=True)
            current.thread_id = "replacement-thread"
            current.reply_target.thread_id = "replacement-thread"
            self.attempts[current.id] = self.service.retain_queued(
                current, organization_id="local", workspace_id="default"
            )
            moved.append(current)
        plan = self.select(moved)
        self.assertEqual(
            json.loads(plan.original_snapshots_json[0])["thread_id"], "thread-a"
        )
        self.assertEqual(plan.members[0].thread_id, "replacement-thread")
        self.assertEqual(plan.members[0].reply_target.thread_id, "replacement-thread")

    def test_exact_profile_repository_reply_and_context_scope(self):
        for field, value in (
            ("agent_profile_revision", 8),
            ("agent_profile_actor_id", "foreign"),
            ("execution_profile_id", "write"),
            ("repository_resource_id", "repo-b"),
            ("writable_repository_resource_ids", ("repo-b",)),
            ("project_id", "foreign"),
            ("reply_target", None),
            ("sandbox", "read-only"),
        ):
            items = deepcopy(self.items[:2])
            items[1] = items[1].model_copy(update={field: value})
            with self.subTest(field=field):
                self.assertIsNone(self.select(items))

    def test_unknown_started_handoff_and_unattested_entries_stay_separate(self):
        for status in (
            "reconciliation_required",
            "started",
            "admitting",
            "blocked",
            "retrying",
        ):
            attempts = dict(self.attempts)
            attempts[self.items[1].id] = attempts[self.items[1].id].model_copy(
                update={"status": status}
            )
            with self.subTest(status=status):
                self.assertIsNone(
                    self.select(self.items[:2], attempts_by_original_id=attempts)
                )
        self.assertIsNone(
            self.select(
                self.items[:2], verified_notification_ids=frozenset({self.items[0].id})
            )
        )
        handoff = deepcopy(self.items[:2])
        handoff[1].handoff_requested_at = 123
        self.assertIsNone(self.select(handoff))
        human = deepcopy(self.items[:2])
        human[0].source = "slack"
        self.assertIsNone(self.select(human))

    def test_no_message_based_attestation_and_snapshot_change_or_foreign_tenant(self):
        items = deepcopy(self.items[:2])
        items[1].message = "Changed request content"
        self.assertIsNone(self.select(items))
        self.assertIsNone(self.select(self.items[:2], organization_id="foreign"))
        self.assertIsNone(self.select(self.items[:2], workspace_id="foreign"))
        self.assertIsNone(
            self.select(self.items[:2], verified_notification_ids=frozenset())
        )

    def test_current_authority_scope_must_match_despite_same_profile(self):
        self.assertIsNone(
            self.select(self.items[:2], authority_scope_sha256_by_original_id={})
        )
        self.assertIsNone(
            self.select(
                self.items[:2],
                authority_scope_sha256_by_original_id={
                    "original-1": "a" * 64,
                    "original-2": "b" * 64,
                },
            )
        )

    def test_provider_selection_is_data_and_human_protocols_are_forbidden(self):
        items = deepcopy(self.items[:2])
        attempts = dict(self.attempts)
        for item in items:
            item.source = "github"
            attempts[item.id] = attempts[item.id].model_copy(
                update={
                    "source": "github",
                    "queued_turn_snapshot": item.model_dump(mode="json"),
                }
            )
        self.assertIsNotNone(
            self.select(
                items,
                attempts_by_original_id=attempts,
                notification_source_origin="github",
            )
        )
        for source in ("web", "slack", "", "SLACK", "queued:slack"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.select(notification_source_origin=source)

    def test_byte_bound_never_truncates_or_consumes_oversized_original(self):
        items = deepcopy(self.items)
        items[1].message = "🧪" * 10000
        self.attempts[items[1].id] = self.service.retain_queued(
            items[1].model_copy(update={"id": "new-long"}),
            organization_id="local",
            workspace_id="default",
        )
        # Use an entirely new original so the canonical store refuses no changed identity.
        items[1].id = "new-long"
        self.attempts[items[1].id] = self.attempts.pop("original-2")
        plan = self.select(
            items,
            verified_notification_ids=frozenset(item.id for item in items),
            max_context_bytes=10000,
            authority_scope_sha256_by_original_id={item.id: "a" * 64 for item in items},
        )
        self.assertEqual(
            [item.id for item in plan.members],
            ["original-1", "original-3", "original-4"],
        )
        self.assertEqual(len(items[1].message), 10000)
        self.assertLessEqual(len(plan.native_input().encode()), 10000)

    def test_anchor_and_compatible_order_preserve_unrelated_work(self):
        plan = self.select(anchor_id="original-2")
        self.assertEqual(
            [item.id for item in plan.members],
            ["original-2", "original-3", "original-4"],
        )
        self.assertIsNone(self.select(anchor_id="missing"))
        with self.assertRaises(ValueError):
            self.select([self.items[0], self.items[0]])

    def test_fingerprint_rejects_changed_retained_snapshot_and_detaches_members(self):
        plan = self.select()
        member = plan.members[0]
        member.message = "Mutation of detached view"
        self.assertNotIn("Mutation of detached view", plan.native_input())
        data = plan.model_dump(mode="json")
        value = json.loads(data["original_snapshots_json"][0])
        value["message"] = "Tampered"
        data["original_snapshots_json"][0] = json.dumps(value)
        with self.assertRaises(ValidationError):
            NotificationBatchPlan.model_validate(data)

    def test_invalid_bounds_and_explicit_retry_do_not_join_batch(self):
        for changes in (
            {"max_members": 17},
            {"max_members": 1},
            {"max_members": 2.5},
            {"max_members": True},
            {"max_context_bytes": 255},
            {"max_context_bytes": 262145},
            {"max_context_bytes": 65536.5},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.select(**changes)
        attempts = dict(self.attempts)
        attempts[self.items[1].id] = attempts[self.items[1].id].model_copy(
            update={"attempt_number": 2}
        )
        self.assertIsNone(self.select(self.items[:2], attempts_by_original_id=attempts))

    def test_atomic_claim_reopen_and_idempotent_retry_preserve_originals(self):
        plan = self.select()
        before = {
            key: value.queued_turn_snapshot for key, value in self.attempts.items()
        }
        admission = self.service.store.claim_notification_batch(
            plan, claim_id="claim-one", now=10
        )
        reopened = ExecutionPreflightStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
        )
        self.assertEqual(reopened.get_notification_batch(admission.id), admission)
        self.assertEqual(
            reopened.claim_notification_batch(plan, claim_id="claim-one", now=20),
            admission,
        )
        for member in plan.members:
            stored = reopened.get("preflight-queued-" + member.id)
            self.assertEqual(stored.notification_batch_id, admission.id)
            self.assertEqual(stored.status, "admitting")
            self.assertEqual(stored.queued_turn_snapshot, before[member.id])
            self.assertIsNone(stored.queued_start_receipt)
        self.assertEqual(reopened.get("preflight-queued-original-4").status, "queued")

    def test_failed_last_member_rechecks_commit_no_partial_claim(self):
        plan = self.select()
        self.service.store.update(
            "preflight-queued-original-3",
            lambda current: current.model_copy(
                update={"status": "reconciliation_required"}
            ),
        )
        with self.assertRaises(ValueError):
            self.service.store.claim_notification_batch(
                plan, claim_id="claim-one", now=10
            )
        self.assertEqual(
            self.service.store.get("preflight-queued-original-1").status, "queued"
        )
        self.assertEqual(
            self.service.store.get("preflight-queued-original-2").status, "queued"
        )
        self.assertIsNone(
            self.service.store.get("preflight-queued-original-1").notification_batch_id
        )

    def test_concurrent_claims_have_one_owner_for_all_members(self):
        plan = self.select()

        def claim(value):
            store = ExecutionPreflightStore(
                SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
            )
            try:
                return store.claim_notification_batch(plan, claim_id=value, now=10)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["claim-one", "claim-two"]))
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            {
                self.service.store.get(value).queued_admission_claim_id
                for value in winners[0].attempt_ids
            },
            {winners[0].claim_id},
        )

    def test_prepared_release_allows_new_claim_without_erasing_history(self):
        plan = self.select()
        first = self.service.store.claim_notification_batch(
            plan, claim_id="claim-one", now=10
        )
        released = self.service.store.transition_notification_batch(
            first.id, claim_id="claim-one", phase="released"
        )
        second = self.service.store.claim_notification_batch(
            plan, claim_id="claim-two", now=20
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.service.store.get_notification_batch(first.id), released)
        self.assertEqual(
            self.service.store.get("preflight-queued-original-1").notification_batch_id,
            second.id,
        )

    def test_native_unknown_is_durable_and_cannot_release_or_single_retry(self):
        plan = self.select()
        admission = self.service.store.claim_notification_batch(
            plan, claim_id="claim-one", now=10
        )
        self.service.store.transition_notification_batch(
            admission.id, claim_id="claim-one", phase="native_outcome_unknown"
        )
        reopened = ExecutionPreflightStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
        )
        self.assertEqual(
            reopened.get_notification_batch(admission.id).phase,
            "native_outcome_unknown",
        )
        with self.assertRaises(ValueError):
            reopened.transition_notification_batch(
                admission.id, claim_id="claim-one", phase="released"
            )
        with self.assertRaises(ValueError):
            reopened.claim_retry(
                "preflight-queued-original-1", claim_id="retry", now=30
            )
        with self.assertRaises(ValueError):
            self.service.begin_queued_start(
                "preflight-queued-original-1", claim_id="claim-one"
            )
        with self.assertRaises(ValueError):
            reopened.put(
                self.attempts["original-1"].model_copy(update={"status": "blocked"})
            )
        self.assertTrue(
            all(
                reopened.get(value).status == "reconciliation_required"
                for value in admission.attempt_ids
            )
        )

    def test_claim_loss_fences_every_member_before_native_gate(self):
        plan = self.select()
        admission = self.service.store.claim_notification_batch(
            plan, claim_id="claim-one", now=10
        )
        # Inject a changed retained claim directly in isolated fixture storage.
        key = "a/preflight-queued-original-3"
        self.service.store.store.record_update(
            self.service.store.NAMESPACE,
            key,
            lambda raw: dict(raw, queued_admission_claim_id="other-claim"),
            default=None,
        )
        with self.assertRaises(ValueError):
            self.service.store.transition_notification_batch(
                admission.id, claim_id="claim-one", phase="native_outcome_unknown"
            )
        self.assertEqual(
            self.service.store.get_notification_batch(admission.id).phase, "prepared"
        )
        self.assertEqual(
            self.service.store.get("preflight-queued-original-1").status, "admitting"
        )

    def test_legacy_preflight_shape_is_exact_until_batch_membership_exists(self):
        ordinary = self.attempts[self.items[0].id]
        self.assertNotIn("notification_batch_id", ordinary.model_dump(mode="json"))
        admission = self.service.store.claim_notification_batch(
            self.select(), claim_id="claim-one", now=10
        )
        stored = self.service.store.get(admission.attempt_ids[0]).model_dump(
            mode="json"
        )
        self.assertIn("notification_batch_id", stored)
        # The old model uses extra=forbid, hence does not silently lose membership.
        from pydantic import create_model, ConfigDict

        Legacy = create_model(
            "LegacyPreflightFields",
            __config__=ConfigDict(extra="forbid"),
            **{
                name: (field.annotation, field.default)
                for name, field in type(ordinary).model_fields.items()
                if name != "notification_batch_id"
            },
        )
        with self.assertRaises(ValidationError):
            Legacy.model_validate(stored)

    def coordinator(self):
        queue = TurnQueueRepository(
            self.service.store.store, Path(self.temp.name) / "queues.json"
        )
        queue.put("thread-a", self.items)
        return NotificationBatchCoordinator(
            queue=queue, preflight=self.service.store, clock=lambda: 10
        )

    def test_atomic_removal_preserves_concurrent_append_and_unrelated_original(self):
        coordinator = self.coordinator()
        plan = self.select()
        arrival = self.items[3].model_copy(
            update={"id": "arrival", "message": "Concurrent arrival"}
        )
        coordinator.queue.update("thread-a", lambda items: [*items, arrival])
        admission = coordinator.prepare(
            plan, claim_id="claim-one", current=lambda: True
        )
        self.assertEqual(
            [item.id for item in coordinator.queue.get("thread-a")],
            ["original-4", "arrival"],
        )
        self.assertEqual(
            admission.queue_order,
            ("original-1", "original-2", "original-3", "original-4", "arrival"),
        )
        self.assertTrue(
            all(
                self.service.store.get(value).queued_turn_snapshot is not None
                for value in admission.attempt_ids
            )
        )

    def test_changed_last_copy_removes_none_and_keeps_prepared_recovery_visible(self):
        coordinator = self.coordinator()
        plan = self.select()
        coordinator.queue.update(
            "thread-a",
            lambda items: [
                item.model_copy(update={"message": "Changed"})
                if item.id == "original-3"
                else item
                for item in items
            ],
        )
        with self.assertRaisesRegex(ValueError, "prepared recovery"):
            coordinator.prepare(plan, claim_id="claim-one", current=lambda: True)
        self.assertEqual(len(coordinator.queue.get("thread-a")), 4)
        batch_id = self.service.store.get(
            "preflight-queued-original-1"
        ).notification_batch_id
        self.assertEqual(
            self.service.store.get_notification_batch(batch_id).phase, "prepared"
        )
        with self.assertRaisesRegex(ValueError, "copy changed"):
            coordinator.recover_prepared(
                batch_id, claim_id="claim-one", current=lambda: True
            )
        self.assertEqual(coordinator.queue.get("thread-a")[2].message, "Changed")

    def test_restart_after_removal_restores_exact_originals_and_releases_all(self):
        coordinator = self.coordinator()
        plan = self.select()
        admission = coordinator.prepare(
            plan, claim_id="claim-one", current=lambda: True
        )
        queue = TurnQueueRepository(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite"),
            Path(self.temp.name) / "queues.json",
        )
        restored = NotificationBatchCoordinator(
            queue=queue, preflight=ExecutionPreflightStore(queue.store)
        )
        restored.recover_prepared(
            admission.id, claim_id="claim-one", current=lambda: True
        )
        self.assertEqual(
            [item.model_dump(mode="json") for item in queue.get("thread-a")],
            [item.model_dump(mode="json") for item in self.items],
        )
        self.assertTrue(
            all(
                restored.preflight.get(value).notification_batch_id is None
                for value in admission.attempt_ids
            )
        )

    def test_crash_after_restoration_before_release_is_idempotently_recoverable(self):
        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        with patch.object(
            self.service.store,
            "transition_notification_batch",
            side_effect=RuntimeError("Injected crash"),
        ):
            with self.assertRaises(RuntimeError):
                coordinator.recover_prepared(
                    admission.id, claim_id="claim-one", current=lambda: True
                )
        self.assertEqual(len(coordinator.queue.get("thread-a")), 4)
        coordinator.recover_prepared(
            admission.id, claim_id="claim-one", current=lambda: True
        )
        self.assertEqual(len(coordinator.queue.get("thread-a")), 4)

    def test_crash_before_removal_preserves_queue_and_prepared_claim_is_recoverable(
        self,
    ):
        coordinator = self.coordinator()
        plan = self.select()
        with patch.object(
            coordinator.queue,
            "remove_many_exact",
            side_effect=RuntimeError("Injected crash"),
        ):
            with self.assertRaises(RuntimeError):
                coordinator.prepare(plan, claim_id="claim-one", current=lambda: True)
        self.assertEqual(
            [item.id for item in coordinator.queue.get("thread-a")],
            [item.id for item in self.items],
        )
        batch_id = self.service.store.get(
            "preflight-queued-original-1"
        ).notification_batch_id
        coordinator.recover_prepared(
            batch_id, claim_id="claim-one", current=lambda: True
        )
        self.assertEqual(len(coordinator.queue.get("thread-a")), 4)
        self.assertEqual(
            self.service.store.get("preflight-queued-original-1").status, "queued"
        )

    def test_lost_ownership_and_unknown_native_gate_refuse_restoration(self):
        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        with self.assertRaises(ValueError):
            coordinator.recover_prepared(
                admission.id, claim_id="claim-one", current=lambda: False
            )
        self.assertEqual(len(coordinator.queue.get("thread-a")), 1)
        self.service.store.transition_notification_batch(
            admission.id, claim_id="claim-one", phase="native_outcome_unknown"
        )
        with self.assertRaises(ValueError):
            coordinator.recover_prepared(
                admission.id, claim_id="claim-one", current=lambda: True
            )
        self.assertEqual(len(coordinator.queue.get("thread-a")), 1)

    def test_restoration_preserves_concurrent_priority_when_original_peers_are_gone(
        self,
    ):
        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        priority = self.items[3].model_copy(update={"id": "priority"})
        ordinary = self.items[3].model_copy(update={"id": "ordinary"})
        coordinator.queue.put("thread-a", [priority, ordinary])
        coordinator.recover_prepared(
            admission.id,
            claim_id="claim-one",
            current=lambda: True,
            priority_ids=frozenset({"priority"}),
        )
        self.assertEqual(
            [item.id for item in coordinator.queue.get("thread-a")],
            ["priority", "original-1", "original-2", "original-3", "ordinary"],
        )

    def receipt(self, admission, **changes):
        values = dict(
            admission_claim_id=admission.claim_id,
            requested_execution_id=admission.id,
            execution_id="canonical-execution",
            thread_id="thread-a",
            provider_native_session_id="native-session",
            provider_native_turn_id="native-turn",
            provider_id="fixture-provider",
            runtime_id="fixture-runtime",
            observed_at=20,
        )
        values.update(changes)
        return QueuedNativeStartReceipt(**values)

    def test_singleton_receipt_is_durable_without_inventing_original_completion(self):
        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        self.service.store.consume_notification_native_start(
            admission.id,
            claim_id=admission.claim_id,
            requested_execution_id=admission.id,
            thread_id="thread-a",
            message=admission.native_input(),
        )
        receipt = self.receipt(admission)
        recorded = self.service.store.record_notification_native_start(
            admission.id, receipt
        )
        reopened = ExecutionPreflightStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
        )
        self.assertEqual(
            reopened.get_notification_batch(admission.id).native_receipt, receipt
        )
        self.assertEqual(
            reopened.record_notification_native_start(admission.id, receipt), recorded
        )
        for attempt_id in admission.attempt_ids:
            original = reopened.get(attempt_id)
            self.assertEqual(original.status, "reconciliation_required")
            self.assertIsNone(original.queued_start_receipt)
        with self.assertRaises(ValueError):
            reopened.record_notification_native_start(
                admission.id,
                self.receipt(admission, provider_native_turn_id="different"),
            )
        with self.assertRaises(ValueError):
            reopened.consume_notification_native_start(
                admission.id,
                claim_id=admission.claim_id,
                requested_execution_id=admission.id,
                thread_id="thread-a",
                message=admission.native_input(),
            )

    def test_receipt_requires_exact_claim_scope_and_consumed_native_gate(self):
        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        with self.assertRaises(ValueError):
            self.service.store.record_notification_native_start(
                admission.id, self.receipt(admission)
            )
        self.service.store.consume_notification_native_start(
            admission.id,
            claim_id=admission.claim_id,
            requested_execution_id=admission.id,
            thread_id="thread-a",
            message=admission.native_input(),
        )
        for fields in (
            {"admission_claim_id": "foreign"},
            {"thread_id": "foreign"},
            {"requested_execution_id": "original-execution"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.service.store.record_notification_native_start(
                    admission.id, self.receipt(admission, **fields)
                )
        self.assertIsNone(
            self.service.store.get_notification_batch(admission.id).native_receipt
        )

    def test_tampered_native_body_cannot_consume_gate(self):
        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        with self.assertRaises(ValueError):
            self.service.store.consume_notification_native_start(
                admission.id,
                claim_id=admission.claim_id,
                requested_execution_id=admission.id,
                thread_id="thread-a",
                message="Truncated original body",
            )
        self.assertEqual(
            self.service.store.get_notification_batch(admission.id).phase, "prepared"
        )

    def native_runtime_fixture(self):
        from tests.test_turn_execution import TurnExecutionStartTests
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.execution_workers import ExecutionRuntimeBinding

        coordinator = self.coordinator()
        admission = coordinator.prepare(
            self.select(), claim_id="claim-one", current=lambda: True
        )
        host, binding, sessions, runtime = TurnExecutionStartTests()._service()
        runtime.queued_preflight = self.service
        profile = AgentProfileExecutionBinding(
            profile_id="agent-a",
            profile_revision=7,
            profile_record_id="profile-r7",
            selected_provider_id="openai",
            selected_runtime_id="codex",
        )
        route = ExecutionRuntimeBinding(
            provider_id="openai", runtime_id="codex", capability_revision=1
        )
        runtime._select_runtime_binding = AsyncMock(return_value=(route, profile))
        assignment = sessions.session.validate_current()
        assignment.agent_profile = profile
        assignment.runtime_binding = route
        sessions.session.validate_current = MagicMock(return_value=assignment)
        actor = SimpleNamespace(
            identity_id="actor-a", organization_id="local", workspace_id="default"
        )
        project = Project(
            id="project-a",
            name="Fixture Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        return coordinator, admission, sessions, runtime, actor, project

    def test_native_runtime_records_one_actual_fixture_receipt_and_refuses_second_start(
        self,
    ):
        coordinator, admission, sessions, runtime, actor, project = (
            self.native_runtime_fixture()
        )
        result = asyncio.run(
            coordinator.submit_native(
                admission,
                runtime=runtime,
                project=project,
                actor=actor,
                current=lambda: True,
            )
        )
        recorded = self.service.store.get_notification_batch(admission.id)
        self.assertEqual(
            recorded.native_receipt.provider_native_turn_id, result["turn"]["id"]
        )
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )
        with self.assertRaises(ValueError):
            asyncio.run(
                coordinator.submit_native(
                    admission,
                    runtime=runtime,
                    project=project,
                    actor=actor,
                    current=lambda: True,
                )
            )
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )

    def test_controller_refusal_before_worker_routing_is_recoverable(self):
        coordinator, admission, sessions, runtime, actor, project = (
            self.native_runtime_fixture()
        )
        runtime.upgrade_turn_admission_guard = object()
        runtime._require_upgrade_turn_admission = AsyncMock(
            side_effect=[
                ("local", "default"),
                HTTPException(
                    status_code=409, detail={"code": "upgrade_maintenance_active"}
                ),
            ]
        )
        with self.assertRaises(HTTPException):
            asyncio.run(
                coordinator.submit_native(
                    admission,
                    runtime=runtime,
                    project=project,
                    actor=actor,
                    current=lambda: True,
                )
            )
        refused = self.service.store.get_notification_batch(admission.id)
        self.assertEqual(refused.phase, "prestart_refused")
        self.assertEqual(refused.prestart_refusal_code, "upgrade_maintenance_active")
        self.assertEqual(sessions.session.requests, [])
        coordinator.recover_prepared(
            admission.id, claim_id=admission.claim_id, current=lambda: True
        )
        self.assertEqual(
            [item.id for item in coordinator.queue.get("thread-a")],
            [item.id for item in self.items],
        )
        self.assertEqual(
            self.service.store.get_notification_batch(
                admission.id
            ).prestart_refusal_code,
            "upgrade_maintenance_active",
        )

    def test_untyped_guard_error_remains_unknown_and_not_recoverable(self):
        coordinator, admission, sessions, runtime, actor, project = (
            self.native_runtime_fixture()
        )
        runtime.upgrade_turn_admission_guard = object()
        runtime._require_upgrade_turn_admission = AsyncMock(
            side_effect=[("local", "default"), RuntimeError("Unknown failure")]
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(
                coordinator.submit_native(
                    admission,
                    runtime=runtime,
                    project=project,
                    actor=actor,
                    current=lambda: True,
                )
            )
        self.assertEqual(
            self.service.store.get_notification_batch(admission.id).phase,
            "native_outcome_unknown",
        )
        with self.assertRaises(ValueError):
            coordinator.recover_prepared(
                admission.id, claim_id=admission.claim_id, current=lambda: True
            )

    def test_receipt_write_failure_does_not_repeat_native_start(self):
        coordinator, admission, sessions, runtime, actor, project = (
            self.native_runtime_fixture()
        )
        with patch.object(
            self.service.store,
            "record_notification_native_start",
            side_effect=RuntimeError("Receipt store failure"),
        ):
            with self.assertRaises(Exception):
                asyncio.run(
                    coordinator.submit_native(
                        admission,
                        runtime=runtime,
                        project=project,
                        actor=actor,
                        current=lambda: True,
                    )
                )
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )
        self.assertEqual(
            self.service.store.get_notification_batch(admission.id).phase,
            "native_outcome_unknown",
        )
        with self.assertRaises(ValueError):
            asyncio.run(
                coordinator.submit_native(
                    admission,
                    runtime=runtime,
                    project=project,
                    actor=actor,
                    current=lambda: True,
                )
            )
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )

    def pinned(self, item, event_id="provider-event"):
        store = CanonicalEventStore(self.service.store.store)
        event = CanonicalEventEnvelope(
            event_id=event_id,
            event_type="task_source.event",
            occurred_at=1,
            source="gitlab:fixture",
            tenant_id="local",
            workspace_id="default",
            payload={"project_id": "project-a", "provider_event_type": "issue.updated"},
        )
        store.record_if_new(event, idempotency_key=event_id)
        pin = ProviderNotificationPin.from_event(
            event,
            source_origin="gitlab",
            provider_event_kind="issue",
            work_item_ref=item.work_item_ref,
            message=item.message,
        )
        return item.model_copy(update={"provider_notification_pin": pin}), store

    def test_provider_pin_reopens_against_exact_canonical_event_without_inference(self):
        pinned, store = self.pinned(self.items[0])
        reopened = CanonicalEventStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
        )
        restored = QueuedTurn.model_validate_json(pinned.model_dump_json())
        self.assertEqual(
            verified_notification_ids(
                [restored, self.items[1]],
                event_lookup=reopened.event,
                organization_id="local",
                workspace_id="default",
            ),
            frozenset({pinned.id}),
        )
        self.assertEqual(
            verified_notification_ids(
                [restored],
                event_lookup=lambda event: None,
                organization_id="local",
                workspace_id="default",
            ),
            frozenset(),
        )

    def test_provider_pin_rejects_changed_content_event_scope_handoff_and_note(self):
        pinned, store = self.pinned(self.items[0])
        event = store.event(pinned.provider_notification_pin.event_id)
        for candidate, lookup in (
            (pinned.model_copy(update={"message": "Changed"}), store.event),
            (pinned.model_copy(update={"handoff_requested_at": 123}), store.event),
            (
                pinned,
                lambda event_id: event.model_copy(update={"workspace_id": "foreign"}),
            ),
            (
                pinned,
                lambda event_id: event.model_copy(
                    update={
                        "payload": {
                            **event.payload,
                            "provider_event_type": "note.created",
                        }
                    }
                ),
            ),
        ):
            self.assertEqual(
                verified_notification_ids(
                    [candidate],
                    event_lookup=lookup,
                    organization_id="local",
                    workspace_id="default",
                ),
                frozenset(),
            )
        self.assertEqual(
            verified_notification_ids(
                [pinned],
                event_lookup=store.event,
                organization_id="foreign",
                workspace_id="default",
            ),
            frozenset(),
        )

    def test_distinct_event_pins_do_not_change_compatible_execution_scope(self):
        first, store = self.pinned(self.items[0], "event-one")
        second, _ = self.pinned(self.items[1], "event-two")
        attempts = {}
        for item in (first, second):
            attempts[item.id] = self.attempts[item.id].model_copy(
                update={"queued_turn_snapshot": item.model_dump(mode="json")}
            )
        plan = self.select([first, second], attempts_by_original_id=attempts)
        self.assertIsNotNone(plan)
        self.assertEqual(
            json.loads(plan.original_snapshots_json[1])["provider_notification_pin"][
                "event_id"
            ],
            "event-two",
        )

    def test_pinned_originals_are_not_consumed_by_legacy_compaction(self):
        from codex_web.services.work_item_wakeups import WorkItemWakeupQueuePolicy

        items = []
        for index, item in enumerate(self.items[:2]):
            item = item.model_copy(
                update={
                    "reply_target": None,
                    "work_item_ref": "group/project#1",
                    "message": "Owned-work wakeup for group/project#1. Exact next action: "
                    + ("x" * 800),
                }
            )
            pinned, _ = self.pinned(item, "compaction-event-" + str(index))
            items.append(pinned)
        before = [item.model_dump(mode="json") for item in items]
        policy = WorkItemWakeupQueuePolicy(
            SimpleNamespace(
                _truncate_text=lambda value, limit: value[:limit],
                _work_item_state=lambda ref: SimpleNamespace(
                    current_stage="closed", closed_at=1
                ),
            )
        )
        kept, changed = policy.coalesce(items)
        self.assertFalse(changed)
        self.assertEqual([item.model_dump(mode="json") for item in kept], before)

    def automatic_drain_fixture(self):
        from codex_web.services.notification_batch_drain import NotificationBatchDrain

        coordinator, admission, sessions, runtime, actor, project = (
            self.native_runtime_fixture()
        )
        coordinator.recover_prepared(
            admission.id, claim_id=admission.claim_id, current=lambda: True
        )
        items = []
        for index, item in enumerate(self.items):
            original = item.model_copy(update={"id": "automatic-" + item.id})
            pinned, events = self.pinned(original, "automatic-event-" + str(index))
            items.append(pinned)
        coordinator.queue.put("thread-a", items)
        runtime.actor_resolver = lambda identity, project: actor
        runtime.ownership = SimpleNamespace(
            require_fence=lambda name: SimpleNamespace(fencing_token=7)
        )
        runtime.host._thread_queue = coordinator.queue.get
        runtime.host._thread_queue_record = coordinator.queue.get
        runtime.host._update_thread_queue_record = coordinator.queue.update
        runtime.host._project = lambda project_id: project
        policy = {"enabled": True, "max_members": 3, "max_context_bytes": 65536}
        handler = NotificationBatchDrain(
            coordinator=coordinator,
            event_lookup=events.event,
            policy_for=lambda actor, project: policy,
            authority_for=lambda item, project: "a" * 64,
        )
        runtime.notification_batch_handler = handler
        return coordinator, sessions, runtime, project, items, handler

    def test_owned_queue_drain_submits_three_originals_in_one_native_input(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        asyncio.run(runtime._drain_thread_queue_owned("thread-a"))
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )
        self.assertEqual(
            [item.id for item in coordinator.queue.get("thread-a")], [items[3].id]
        )
        first = self.service.store.get("preflight-queued-" + items[0].id)
        batch = self.service.store.get_notification_batch(first.notification_batch_id)
        self.assertEqual(batch.phase, "native_input_started")
        self.assertEqual(
            [item.id for item in batch.plan.members], [item.id for item in items[:3]]
        )
        for member in batch.attempt_ids:
            retained = self.service.store.get(member)
            self.assertEqual(retained.status, "reconciliation_required")
            self.assertIsNone(retained.queued_start_receipt)

    def test_disabled_or_unproven_automatic_batch_leaves_all_originals(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        policy_for, event_lookup = handler.policy_for, handler.event_lookup
        ownership, actor_resolver = runtime.ownership, runtime.actor_resolver
        for condition in ("disabled", "missing_event", "no_owner", "no_actor"):
            handler.policy_for, handler.event_lookup = policy_for, event_lookup
            runtime.ownership, runtime.actor_resolver = ownership, actor_resolver
            if condition == "disabled":
                handler.policy_for = lambda actor, project: {"enabled": False}
            elif condition == "missing_event":
                handler.event_lookup = lambda event_id: None
            elif condition == "no_owner":
                runtime.ownership = None
            else:
                runtime.actor_resolver = None
            with self.subTest(condition=condition):
                self.assertFalse(
                    asyncio.run(handler.try_drain(runtime, items[0], project))
                )
                self.assertEqual(coordinator.queue.get("thread-a"), items)
                self.assertEqual(sessions.session.requests, [])

    def test_changed_fence_prevents_automatic_batch_claim_and_native_io(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        runtime.ownership.require_fence = MagicMock(
            side_effect=[
                SimpleNamespace(fencing_token=7),
                SimpleNamespace(fencing_token=8),
            ]
        )
        with self.assertRaises(ValueError):
            asyncio.run(handler.try_drain(runtime, items[0], project))
        self.assertEqual(coordinator.queue.get("thread-a"), items)
        self.assertEqual(sessions.session.requests, [])

    def test_full_admission_envelope_fits_the_context_bound(self):
        from codex_web.notification_batches import NotificationBatchAdmission

        plan = self.select(self.items[:2])
        admission = NotificationBatchAdmission(
            id=NotificationBatchAdmission.identity(plan.id, "claim"),
            plan=plan,
            claim_id="claim",
            claimed_at=1,
        )
        exact = len(admission.native_input().encode())
        self.assertIsNotNone(self.select(self.items[:2], max_context_bytes=exact))
        self.assertIsNone(self.select(self.items[:2], max_context_bytes=exact - 1))

    def test_legacy_save_does_not_semantically_dedupe_distinct_provider_events(self):
        coordinator = self.coordinator()
        originals = []
        for index, item in enumerate(self.items[:2]):
            pinned, _ = self.pinned(
                item.model_copy(update={"message": "Same notification"}),
                "same-message-event-" + str(index),
            )
            originals.append(pinned)
        coordinator.queue.put("thread-a", originals)
        snapshot = coordinator.queue.load()
        snapshot["thread-a"].append(self.items[2])
        coordinator.queue.save(snapshot)
        self.assertEqual(coordinator.queue.get("thread-a"), [*originals, self.items[2]])

    def test_competing_claim_is_never_released_by_automatic_drain(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        prepare = coordinator.prepare
        competitor = []

        def competing(plan, *, claim_id, current):
            competitor.append(prepare(plan, claim_id="other-owner", current=current))
            raise ValueError("Another claim won")

        with (
            patch.object(coordinator, "prepare", side_effect=competing),
            patch.object(
                coordinator, "recover_prepared", wraps=coordinator.recover_prepared
            ) as recover,
        ):
            self.assertTrue(asyncio.run(handler.try_drain(runtime, items[0], project)))
        recover.assert_not_called()
        self.assertEqual(
            self.service.store.get_notification_batch(competitor[0].id).phase,
            "prepared",
        )
        self.assertEqual(sessions.session.requests, [])

    def test_automatic_drain_recovers_only_affirmative_preworker_refusal(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        runtime.upgrade_turn_admission_guard = object()
        runtime._require_upgrade_turn_admission = AsyncMock(
            side_effect=[
                ("local", "default"),
                HTTPException(
                    status_code=409, detail={"code": "upgrade_maintenance_active"}
                ),
            ]
        )
        self.assertTrue(asyncio.run(handler.try_drain(runtime, items[0], project)))
        self.assertEqual(coordinator.queue.get("thread-a"), items)
        self.assertEqual(sessions.session.requests, [])
        for item in items[:3]:
            original = self.service.store.get("preflight-queued-" + item.id)
            self.assertEqual(original.status, "queued")
            self.assertIsNone(original.notification_batch_id)

    def test_automatic_drain_retains_native_uncertainty_without_replay(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        with patch.object(
            self.service.store,
            "record_notification_native_start",
            side_effect=RuntimeError("Receipt storage failed"),
        ):
            self.assertTrue(asyncio.run(handler.try_drain(runtime, items[0], project)))
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )
        self.assertEqual(coordinator.queue.get("thread-a"), items[3:])
        original = self.service.store.get("preflight-queued-" + items[0].id)
        batch = self.service.store.get_notification_batch(
            original.notification_batch_id
        )
        self.assertEqual(batch.phase, "native_outcome_unknown")
        self.assertEqual(batch.plan.members, tuple(items[:3]))
        with self.assertRaises(ValueError):
            coordinator.recover_prepared(
                batch.id, claim_id=batch.claim_id, current=lambda: True
            )

    def test_mutated_policy_cannot_rewrite_the_captured_admission_limits(self):
        coordinator, sessions, runtime, project, items, handler = (
            self.automatic_drain_fixture()
        )
        policy = {"enabled": True, "max_members": 3, "max_context_bytes": 65536}

        def resolve_policy(actor, project):
            return policy

        handler.policy_for = resolve_policy
        authority = handler.authority_for

        def change_policy(item, project):
            policy["max_members"] = 2
            return authority(item, project)

        handler.authority_for = change_policy
        with self.assertRaises(ValueError):
            asyncio.run(handler.try_drain(runtime, items[0], project))
        self.assertEqual(coordinator.queue.get("thread-a"), items)
        self.assertEqual(sessions.session.requests, [])
