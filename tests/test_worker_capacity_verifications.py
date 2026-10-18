"""Existing broker Verification composition, synthetic metadata only.

No actual cgroup is claimed by this fixture. Production measurement is an
independent operator prerequisite; this exercises typed admission and custody.
"""
import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.artifact_evidence import EvidenceCreate, VerificationCreate
from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest
from codex_web.models import Project
from codex_web.resources import ResourceCreate
from codex_web.services.configuration import ConfigurationService
from codex_web.services.recovery_preservation_configuration import install_preservation_configuration, preservation_configuration
from codex_web.services.recovery_preservation_provider import RecoveryPreservationProvider
from codex_web.services.recovery import RecoveryService
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.storage_preservation import canonical
from codex_web.recovery_preservation import CAPACITY_CONTRACT
from tests import test_worker_verifications as verification_fixture
from tests.test_recovery_scheduled_postgres import _PostgresFixture
from tests import test_control_plane_broker as broker_fixtures


class CapacityWorkerVerificationTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(verification_fixture.WorkerVerificationTests._assignment)
    _publish_worker_authority = verification_fixture.WorkerVerificationTests._publish_worker_authority
    _request = verification_fixture.WorkerVerificationTests._request
    _publish_profile = verification_fixture.WorkerVerificationTests._publish_profile
    _stop_services = verification_fixture.WorkerVerificationTests._stop_services
    post = verification_fixture.WorkerVerificationTests.post

    def verification_resources(self, root):
        self.database = self.resources.create(ResourceCreate(resource_type='database',
            name='Private fixture whole StateStore'),actor=self.admin)
        return (self.target.id,self.database.id)

    async def asyncSetUp(self):
        await verification_fixture.WorkerVerificationTests.asyncSetUp(self)
        root=Path(self.temp.name)
        self.resources.bind_project(project=Project(id='project-a',name='Fixture',path=str(root/'repo')),
            resource_id=self.database.id,actor=self.admin)
        self.configuration=ConfigurationService(ConfigurationRegistryStore(self.sqlite))
        install_preservation_configuration(self.configuration)
        for key,value in {'resource_id':self.database.id,'memory_bytes':512*1024**2,
                'disk_bytes':1024**3,'wall_seconds':60}.items():
            draft=self.configuration.create_draft(ConfigurationDraftCreate(key='recovery.preservation.'+key,
                scope_type='deployment',value=value,actor=self.admin.identity_id))
            self.configuration.publish(draft.id,ConfigurationPublishRequest(actor=self.admin.identity_id))
        self.provider=RecoveryPreservationProvider(self.recovery,self.configuration,self.resources,
            lambda identity:self.admin)
        self.provider_registry=self.intents.execution.registry
        self.provider_registry.register(self.provider)
        self.binding=self.provider_registry.bind(ActionProviderBindingCreate(provider_type='recovery-storage',
            provider_instance='canonical-state-preservation',resource_ids=(self.database.id,)),
            actor=self.admin,resources=self.resources)
        config,pins=preservation_configuration(self.configuration)
        self.capacity_metadata={'contract':CAPACITY_CONTRACT,'resource_id':self.database.id,
            'recovery_executor_owner_id':self.recovery._scheduled_admission.owner_id,
            'memory_enforcement':'kernel-cgroup-aggregate','memory_max_bytes':config['memory_bytes'],
            'memory_swap_max_bytes':0,'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest(),
            'memory_bytes':config['memory_bytes'],'disk_bytes':config['disk_bytes'],
            'wall_seconds':config['wall_seconds'],'expires_at':time.time()+300,
            'database_directory':str(root),'database_reservation_directory':str(root)}
        self.original=self.capacity_evidence()
        self.request={'work_item_ref':self.ref,'evidence_ids':[self.original.id],
            'method':CAPACITY_CONTRACT,'result':'verified',
            'source':'sha256:'+hashlib.sha256(b'actual independent synthetic capacity review').hexdigest()}

    def capacity_evidence(self, metadata=None, **updates):
        body=dict(work_item_ref=self.ref,project_id='project-a',resource_ids=(self.database.id,),
            evidence_type='artifact_verification',result='pass',observed_at=time.time(),
            metadata=metadata or self.capacity_metadata)
        body.update(updates)
        return self.evidence.create_evidence(EvidenceCreate(**body),actor=self.admin)

    async def assert_denied(self, request=None):
        before=self.evidence.store.load().verifications
        status,_,body=await self.post(request)
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,before)
        self.assertEqual(self.recovery.store.load(),self.original_state)
        return body

    async def test_capacity_positive_original_worker_and_lost_response_readback(self):
        original=self.evidence.create_verification
        def lose(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError('Fixture lost response after actual append')
        with patch.object(self.evidence,'create_verification',side_effect=lose):
            self.assertEqual((await self.post())[0],500)
        persisted=self.evidence.store.load().verifications
        self.assertEqual(len(persisted),1)
        record=persisted[0]
        self.assertEqual(record.method,CAPACITY_CONTRACT)
        self.assertTrue(record.independent)
        self.assertEqual(record.verifier_identity_id,self.worker_actor.identity_id)
        self.assertEqual(record.evidence_ids,(self.original.id,))
        for _ in range(2):
            status,_,body=await self._request('GET','/api/verifications?work_item_ref='+quote(self.ref,safe='')+
                '&source='+quote(self.request['source'],safe=''))
            self.assertEqual(status,200,body)
            self.assertEqual(body['outcome'],'unique')
            self.assertEqual(body['items'][0]['id'],record.id)
        self.assertEqual(self.evidence.store.load().verifications,persisted)
        self.assertEqual(self.evidence.get_evidence(self.original.id,actor=self.admin),self.original)
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def test_only_two_supported_methods_no_independence_or_identity_override(self):
        for change in ({'method':'arbitrary-verification'},{'independent':True},
                {'verifier_identity_id':self.admin.identity_id}):
            await self.assert_denied(dict(self.request,**change))

    async def test_capacity_mismatch_stale_or_insufficient_kernel_and_path_refuse(self):
        for change in ({'recovery_executor_owner_id':'historical-owner'},
                {'configuration_sha256':'0'*64},{'memory_enforcement':'RLIMIT_AS'},
                {'memory_max_bytes':1024**3},{'memory_swap_max_bytes':1},
                {'disk_bytes':1},{'wall_seconds':1},{'expires_at':time.time()+1},
                {'database_directory':'relative'},{'database_reservation_directory':'relative'}):
            proof=self.capacity_evidence(dict(self.capacity_metadata,**change))
            await self.assert_denied(dict(self.request,evidence_ids=[proof.id]))
        for stamp in (time.time()-61,time.time()+10):
            proof=self.capacity_evidence(observed_at=stamp)
            await self.assert_denied(dict(self.request,evidence_ids=[proof.id]))

    async def test_current_runtime_restart_refuses_old_capacity_owner(self):
        replacement=RecoveryService(self.recovery.store,state_store=self.sqlite,crypto=self.recovery.crypto,
            evidence=self.evidence,scheduler=self.scheduler,service_actor=self.admin)
        self.addAsyncCleanup(replacement.stop)
        self.assertNotEqual(replacement._scheduled_admission.owner_id,self.capacity_metadata['recovery_executor_owner_id'])
        self.provider_registry.register(RecoveryPreservationProvider(replacement,self.configuration,
            self.resources,lambda identity:self.admin))
        await self.assert_denied()

    async def test_binding_disabled_at_append_refuses(self):
        original=self.evidence.create_verification
        def disable(payload,*,actor,_commit=None):
            def commit(updater):
                self.provider_registry.set_enabled(self.binding.id,False,actor=self.admin)
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=disable):
            await self.assert_denied()

    def change_config(self, service=None):
        service=service or self.configuration
        draft=service.create_draft(ConfigurationDraftCreate(key='recovery.preservation.disk_bytes',
            scope_type='deployment',value=2*1024**3,actor=self.admin.identity_id))
        return service.publish(draft.id,ConfigurationPublishRequest(actor=self.admin.identity_id))

    async def test_configuration_changed_at_append_refuses_old_pins(self):
        original=self.evidence.create_verification
        def change(payload,*,actor,_commit=None):
            def commit(updater):
                self.change_config()
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=change):
            await self.assert_denied()

    async def test_project_binding_missing_refuses_exact_database(self):
        def unbind(state):
            state.project_bindings=[b for b in state.project_bindings if b.resource_id!=self.database.id]
            return state
        self.resources.store.update(unbind)
        await self.assert_denied()

    async def test_worker_membership_revoked_refuses_capacity_append(self):
        membership=next(m for m in self.identity.store.load().memberships
            if m.identity_id==self.worker_actor.identity_id and m.workspace_id==self.worker_actor.workspace_id)
        self.identity.revoke_membership(membership.id,actor=self.admin)
        await self.assert_denied()

    async def test_repository_scope_cannot_borrow_project_database_authority(self):
        # Actual canonical assignment resource list changes; project association
        # alone never makes the proof consumable by a repository-only admission.
        def narrow(state):
            state.assignments=[a.model_copy(update={'resource_ids':(self.target.id,)})
                if a.id==self.assignment.id else a for a in state.assignments]
            return state
        self.workers.store.update(narrow)
        await self.assert_denied()

    async def test_evidence_invalidated_at_append_refuses(self):
        original=self.evidence.create_verification
        def invalidate(payload,*,actor,_commit=None):
            def commit(updater):
                self.evidence.invalidate_evidence(self.original.id,'Fixture revoked original capacity',actor=self.admin)
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=invalidate):
            await self.assert_denied()

    async def test_false_independence_linked_worker_artifact_and_foreign_project_refused(self):
        from codex_web.artifact_evidence import ArtifactCreate
        artifact=self.evidence.create_artifact(ArtifactCreate(artifact_type='report',name='Fixture B observation',
            work_item_ref=self.ref,project_id='project-a',resource_ids=(self.database.id,)),actor=self.worker_actor)
        proof=self.capacity_evidence(artifact_ids=(artifact.id,))
        await self.assert_denied(dict(self.request,evidence_ids=[proof.id]))
        proof=self.capacity_evidence(project_id='foreign-project')
        await self.assert_denied(dict(self.request,evidence_ids=[proof.id]))


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated PostgreSQL DSN required')
class PostgresCapacityWorkerVerificationTests(_PostgresFixture,CapacityWorkerVerificationTests):
    async def asyncSetUp(self):
        with patch.object(broker_fixtures,'SQLiteStateStore',self.state_store_factory):
            await super().asyncSetUp()

    def writer_configuration(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        pool=ConnectionPool(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'],min_size=1,max_size=1,
            timeout=3,kwargs={'options':'-c search_path='+self._test_schema})
        self.addCleanup(pool.close)
        store=PostgresStateStore(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'],connection_pool=pool)
        self.addCleanup(store.close)
        return ConfigurationService(ConfigurationRegistryStore(store))

    async def test_separate_configuration_writer_before_append_refuses(self):
        import asyncio
        import threading
        writer=self.writer_configuration()
        reached,release=threading.Event(),threading.Event()
        original=self.evidence.create_verification
        def pause(payload,*,actor,_commit=None):
            def commit(updater):
                reached.set()
                if not release.wait(5):
                    raise AssertionError('capacity append barrier not released')
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=pause):
            task=asyncio.create_task(self.post())
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait,5))
                await asyncio.to_thread(self.change_config,writer)
            finally:
                release.set()
                status,_,body=await task
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def test_late_configuration_writer_serializes_after_capacity_append(self):
        import asyncio
        import threading
        writer=self.writer_configuration()
        reached,release,started=threading.Event(),threading.Event(),threading.Event()
        original=self.evidence.create_verification
        def pause(payload,*,actor,_commit=None):
            def commit(updater):
                def locked(state):
                    result=updater(state)
                    reached.set()
                    if not release.wait(5):
                        raise AssertionError('locked capacity barrier not released')
                    return result
                return _commit(locked)
            return original(payload,actor=actor,_commit=commit)
        def change():
            started.set()
            return self.change_config(writer)
        with patch.object(self.evidence,'create_verification',side_effect=pause):
            task=asyncio.create_task(self.post())
            mutation=None
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait,5))
                mutation=asyncio.create_task(asyncio.to_thread(change))
                self.assertTrue(await asyncio.to_thread(started.wait,5))
                await asyncio.sleep(.05)
                self.assertFalse(mutation.done(),'configuration writer crossed shared capacity append locks')
            finally:
                release.set()
                status,_,body=await task
                if mutation is not None:
                    await mutation
        self.assertEqual(status,200,body)
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        await self.assert_denied()
