import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from codex_web.compatibility import ContractCompatibilityError, ContractSpec
from codex_web.scheduler import ScheduleStatus
from codex_web.services.identity import AuthorizationError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_executor import RecoveryExecutorBusy
from codex_web.services.recovery_schedules import RecoveryScheduleAdmission
from codex_web.storage.recovery import RecoveryStore
from tests import test_recovery as fixtures


class RecoveryScheduleAdmissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledRecoveryTests.setUp
    tearDown = fixtures.ScheduledRecoveryTests.tearDown
    asyncTearDown = fixtures.ScheduledRecoveryTests.asyncTearDown

    async def asyncSetUp(self):
        # Real Scheduler -> durable Bus, without invoking effects while testing
        # admission. No fabricated successful inbox acknowledgement.
        self.service._unsubscribe()
        self.events = []
        self.bus.subscribe(self.events.append)
        self.admission = RecoveryScheduleAdmission(self.service, "owner-a")
        self.clock.value = 1061
        await self.scheduler.run_due()
        self.event = self.events[-1]

    async def next_event(self):
        self.clock.value += 60
        await self.scheduler.run_due()
        return self.events[-1]

    async def stored_variant(self, *, source=None, tenant=None, workspace=None, **changes):
        payload = {**self.event.payload, **changes}
        delivery = await self.service.canonical_events.ingest(
            event_type=self.event.event_type,
            source=self.event.source if source is None else source,
            tenant_id=self.event.tenant_id if tenant is None else tenant,
            workspace_id=self.event.workspace_id if workspace is None else workspace,
            occurred_at=self.event.occurred_at,
            payload=payload, idempotency_key=f"variant-{len(self.events)}",
        )
        return delivery.event

    async def test_firing_reserves_identity_and_duplicate_cannot_reexecute_after_restart(self):
        firing = self.admission.admit(self.event)
        self.assertEqual(firing.status, "running")
        self.assertTrue(firing.effect_id.startswith("backup-"))
        self.assertEqual(firing.actor_id, self.actor.identity_id)
        self.assertEqual(firing.policy_fingerprint, self.service.policy().fingerprint())
        self.assertIsNone(self.admission.admit(self.event))
        replacement = RecoveryScheduleAdmission(self.service, "owner-b")
        self.assertIsNone(replacement.admit(self.event))
        next_event = await self.next_event()
        with self.assertRaises(RecoveryExecutorBusy):
            replacement.admit(next_event)
        with self.assertRaises(RecoveryConflictError):
            replacement.finish(firing, succeeded=True)
        self.admission.finish(firing, succeeded=True)
        self.assertIsNone(replacement.admit(self.event))
        self.assertIsNotNone(replacement.admit(next_event))

    async def test_terminal_unknown_never_replays_but_a_future_occurrence_can_run(self):
        firing = self.admission.admit(self.event)
        self.admission.finish(firing, succeeded=False, error_code="BackupDestinationError")
        state = self.service.store.load()
        receipt = state.scheduled_firings[self.event.event_id]
        self.assertEqual(receipt.status, "unknown")
        self.assertIsNotNone(receipt.finished_at)
        self.assertEqual(receipt.effect_id, firing.effect_id)
        self.assertIsNone(self.admission.admit(self.event))
        with self.assertRaises(RecoveryConflictError):
            self.admission.finish(firing, succeeded=True)
        self.assertIsNotNone(self.admission.admit(await self.next_event()))

    async def test_actual_concurrent_admission_has_one_winner(self):
        gate = threading.Barrier(2)

        def admit(owner):
            gate.wait(timeout=5)
            return RecoveryScheduleAdmission(self.service, owner).admit(self.event)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = await asyncio.gather(*[
                asyncio.wrap_future(pool.submit(admit, owner)) for owner in ("one", "two")
            ])
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.service.store.load().scheduled_firings), 1)

    async def test_unstored_and_foreign_or_inexact_canonical_events_are_denied(self):
        self.assertIsNone(self.admission.admit(self.event.model_copy(update={"event_id": "not-stored"})))
        for changes in [
            {"source": "another-source"}, {"tenant": "foreign"}, {"workspace": "foreign"},
            {"schedule_id": "foreign"}, {"payload": {}},
            {"payload": {"policy_fingerprint": "old"}}, {"scheduled_for": float("nan")},
            {"scheduled_for": True}, {"scheduled_for": 999}, {"scheduled_for": 2000},
            {"misfire_policy": "catch_up"}, {"trigger_type": "another-trigger"},
        ]:
            with self.subTest(changes=changes):
                self.assertIsNone(self.admission.admit(await self.stored_variant(**changes)))
        self.assertFalse(self.service.store.load().scheduled_firings)

    async def test_current_paused_schedule_policy_and_actor_authority_are_rechecked(self):
        def pause(state):
            item = state.schedules[self.event.payload["schedule_id"]]
            state.schedules[item.id] = item.model_copy(update={"status": ScheduleStatus.PAUSED})
            return state

        self.scheduler.store._update(pause)
        self.assertIsNone(self.admission.admit(self.event))
        self.service.configure(self.service.policy().model_copy(update={"retention_count": 4}), actor=self.actor)
        self.assertIsNone(self.admission.admit(self.event))
        # A persisted canonical event cannot confer authority on its consumer.
        self.service.service_actor = self.actor.model_copy(update={"roles": ()})
        with self.assertRaises(AuthorizationError):
            self.admission.admit(self.event)
        self.assertFalse(self.service.store.load().scheduled_firings)

    async def test_success_compaction_keeps_replay_barrier_and_never_evicts_uncertainty(self):
        first = self.admission.admit(self.event)
        self.admission.finish(first, succeeded=True)
        original = self.event
        # Fill with successful historical receipts. Normal terminal compaction
        # must preserve the current FIRE_ONCE schedule watermark.
        def fill(state):
            receipt = state.scheduled_firings[original.event_id]
            state.scheduled_firings = {
                str(index): receipt.model_copy(update={"event_id": str(index)}) for index in range(128)
            }
            return state

        self.service.store.update(fill)
        self.assertIsNone(self.admission.admit(original))
        next_event = await self.next_event()
        current = self.admission.admit(next_event)
        self.assertIsNotNone(current)
        self.assertEqual(len(self.service.store.load().scheduled_firings), 128)
        self.admission.finish(current, succeeded=False, error_code="uncertain")

        def uncertain(state):
            state.scheduled_firings = {key: item.model_copy(update={"status": "unknown"})
                                       for key, item in state.scheduled_firings.items()}
            return state

        self.service.store.update(uncertain)
        before = self.service.store.load()
        with self.assertRaises(RecoveryExecutorBusy):
            self.admission.admit(await self.next_event())
        self.assertEqual(self.service.store.load(), before)

    async def test_legacy_migration_and_snapshot_preserve_replay_barriers(self):
        firing = self.admission.admit(self.event)
        frozen = self.service._snapshot_documents()["recovery"]
        self.assertEqual(frozen["scheduled_firings"][firing.event_id], firing.model_dump(mode="json"))
        self.assertTrue(frozen["scheduled_watermarks"])
        self.assertEqual(self.service.store.load().scheduled_firings[firing.event_id], firing)
        for version in ("1.0", "1.1"):
            legacy = {"schema_version": version, "policy": self.service.policy().model_dump(mode="json")}
            upgraded = RecoveryStore._decode(legacy)
            self.assertEqual(upgraded.schema_version, "1.2")
            self.assertFalse(upgraded.scheduled_firings)
            self.assertEqual(upgraded.policy, self.service.policy())
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec("recovery-state", "1.1", ("1.1",)).require("1.2")

    async def test_verification_admission_pins_exact_existing_backup(self):
        backup = self.service.create_backup(actor=self.actor)
        self.clock.value = 1301
        await self.scheduler.run_due()
        verification_event = next(event for event in reversed(self.events)
                                  if event.payload.get("trigger_type") == self.service.VERIFY_TRIGGER)
        firing = self.admission.admit(verification_event)
        self.assertEqual(firing.backup_id, backup.id)
        self.assertTrue(firing.effect_id.startswith("restore-verification-"))

    async def test_legacy_schedule_missing_fingerprint_is_denied_and_explicit_configure_repairs_it(self):
        original_id = self.event.payload['schedule_id']
        def legacy(state):
            record = state.schedules[original_id]
            state.schedules[original_id] = record.model_copy(update={'payload': {}})
            return state
        self.scheduler.store._update(legacy)
        event = await self.next_event()
        self.assertEqual(event.payload['payload'], {})
        self.assertIsNone(self.admission.admit(event))
        self.service.configure(self.service.policy(), actor=self.actor)
        state = self.service.store.load()
        self.assertNotEqual(state.backup_schedule_id, original_id)
        self.assertEqual(self.scheduler.get(original_id).status, ScheduleStatus.CANCELLED)
        self.assertIsNone(self.admission.admit(event))
        current = self.admission.admit(await self.next_event())
        self.assertEqual(current.schedule_id, state.backup_schedule_id)
