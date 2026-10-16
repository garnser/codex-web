"""Manual HTTP work shares actual scheduled executor custody and CAS barrier."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from tests import test_recovery_operator_api as api
from codex_web.services.recovery_executor import RecoveryExecutorBusy


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
            with patch.object(destination, 'put', controlled):
                work = asyncio.create_task(client.post('/api/recovery/backups'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 1)
                    status = await asyncio.wait_for(client.get('/api/recovery/status'), 2)
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertFalse(work.done())
                    attempt = await client.post(self.url(), json=self.body())
                    self.assertLess(attempt.status_code, 500, attempt.text)
                    if attempt.status_code == 200:
                        self.assertNotEqual(attempt.json()['item']['status'], 'succeeded')
                    self.assertFalse(work.done(), 'busy refusal waited for the manual writer')
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 1)
                    self.assertFalse(self.recovery.store.load().reconciliations)
                finally:
                    release.set()
                    response = await work
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(self.recovery.store.load().backups), 1)
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
            entered, release, controlled = self.gate(destination.get)
            with patch.object(destination, 'get', controlled):
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
