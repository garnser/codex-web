"""Manual HTTP work shares actual scheduled executor custody and CAS barrier."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from tests import test_recovery_operator_api as api
from codex_web.services.recovery_executor import RecoveryExecutorBusy
from codex_web.services.recovery import RecoveryConflictError


class ManualRecoveryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api.RecoveryOperatorApiTests.asyncSetUp
    async def asyncTearDown(self):
        try:
            await self.recovery.stop()
        finally:
            await api.RecoveryOperatorApiTests.asyncTearDown(self)
    client = api.RecoveryOperatorApiTests.client
    url = api.RecoveryOperatorApiTests.url
    body = api.RecoveryOperatorApiTests.body

    def gate(self, original):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        loop_thread = threading.get_ident()
        def controlled(*args, **kwargs):
            if threading.get_ident() == loop_thread:
                raise AssertionError('manual Recovery I/O blocked event loop')
            entered.set()
            if not release.wait(5):
                raise AssertionError('fixture gate was not released')
            return original(*args, **kwargs)
        return entered, release, controlled

    async def test_manual_backup_responsive_and_busy_reconciliation_cannot_commit(self):
        self.recovery.start()
        async with self.client() as client:
            destination = self.recovery.destinations['local']
            entered, release, controlled = self.gate(destination.put)
            failures = []
            original_reconcile = self.operator.provider.reconcile.reconcile
            def observed(*args, **kwargs):
                try:
                    return original_reconcile(*args, **kwargs)
                except (RecoveryConflictError, RecoveryExecutorBusy) as exc:
                    failures.append(str(exc))
                    raise
            with patch.object(destination, 'put', controlled), \
                 patch.object(self.operator.provider.reconcile, 'reconcile', side_effect=observed) as engine:
                work = asyncio.create_task(client.post('/api/recovery/backups'))
                reconcile = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 1)
                    status = await asyncio.wait_for(client.get('/api/recovery/status'), 2)
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertFalse(work.done())
                    reconcile = asyncio.create_task(client.post(self.url(), json=self.body()))
                    async def queued():
                        while self.recovery._scheduled_executor.status()['waiting_or_running'] != 2:
                            if reconcile.done():
                                self.fail('reconciliation ended before actual queued custody')
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(queued(), 2)
                    status = await asyncio.wait_for(client.get('/api/recovery/status'), 2)
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertFalse(work.done())
                    self.assertFalse(reconcile.done())
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 2)
                    self.assertFalse(self.recovery.store.load().reconciliations)
                finally:
                    release.set()
                    replies = await asyncio.gather(work, *([reconcile] if reconcile is not None else []),
                        return_exceptions=True)
                response = replies[0]
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(replies), 2)
            attempt = replies[1]
            self.assertLess(attempt.status_code, 500, attempt.text)
            if attempt.status_code == 200:
                self.assertNotEqual(attempt.json()['item']['status'], 'succeeded', attempt.text)
            self.assertFalse(self.recovery.store.load().reconciliations)
            self.assertEqual(self.recovery.store.load().schema_version, '1.2')
            self.assertEqual(len(self.recovery.store.load().backups), 1)
            self.assertEqual(engine.call_count, 1)
            self.assertEqual(len(failures), 1)
            self.assertIn(failures[0], (
                'Recovery activation/state snapshot changed',
                # The terminal manual future can remain owned until its HTTP
                # observer retires it. That conservative refusal is also valid.
                'other actual local Recovery work is not drained'))
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)

    async def test_manual_retention_uses_unchanged_policy_and_deletes_old_backup(self):
        async with self.client() as client:
            policy = self.recovery.policy()
            self.recovery.configure(policy.model_copy(update={'retention_count': 1}),
                actor=self.actor, expected_fingerprint=policy.fingerprint())
            first = await client.post('/api/recovery/backups')
            self.assertEqual(first.status_code, 200, first.text)
            self.clock.value += 1
            second = await client.post('/api/recovery/backups')
            self.assertEqual(second.status_code, 200, second.text)
            state = self.recovery.store.load()
            self.assertEqual(set(state.backups), {second.json()['item']['id']})
            destination = self.recovery.destinations['local']
            with self.assertRaises(FileNotFoundError):
                destination.get(first.json()['item']['destination_ref'])
            self.assertTrue(destination.get(second.json()['item']['destination_ref']))

    async def test_manual_retention_delete_failure_retains_manifests_and_actual_bytes(self):
        async with self.client() as client:
            policy = self.recovery.policy()
            self.recovery.configure(policy.model_copy(update={'retention_count': 1}),
                actor=self.actor, expected_fingerprint=policy.fingerprint())
            first = await client.post('/api/recovery/backups')
            self.assertEqual(first.status_code, 200, first.text)
            old = self.recovery.store.load().backups[first.json()['item']['id']]
            destination = self.recovery.destinations['local']
            original_bytes = destination.get(old.destination_ref)
            self.clock.value += 1
            with patch.object(destination, 'put', wraps=destination.put) as put, \
                 patch.object(destination, 'delete', side_effect=OSError('retention deletion unavailable')) as delete:
                with self.assertRaisesRegex(OSError, '^retention deletion unavailable$'):
                    await client.post('/api/recovery/backups')
                self.assertEqual(put.call_count, 1)
                self.assertEqual(delete.call_count, 1)
            state = self.recovery.store.load()
            self.assertEqual(len(state.backups), 2)
            self.assertEqual(state.backups[old.id], old)
            self.assertEqual(destination.get(old.destination_ref), original_bytes)
            self.assertTrue(all(destination.get(item.destination_ref) for item in state.backups.values()))
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)

    async def test_cancelled_manual_observer_retains_actual_writer_until_terminal(self):
        self.recovery.start()
        async with self.client() as client:
            destination = self.recovery.destinations['local']
            entered, release, controlled = self.gate(destination.put)
            with patch.object(destination, 'put', controlled):
                work = asyncio.create_task(client.post('/api/recovery/backups'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    work.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 1)
                    with self.assertRaises(RecoveryExecutorBusy):
                        with self.recovery._scheduled_executor.quiescence():
                            self.fail('active writer admitted as quiescent')
                finally:
                    release.set()
                    await asyncio.gather(work, return_exceptions=True)
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)
            self.assertEqual(len(self.recovery.store.load().backups), 1)

    async def test_quiescence_barrier_refuses_manual_submission_without_starting_work(self):
        self.recovery.start()
        async with self.client() as client:
            before = self.recovery.store.load()
            with self.recovery._scheduled_executor.quiescence():
                response = await client.post('/api/recovery/backups')
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 0)
            self.assertEqual(self.recovery.store.load(), before)
            self.assertFalse(self.recovery._scheduled_executor.status()['quiescence_guard'])

    async def test_shutdown_waits_manual_writer_and_refuses_late_submission(self):
        self.recovery.start()
        async with self.client() as client:
            destination = self.recovery.destinations['local']
            entered, release, controlled = self.gate(destination.put)
            with patch.object(destination, 'put', controlled):
                work = asyncio.create_task(client.post('/api/recovery/backups'))
                stopping = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    stopping = asyncio.create_task(self.recovery.stop())
                    await asyncio.sleep(0)
                    self.assertFalse(stopping.done())
                    late = await client.post('/api/recovery/backups')
                    self.assertEqual(late.status_code, 409, late.text)
                finally:
                    release.set()
                    await work
                    if stopping is not None:
                        await stopping
            self.assertFalse(self.recovery._scheduled_executor.status()['accepting'])
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)

    async def test_credential_rotation_during_put_refuses_registration_and_cleans_candidate(self):
        self.recovery.start()
        async with self.client() as client:
            before = self.recovery.store.load()
            destination = self.recovery.destinations['local']
            entered, release, controlled = self.gate(destination.put)
            with patch.object(destination, 'put', controlled):
                work = asyncio.create_task(client.post('/api/recovery/backups'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.identity.refresh_session(self.credentials.session_id, self.credentials.refresh_token)
                finally:
                    release.set()
                    response = await work
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(self.recovery.store.load(), before)
            self.assertEqual(list(destination.directory.iterdir()), [])
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)

    async def test_manual_verification_io_is_responsive_and_tracked(self):
        self.recovery.start()
        async with self.client() as client:
            backup = await client.post('/api/recovery/backups')
            self.assertEqual(backup.status_code, 200, backup.text)
            identifier = backup.json()['item']['id']
            destination = self.recovery.destinations['local']
            entered, release, controlled = self.gate(destination.get_bounded)
            with patch.object(destination, 'get_bounded', controlled):
                work = asyncio.create_task(client.post('/api/recovery/backups/' + identifier + '/verify'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 1)
                    status = await asyncio.wait_for(client.get('/api/recovery/status'), 2)
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertFalse(work.done())
                finally:
                    release.set()
                    response = await work
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)
