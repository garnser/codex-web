"""Concurrent RPC validation must not hold the disk lock over metadata I/O."""
import asyncio
import threading
import unittest

from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionStaleError
from tests import test_runtime_supervision_custody as fixture


class DiskValidationMetadataConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    _create_assignment = fixture.SupervisionCustodyTests._create_assignment
    _session = fixture.SupervisionCustodyTests._session
    asyncSetUp = fixture.SupervisionCustodyTests.asyncSetUp
    asyncTearDown = fixture.SupervisionCustodyTests.asyncTearDown
    session = fixture.SupervisionCustodyTests.session

    async def test_slow_rpc_metadata_does_not_block_watchdog_validation(self):
        session, _ = await self.session()
        session.last_disk_validation_monotonic = None
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def metadata(assignment):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('fixture metadata was not released')
            return 7

        self.backend.disk_bytes = 11
        self.local_worker.readonly_disk_bytes = metadata
        rpc = asyncio.create_task(asyncio.to_thread(session.validate_current))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            result = await session._supervise(
                'validation', session.validate_current, seconds=.3)
            self.assertEqual(result.id, session.assignment_id)
            self.assertEqual(session.last_disk_bytes, 18)
            self.assertFalse(rpc.done())
        finally:
            release.set()
            await rpc

    async def test_old_metadata_cannot_replace_a_new_over_limit_measurement(self):
        session, _ = await self.session()
        session.last_disk_validation_monotonic = None
        entered, release = threading.Event(), threading.Event()
        calls = []

        def metadata(assignment):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('fixture metadata was not released')
                return 0
            return assignment.limits.disk_bytes + 1

        self.local_worker.readonly_disk_bytes = metadata
        rpc = asyncio.create_task(asyncio.to_thread(session.validate_current))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            with self.assertRaisesRegex(AssignmentBoundAgentProcessSessionStaleError, 'disk_bytes'):
                await session._supervise('validation', session.validate_current, seconds=.3)
        finally:
            release.set()
            with self.assertRaisesRegex(AssignmentBoundAgentProcessSessionStaleError, 'disk_bytes'):
                await rpc

    async def check_expired_observation(self, phase):
        session, _ = await self.session()
        session.last_disk_validation_monotonic = None
        old_bytes = session.last_disk_bytes

        def metadata(assignment):
            if phase == 'metadata':
                self.delegation.now += session.disk_validation_interval_seconds
            return 7

        def disk(*args):
            if phase == 'filesystem':
                self.delegation.now += session.disk_validation_interval_seconds
            return 11

        self.local_worker.readonly_disk_bytes = metadata
        self.backend.execution_disk_usage = disk
        with self.assertRaisesRegex(TimeoutError, 'observation expired'):
            await asyncio.to_thread(session.validate_current)
        self.assertEqual(session.last_disk_bytes, old_bytes)
        self.assertIsNone(session.last_disk_validation_monotonic)

    async def test_expired_metadata_never_publishes_a_complete_cache(self):
        await self.check_expired_observation('metadata')

    async def test_expired_filesystem_never_publishes_a_complete_cache(self):
        await self.check_expired_observation('filesystem')
