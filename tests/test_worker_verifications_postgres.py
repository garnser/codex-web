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

    async def credential_session(self):
        """Real session authority/delegation path, with no process launch/use.

        Only synthetic credentials exist in this private fixture. The actual
        provider reads metadata; neither this test nor Verification resolves its
        value. Resource scanning/process supervision are outside this race test.
        """
        import time
        from pathlib import Path
        from codex_web.secret_backends import LocalFileSecretBackend
        from codex_web.secrets import SecretCreate
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
        from codex_web.services.codex_auth_delegation import CODEX_AUTH_PURPOSE, CodexAuthDelegationService
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
        from codex_web.services.secrets import SecretBroker
        from codex_web.storage.secret_state import SecretStateStore
        backend=LocalFileSecretBackend(Path(self.temp.name)/'synthetic-credentials')
        secrets=SecretBroker(SecretStateStore(self.sqlite),{'local':backend})
        secret=secrets.create(SecretCreate(name='Synthetic session credential',
            value='synthetic-fixture-not-a-real-token',provider='codex',purpose=CODEX_AUTH_PURPOSE,
            allowed_identity_ids=[self.worker_actor.identity_id],expires_at=time.time()+300),actor=self.admin)
        def bind(state):
            state.assignments=[a.model_copy(update={'secret_refs':(secret.id,)})
                if a.id==self.assignment.id else a for a in state.assignments]
            return state
        self.workers.store.update(bind)
        self.assignment=self.workers.assignment_for_execution(self.assignment.execution_id,actor=self.admin)
        self.current_assignment=self.assignment
        provider=CodexAuthDelegationService(secrets)
        local=LocalExecutionWorkerRuntime(self.workers,self.workspaces,None,worker=self.worker,
            worker_actor=self.worker_actor,control_actor=self.admin,codex_auth_delegation=provider)
        def never_launch(*args,**kwargs):
            raise AssertionError('This fixture must not launch a model process')
        session=AssignmentBoundAgentProcessSession(local,None,self.assignment.id,
            runtime_factory=never_launch,credential_provider=provider)
        self.addCleanup(session._supervision_executor.shutdown,wait=True)
        self.addCleanup(session._diagnostic_executor.shutdown,wait=True)
        session.fence=self.assignment.fence
        session.delegation=provider.issue(self.assignment,worker_id=self.worker.id,
            fence=self.assignment.fence,actor=self.worker_actor)
        self.broker.validator=session._validate_current_authority
        self.intents.execution.secret_broker=secrets
        self.assertEqual(session._validate_current_authority().id,self.assignment.id)
        return secrets,secret,provider

    def credential_writer(self,secrets):
        from codex_web.services.secrets import SecretBroker
        from codex_web.storage.secret_state import SecretStateStore
        # Reuse this test's private schema via a truly separate connection/store.
        writer=self.writer_identity()
        return SecretBroker(SecretStateStore(writer.store.store),secrets.backends)

    def change_credential(self,writer,secret,operation):
        from codex_web.secrets import SecretRotate
        if operation=='revoke':
            return writer.revoke(secret.id,reason='Synthetic credential revocation',actor=self.admin)
        self.assertEqual(operation,'rotate')
        return writer.rotate(secret.id,SecretRotate(value='synthetic-fixture-rotated-not-a-real-token'),actor=self.admin)

    async def credential_before_append(self,operation):
        secrets,secret,_=await self.credential_session()
        writer=self.credential_writer(secrets)
        reached,release=threading.Event(),threading.Event()
        original=self.evidence.create_verification
        def paused(payload,*,actor,_commit=None):
            def commit(updater):
                reached.set()
                if not release.wait(5):
                    raise AssertionError('credential pre-append barrier not released')
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=paused):
            request=asyncio.create_task(self.post())
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait,5))
                await asyncio.to_thread(self.change_credential,writer,secret,operation)
            finally:
                release.set()
                status,_,body=await request
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def credential_after_final_read(self,operation):
        secrets,secret,provider=await self.credential_session()
        writer=self.credential_writer(secrets)
        reached,release,started=threading.Event(),threading.Event(),threading.Event()
        original_create=self.evidence.create_verification
        original_validate=provider.validate_current
        local=threading.local()
        def mark_final(payload,*,actor,_commit=None):
            def commit(updater):
                def mark(state):
                    result=updater(state)
                    local.final_read=True
                    return result
                return _commit(mark)
            return original_create(payload,actor=actor,_commit=commit)
        def validate(*args,**kwargs):
            original_validate(*args,**kwargs)  # actual usable metadata/rotation/expiry check
            if getattr(local,'final_read',False):
                reached.set()
                if not release.wait(5):
                    raise AssertionError('final credential-read barrier not released')
        def change():
            started.set()
            return self.change_credential(writer,secret,operation)
        with patch.object(self.evidence,'create_verification',side_effect=mark_final), \
                patch.object(provider,'validate_current',side_effect=validate):
            request=asyncio.create_task(self.post())
            mutation=None
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait,5))
                mutation=asyncio.create_task(asyncio.to_thread(change))
                self.assertTrue(await asyncio.to_thread(started.wait,5))
                await asyncio.sleep(.05)
                self.assertFalse(mutation.done(),'credential metadata writer crossed the final append lock')
            finally:
                release.set()
                status,_,body=await request
                if mutation is not None:
                    await mutation
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['verifier_identity_id'],self.worker_actor.identity_id)
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        status,_,_=await self.post()
        self.assertEqual(status,403)
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def test_credential_revoked_before_append_refuses(self):
        await self.credential_before_append('revoke')

    async def test_credential_rotated_before_append_refuses(self):
        await self.credential_before_append('rotate')

    async def test_credential_revoke_after_final_read_serializes(self):
        await self.credential_after_final_read('revoke')

    async def test_credential_rotate_after_final_read_serializes(self):
        await self.credential_after_final_read('rotate')

    async def test_foreign_credential_readset_refused(self):
        secrets,_,_=await self.credential_session()
        self.intents.execution.secret_broker=self.credential_writer(secrets)
        status,_,body=await self.post()
        self.assertEqual(status,403,body)
        self.assertIn('credential validator read set',body['error']['message'])
        self.assertEqual(self.evidence.store.load().verifications,[])
