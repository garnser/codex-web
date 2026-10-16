"""Composed Scheduler -> Bus -> encrypted recovery, using controlled I/O gates."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from codex_web.crypto import KeyPurpose, ManagedKeyCreate
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_executor import RecoveryExecutorClosed
from codex_web.services.runtime_supervisor import RuntimeSupervisor
from codex_web.services.scheduler import SchedulerService
from tests import test_recovery as fixtures


class ScheduledRecoveryWorkTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledRecoveryTests.setUp
    tearDown = fixtures.ScheduledRecoveryTests.tearDown
    asyncTearDown = fixtures.ScheduledRecoveryTests.asyncTearDown

    def gate(self, original):
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        self.addCleanup(release.set)

        def controlled(*args, **kwargs):
            entered.set()
            if threading.get_ident() == loop_thread:
                raise AssertionError("scheduled recovery I/O ran on the main event loop")
            if not release.wait(5):
                raise AssertionError("controlled recovery I/O was not released")
            return original(*args, **kwargs)

        return entered, release, controlled

    async def assert_responsive(self, work):
        if work.done():
            await work  # Report a synchronous-path failure before assertions.
        app = FastAPI()
        @app.get('/livez')
        async def livez():
            return {"ok": True}
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
            self.assertEqual((await client.get('/livez')).json(), {"ok": True})
        supervisor = RuntimeSupervisor(SimpleNamespace(state=SimpleNamespace()), SimpleNamespace())
        completed = asyncio.create_task(asyncio.to_thread(lambda: "independent validation complete"))
        supervisor.tasks.update({"scheduler": work, "independent-validation": completed})
        self.assertEqual(await asyncio.wait_for(completed, 2), "independent validation complete")
        self.assertTrue(supervisor.task_status()["independent-validation"]["done"])
        self.assertTrue(supervisor.task_status()["scheduler"]["running"])
        self.assertFalse(work.done())

    async def test_real_backup_allows_request_heartbeat_and_supervision_before_completion(self):
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.put)
        self.state.put('business', {'important': True})
        self.clock.value = 1061
        with patch.object(destination, 'put', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.assert_responsive(work)
                receipt = next(iter(self.service.store.load().scheduled_firings.values()))
                self.assertEqual(receipt.status, "running")
            finally:
                release.set()
                await work
        state = self.service.store.load()
        self.assertEqual(len(state.backups), 1)
        self.assertIn(receipt.effect_id, state.backups)
        self.assertEqual(state.scheduled_firings[receipt.event_id].status, "succeeded")
        verification = await asyncio.to_thread(self.service.verify_restore, receipt.effect_id, actor=self.actor)
        self.assertEqual(verification.status.value, "pass")

    async def test_real_verification_allows_loop_progress_and_uses_reserved_identity(self):
        self.service.create_backup(actor=self.actor)
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.get)
        self.clock.value = 1301
        with patch.object(destination, 'get', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.assert_responsive(work)
            finally:
                release.set()
                await work
        state = self.service.store.load()
        receipt = next(item for item in state.scheduled_firings.values()
                       if item.trigger_type == self.service.VERIFY_TRIGGER)
        self.assertEqual(state.verifications[receipt.effect_id].status.value, "pass")
        self.assertEqual(state.scheduled_firings[receipt.event_id].status, "succeeded")
        self.assertEqual(len(self.evidence.list_evidence(actor=self.actor)), 1)

    async def test_cancelled_observers_do_not_release_custody_or_duplicate_actual_backup(self):
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.put)
        self.clock.value = 1061
        with patch.object(destination, 'put', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            duplicate = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                receipt = next(iter(self.service.store.load().scheduled_firings.values()))
                event = self.bus.store.event(receipt.event_id)
                duplicate = asyncio.create_task(self.service._handle_schedule_event(event))
                await asyncio.sleep(0)
                work.cancel()
                duplicate.cancel()
                await asyncio.sleep(0)
                work.cancel()
                await asyncio.sleep(0)
                self.assertFalse(work.done())
                self.assertFalse(duplicate.done())
                self.assertEqual(self.service.store.load().scheduled_firings[receipt.event_id].status, "running")
            finally:
                release.set()
                outcomes = await asyncio.gather(work, *([duplicate] if duplicate is not None else []), return_exceptions=True)
        self.assertTrue(all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes))
        await self.service._handle_schedule_event(event)
        state = self.service.store.load()
        self.assertEqual(len(state.backups), 1)
        self.assertEqual(state.scheduled_firings[receipt.event_id].status, "succeeded")

    async def test_supervisor_shutdown_closes_admission_and_waits_for_actual_work(self):
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.put)
        supervisor = RuntimeSupervisor(SimpleNamespace(state=SimpleNamespace(recovery_service=self.service)), SimpleNamespace())
        self.clock.value = 1061
        with patch.object(destination, 'put', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            supervisor.tasks['scheduler'] = work
            stopping = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                receipt = next(iter(self.service.store.load().scheduled_firings.values()))
                stopping = asyncio.create_task(supervisor.stop())
                await asyncio.sleep(0)
                self.assertFalse(self.service._scheduled_executor.status()['accepting'])
                with self.assertRaises(RecoveryExecutorClosed):
                    await self.service._handle_schedule_event(self.bus.store.event(receipt.event_id))
                stopping.cancel()
                await asyncio.sleep(0)
                stopping.cancel()
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                self.assertFalse(work.done())
            finally:
                release.set()
                outcomes = await asyncio.gather(work, *([stopping] if stopping else []), return_exceptions=True)
        self.assertEqual(self.service._scheduled_executor.status()['inflight_work'], 0)
        self.assertIsNone(self.service._scheduled_executor._pool)
        self.assertEqual(self.service.store.load().scheduled_firings[receipt.event_id].status, 'succeeded')
        self.service.start()
        self.assertTrue(self.service._scheduled_executor.status()['accepting'])
        await self.service._handle_schedule_event(self.bus.store.event(receipt.event_id))
        self.assertEqual(len(self.service.store.load().backups), 1)

    async def test_changed_policy_during_actual_backup_prevents_manifest_and_records_unknown(self):
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.put)
        self.clock.value = 1061
        with patch.object(destination, 'put', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                receipt = next(iter(self.service.store.load().scheduled_firings.values()))
                self.service.configure(self.service.policy().model_copy(update={'retention_count': 4}), actor=self.actor)
            finally:
                release.set()
                with self.assertRaises(RecoveryConflictError):
                    await work
        state = self.service.store.load()
        self.assertFalse(state.backups)
        self.assertEqual(state.scheduled_firings[receipt.event_id].status, 'unknown')
        self.assertFalse(list(destination.directory.iterdir()))
        await self.service._handle_schedule_event(self.bus.store.event(receipt.event_id))
        self.assertFalse(self.service.store.load().backups)

    async def test_lost_destination_response_keeps_reserved_effect_and_never_replays(self):
        destination = self.service.destinations['local']
        original = destination.put
        calls = []
        def lost_response(*args):
            calls.append(args[0])
            original(*args)
            raise OSError('lost destination response')
        self.clock.value = 1061
        with patch.object(destination, 'put', lost_response):
            with self.assertRaises(OSError):
                await self.scheduler.run_due()
            receipt = next(iter(self.service.store.load().scheduled_firings.values()))
            await self.service._handle_schedule_event(self.bus.store.event(receipt.event_id))
        self.assertEqual(calls, [receipt.effect_id])
        self.assertEqual(len(list(destination.directory.iterdir())), 1)
        self.assertEqual(self.service.store.load().scheduled_firings[receipt.event_id].status, 'unknown')
        self.assertIn('scheduled_recovery_requires_reconciliation', self.service.health(actor=self.actor).blockers)

    async def test_key_retirement_during_backup_rejects_frozen_manifest(self):
        auxiliary = self.crypto.create_key(ManagedKeyCreate(purpose=KeyPurpose.APPLICATION_DATA, backend_type='local'), actor=self.actor)
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.put)
        self.clock.value = 1061
        with patch.object(destination, 'put', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.crypto.revoke_key(auxiliary.id, 'concurrent unused key retirement', actor=self.actor)
            finally:
                release.set()
                with self.assertRaises(RecoveryConflictError):
                    await work
        self.assertFalse(self.service.store.load().backups)
        self.assertFalse(list(destination.directory.iterdir()))
        self.assertEqual(next(iter(self.service.store.load().scheduled_firings.values())).status, 'unknown')

    async def test_lost_verification_evidence_response_never_publishes_twice(self):
        self.service.create_backup(actor=self.actor)
        original = self.evidence.create_evidence
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('lost evidence response')
        self.clock.value = 1301
        with patch.object(self.evidence, 'create_evidence', lost_response):
            with self.assertRaises(OSError):
                await self.scheduler.run_due()
            state = self.service.store.load()
            receipt = next(item for item in state.scheduled_firings.values()
                           if item.trigger_type == self.service.VERIFY_TRIGGER)
            await self.service._handle_schedule_event(self.bus.store.event(receipt.event_id))
        self.assertEqual(len(self.evidence.list_evidence(actor=self.actor)), 1)
        self.assertEqual(self.service.store.load().verifications[receipt.effect_id].evidence_id,
                         self.evidence.list_evidence(actor=self.actor)[0].id)
        self.assertEqual(receipt.status, 'succeeded')
        self.assertEqual(self.evidence.list_evidence(actor=self.actor)[0].metadata['verification_id'], receipt.effect_id)
        self.assertEqual(self.evidence.list_evidence(actor=self.actor)[0].metadata['canonical_event_id'], receipt.event_id)

    async def test_pausing_current_schedule_during_verification_prevents_evidence_and_success(self):
        self.service.create_backup(actor=self.actor)
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.get)
        self.clock.value = 1301
        with patch.object(destination, 'get', controlled):
            work = asyncio.create_task(self.scheduler.run_due())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                state = self.service.store.load()
                self.scheduler.pause(state.verification_schedule_id, actor_id=self.actor.identity_id)
            finally:
                release.set()
                with self.assertRaises(RecoveryConflictError):
                    await work
        self.assertFalse(self.evidence.list_evidence(actor=self.actor))
        state = self.service.store.load()
        self.assertFalse(state.verifications)
        receipt = next(item for item in state.scheduled_firings.values()
                       if item.trigger_type == self.service.VERIFY_TRIGGER)
        self.assertEqual(receipt.status, 'unknown')

    async def test_shutdown_drains_already_accepted_verification_behind_backup(self):
        self.service.create_backup(actor=self.actor)
        destination = self.service.destinations['local']
        entered, release, controlled = self.gate(destination.put)
        peer_scheduler = SchedulerService(self.scheduler.store, self.service.canonical_events,
                                         clock=self.clock, owner_id='second-scheduler')
        executor = self.service._scheduled_executor
        original_run = executor.run
        queued = asyncio.Event()
        async def observed_run(key, operation):
            if executor.status()['waiting_or_running']:
                queued.set()
            return await original_run(key, operation)
        self.clock.value = 1301
        with patch.object(destination, 'put', controlled), patch.object(executor, 'run', observed_run):
            backup = asyncio.create_task(self.scheduler.run_due(max_claims=1))
            verification = stopping = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                verification = asyncio.create_task(peer_scheduler.run_due(max_claims=1))
                await asyncio.wait_for(queued.wait(), 2)
                self.assertEqual(executor.status()['waiting_or_running'], 2)
                backup.cancel()
                verification.cancel()
                stopping = asyncio.create_task(self.service.stop())
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                self.assertFalse(verification.done())
                # Verification has an owned queued future, but cannot perform
                # effects while the backup still owns the sole worker thread.
                self.assertFalse(self.evidence.list_evidence(actor=self.actor))
            finally:
                release.set()
                outcomes = await asyncio.gather(backup, *([verification] if verification else []),
                                               *([stopping] if stopping else []), return_exceptions=True)
        state = self.service.store.load()
        self.assertEqual(len(state.scheduled_firings), 2)
        self.assertTrue(all(item.status == 'succeeded' for item in state.scheduled_firings.values()))
        self.assertEqual(len(self.evidence.list_evidence(actor=self.actor)), 1)
        self.assertEqual(len(state.verifications), 1)
        self.assertIsNone(executor._pool)
