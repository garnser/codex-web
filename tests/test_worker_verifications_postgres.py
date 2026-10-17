"""Private-schema PostgreSQL authority/append races on real separate connections."""
import asyncio
import os
import threading
import unittest
from unittest.mock import patch

from codex_web.services.identity import IdentityService
from codex_web.storage.identity_state import IdentityStateStore
from tests import test_control_plane_broker as broker_fixtures
from tests import test_worker_verifications as verification_fixtures
from tests.test_recovery_scheduled_postgres import _PostgresFixture


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresWorkerVerificationTests(_PostgresFixture, verification_fixtures.WorkerVerificationTests):
    async def asyncSetUp(self):
        with patch.object(broker_fixtures,'SQLiteStateStore',self.state_store_factory):
            await super().asyncSetUp()

    def writer_identity(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        pool=ConnectionPool(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'],min_size=1,max_size=1,
            timeout=3,kwargs={'options':'-c search_path='+self._test_schema})
        self.addCleanup(pool.close)
        store=PostgresStateStore(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'],connection_pool=pool)
        self.addCleanup(store.close)
        return IdentityService(IdentityStateStore(store))

    async def test_separate_writer_revocation_committed_before_append_refuses(self):
        writer=self.writer_identity()
        membership=next(m for m in writer.store.load().memberships
            if m.identity_id==self.worker_actor.identity_id and m.workspace_id==self.admin.workspace_id)
        reached,release=threading.Event(),threading.Event()
        original=self.evidence.create_verification
        def paused(payload,*,actor,_commit=None):
            def commit(updater):
                reached.set()
                if not release.wait(5):
                    raise AssertionError('append barrier not released')
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=paused):
            task=asyncio.create_task(self.post())
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait,5))
                await asyncio.to_thread(writer.revoke_membership,membership.id,actor=self.admin)
            finally:
                release.set()
            status,_,body=await task
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_final_shared_identity_lock_serializes_late_revocation(self):
        writer=self.writer_identity()
        membership=next(m for m in writer.store.load().memberships
            if m.identity_id==self.worker_actor.identity_id and m.workspace_id==self.admin.workspace_id)
        reached,release,writer_started=threading.Event(),threading.Event(),threading.Event()
        original=self.evidence.create_verification
        def paused(payload,*,actor,_commit=None):
            def commit(updater):
                def locked(state):
                    reached.set()
                    if not release.wait(5):
                        raise AssertionError('locked append barrier not released')
                    return updater(state)
                return _commit(locked)
            return original(payload,actor=actor,_commit=commit)
        def revoke():
            writer_started.set()
            return writer.revoke_membership(membership.id,actor=self.admin)
        with patch.object(self.evidence,'create_verification',side_effect=paused):
            request=asyncio.create_task(self.post())
            mutation=None
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait,5))
                mutation=asyncio.create_task(asyncio.to_thread(revoke))
                self.assertTrue(await asyncio.to_thread(writer_started.wait,5))
                await asyncio.sleep(.05)
                self.assertFalse(mutation.done(),'separate writer crossed final shared transaction lock')
            finally:
                release.set()
                status,_,body=await request
                if mutation is not None:
                    await mutation
        self.assertEqual(status,200,body)
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        self.assertEqual(self.recovery.store.load(),self.original_state)
        # The later revocation is current for subsequent access, not overwritten.
        status,_,_=await self.post()
        self.assertEqual(status,403)
