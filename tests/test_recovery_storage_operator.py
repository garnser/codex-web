"""Real HTTP/ActionIntent/Artifact/KMS composition, private synthetic state only.

The same cases run on private PostgreSQL with pool1. No live application startup
or installed DSN is part of this fixture. Qualify only in the admitted sandbox.
"""
import asyncio
import hashlib
import json
import os
import threading
import time
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from codex_web.api.identity import CSRF_HEADER, SESSION_COOKIE, install_identity_middleware
from codex_web.api.recovery import build_recovery_router
from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, EvidenceType, VerificationCreate, VerificationResult
from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest, ConfigurationScope
from codex_web.identity import AuthenticationAssurance, HumanUserCreate, MembershipRole
from codex_web.recovery_preservation import CAPTURE, VERIFY, SOURCE, CAPACITY_CONTRACT, StoragePreservationRequest
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.security import ExecutionSecurityPolicy, FilesystemBoundaryPolicy
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.configuration import ConfigurationService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.recovery_operator import RecoveryOperatorActions
from codex_web.services.recovery_preservation_configuration import PREFIX, preservation_configuration
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.storage_preservation import canonical
from tests import test_action_intents as intent_fixture
from tests import test_recovery_action_intent as recovery_fixture
from tests.test_recovery_scheduled_postgres import _PostgresFixture


class StorageOperatorTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = recovery_fixture.RecoveryActionIntentTests.asyncTearDown

    async def asyncSetUp(self):
        await recovery_fixture.RecoveryActionIntentTests.asyncSetUp(self)
        self.root=Path(self.temp.name)
        (self.root/'content').mkdir()
        self.local_content=LocalArtifactContentStore(self.root/'content')
        registry=ArtifactContentRegistry();registry.register(self.local_content)
        self.artifacts.content=ArtifactContentService(registry)
        self.configuration=ConfigurationService(ConfigurationRegistryStore(self.sqlite))
        self.operator=RecoveryOperatorActions(self.recovery,self.service,self.worker_actor,configuration=self.configuration)
        self.database=self.resources.create(ResourceCreate(resource_type=ResourceType.DATABASE,
            name='Private fixture whole StateStore'),actor=self.actor)
        for name,value in {'resource_id':self.database.id,'memory_bytes':512*1024**2,
                           'disk_bytes':512*1024**2,'wall_seconds':60}.items():
            self.publish(name,value)
        self.credentials=self.identity.create_session(identity_id=self.actor.identity_id,
            scope=self.actor.tenant,assurance=AuthenticationAssurance.MFA)
        human,_=self.identity.create_human_user(HumanUserCreate(display_name='Independent capacity observer',
            roles=(MembershipRole.ADMIN,)),actor=self.actor)
        self.independent=self.identity.actor_for_identity(human.id,scope=self.actor.tenant)
        _,pins=preservation_configuration(self.configuration)
        database_directory=self.root
        if hasattr(self.sqlite,'dsn'):
            with self.sqlite._connection() as connection:
                database_directory=Path(connection.execute('SHOW data_directory').fetchone()[0])
        # Root supplies an admitted writable reservation filesystem when PG
        # data and client temp are separate mounts; the real mapping is checked.
        reservation_root=Path(os.environ.get('CODEX_WEB_TEST_PRESERVATION_RESERVATION_DIR',str(self.root)))
        self.reservation_directory=Path(tempfile.mkdtemp(prefix='storage-reservation-',dir=reservation_root))
        self.addCleanup(lambda:shutil.rmtree(self.reservation_directory,ignore_errors=True))
        evidence=self.artifacts.create_evidence(EvidenceCreate(evidence_type=EvidenceType.ARTIFACT_VERIFICATION,
            resource_ids=(self.database.id,),source='fixture-private-resource-admission',
            result=EvidenceResult.PASS,summary='Synthetic private sandbox capacity',metadata={
                'contract':CAPACITY_CONTRACT,'resource_id':self.database.id,
                'recovery_executor_owner_id':self.recovery._scheduled_admission.owner_id,
                'memory_enforcement':'kernel-cgroup-aggregate','memory_max_bytes':512*1024**2,
                'memory_swap_max_bytes':0,
                'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest(),
                'memory_bytes':512*1024**2,'disk_bytes':512*1024**2,'wall_seconds':60,
                'expires_at':time.time()+300,'database_directory':str(database_directory),
                'database_reservation_directory':str(self.reservation_directory)}),actor=self.actor)
        verification=self.artifacts.create_verification(VerificationCreate(evidence_ids=(evidence.id,),
            method='Private fixture kernel admission',result=VerificationResult.VERIFIED),actor=self.independent)
        self.assertTrue(verification.independent)
        self.payload=StoragePreservationRequest(operation_id='capture-fixture',resource_id=self.database.id,
            expected_policy_fingerprint=self.recovery.policy().fingerprint(),
            capacity_evidence_id=evidence.id,capacity_verification_id=verification.id,
            idempotency_key='capture-fixture',confirm_opaque_preservation=True)
        self.storage_binding=self.registry.bind(ActionProviderBindingCreate(
            provider_type='recovery-storage',provider_instance='canonical-state-preservation',
            resource_ids=(self.database.id,),security_policy=ExecutionSecurityPolicy(
                filesystem=FilesystemBoundaryPolicy(allowed_write_roots=(str(self.root),str(self.reservation_directory))))),
            actor=self.actor,resources=self.resources)
        self.original=self.recovery.store.load()
        self.insert_opaque()

    def publish(self,suffix,value):
        record=self.configuration.create_draft(ConfigurationDraftCreate(key=PREFIX+suffix,
            scope_type=ConfigurationScope.DEPLOYMENT,value=value,actor=self.actor.identity_id))
        return self.configuration.publish(record.id,ConfigurationPublishRequest(actor=self.actor.identity_id))

    def insert_opaque(self):
        table='codex_state_documents' if hasattr(self.sqlite,'dsn') else 'state_documents'
        mark='%s' if hasattr(self.sqlite,'dsn') else '?'
        self.opaque='{"retained_unknown":'+'x'*6886
        with self.sqlite._connection() as connection:
            connection.execute(f'INSERT INTO {table}(namespace,payload,updated_at) VALUES ({mark},{mark},{mark})',
                ('synthetic-malformed-usage',self.opaque,1.125))

    def client(self):
        app=FastAPI();install_identity_middleware(app,self.identity)
        app.include_router(build_recovery_router(self.recovery,actions=self.operator,identity=self.identity))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture',
            cookies={SESSION_COOKIE:self.credentials.session_token},headers={CSRF_HEADER:self.credentials.csrf_token})

    def body(self,action=CAPTURE,payload=None):
        return {'binding_id':self.storage_binding.id,'action':action,
                'request':(payload or self.payload).model_dump(mode='json')}

    def committed(self):
        return [e for e in self.artifacts.store.load().evidence if e.source==SOURCE]

    async def capture(self,client):
        response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
        self.assertEqual(response.status_code,200,response.text)
        item=response.json()['item'];self.assertEqual(item['status'],'succeeded',item.get('last_error'))
        return item

    def assert_original(self):
        self.assertEqual(self.recovery.store.load(),self.original)
        self.assertEqual(self.original.schema_version,'1.2')
        table='codex_state_documents' if hasattr(self.sqlite,'dsn') else 'state_documents'
        with self.sqlite._connection() as connection:
            row=connection.execute(f"SELECT payload,updated_at FROM {table} WHERE namespace='synthetic-malformed-usage'").fetchone()
        self.assertEqual(row,(self.opaque,1.125))
        self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'],0)

    async def test_capture_verify_same_backend_once_preserves_invalid_rows_without_health_pass(self):
        async with self.client() as client:
            item=await self.capture(client)
            again=await self.capture(client);self.assertEqual(again['id'],item['id'])
            receipt=await client.get('/api/recovery/storage-preservation/receipts/'+item['id'])
            self.assertEqual(receipt.status_code,200,receipt.text)
            report=receipt.json()['report']
            self.assertEqual(report['semantic_status'],'invalid')
            self.assertFalse(report['semantic_recovery_qualified'])
            artifact=receipt.json()['artifact_ids'][0]
            verify=self.payload.model_copy(update={'operation_id':'verify-fixture','idempotency_key':'verify-fixture','artifact_id':artifact})
            result=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,verify))
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['item']['status'],'succeeded',result.text)
            isolated=await client.get('/api/recovery/storage-preservation/receipts/'+result.json()['item']['id'])
            self.assertTrue(isolated.json()['report']['isolated_restore_verified'])
            self.assertEqual(isolated.json()['report']['raw_stream_sha256'],report['raw_stream_sha256'])
            self.assertEqual(len(self.committed()),2)
        self.assert_original()

    async def test_lost_result_settles_same_evidence_without_recapture(self):
        provider=self.operator.storage_provider
        async with self.client() as client:
            with patch.object(provider,'_result',side_effect=RuntimeError('fixture response lost')):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,200,response.text)
            item=response.json()['item'];self.assertNotEqual(item['status'],'succeeded')
            self.assertEqual(len(self.committed()),1,response.text)
            evidence=self.committed()[0]
            with patch.object(provider.operations,'execute',side_effect=AssertionError('recapture forbidden')):
                for _ in range(2):
                    observed=await client.post('/api/recovery/storage-preservation/operations/capture-fixture/intents/'+item['id']+'/observe')
                    self.assertEqual(observed.status_code,200,observed.text)
                    self.assertEqual(observed.json()['item']['status'],'succeeded',observed.text)
            self.assertEqual(self.committed(),[evidence])
        self.assert_original()

    async def test_current_primary_session_cannot_borrow_fixture_mfa(self):
        def demote(state):
            state.sessions=[s.model_copy(update={'assurance':AuthenticationAssurance.PRIMARY,'step_up_until':None})
                if s.id==self.credentials.session_id else s for s in state.sessions];return state
        self.identity.store.update(demote)
        async with self.client() as client:
            response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,403,response.text)
        self.assertFalse(self.committed());self.assert_original()

    async def test_missing_independent_capacity_and_foreign_resource_refuse(self):
        async with self.client() as client:
            for update in ({'capacity_verification_id':'missing'}, {'resource_id':self.resource.id}):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(payload=self.payload.model_copy(update=update)))
                self.assertLess(response.status_code,500,response.text)
                if response.status_code==200:self.assertNotEqual(response.json()['item']['status'],'succeeded')
        self.assertFalse(self.committed());self.assert_original()

    async def test_policy_change_after_ciphertext_before_settlement_keeps_unknown_custody(self):
        real=self.operator.storage_provider.operations._assess
        def changed(*args,**kwargs):
            report=real(*args,**kwargs)
            old=self.recovery.policy()
            self.recovery.configure(old.model_copy(update={'retention_count':old.retention_count+1}),
                actor=self.actor,expected_fingerprint=old.fingerprint())
            return report
        async with self.client() as client:
            with patch.object(self.operator.storage_provider.operations,'_assess',side_effect=changed):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,200,response.text)
            item=response.json()['item'];self.assertNotEqual(item['status'],'succeeded')
            self.assertFalse(self.committed())
            reserved=[a for a in self.artifacts.store.load().artifacts if a.source==SOURCE]
            self.assertEqual(len(reserved),1);self.assertIsNone(reserved[0].content)
            self.assertTrue(reserved[0].metadata['pending_content_sha256'])
            before=reserved[0]
            with patch.object(self.local_content,'put',side_effect=AssertionError('unknown replay')):
                again=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(again.status_code,409,again.text)
            self.assertEqual([a for a in self.artifacts.store.load().artifacts if a.source==SOURCE],[before])
        self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'],0)

    async def test_cancelled_observer_cancels_budget_and_joins_actual_io(self):
        from codex_web.storage.state_store import StorageOperationBudget
        budget=StorageOperationBudget(60)
        entered=threading.Event();release=threading.Event();actual=self.local_content.put
        def blocked(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('fixture gate not released')
            return actual(*args,**kwargs)
        async with self.client() as client:
            with patch.object(self.local_content,'put',side_effect=blocked), \
                 patch('codex_web.services.recovery_preservation_provider.StorageOperationBudget',return_value=budget):
                task=asyncio.create_task(client.post('/api/recovery/storage-preservation/operations',json=self.body()))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    task.cancel()
                    self.assertTrue(await asyncio.to_thread(budget.cancelled.wait,2))
                    self.assertFalse(task.done())
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'],1)
                finally:
                    release.set()
                    result=await asyncio.gather(task,return_exceptions=True)
                self.assertIsInstance(result[0],asyncio.CancelledError)
        self.assertFalse(self.committed());self.assert_original()

    async def test_disabled_binding_and_forged_authority_fields_cannot_capture(self):
        async with self.client() as client:
            body=self.body();body['actor']='forged-current-admin'
            response=await client.post('/api/recovery/storage-preservation/operations',json=body)
            self.assertEqual(response.status_code,422,response.text)
            self.registry.set_enabled(self.storage_binding.id,False,actor=self.actor)
            response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,409,response.text)
        self.assertFalse(self.committed());self.assert_original()

    async def test_reserved_disk_refusal_precedes_content_and_preserves_source(self):
        # Publish through the real configuration service then reissue fresh
        # proof for the changed pin: stale-proof rejection is not disk coverage.
        record=self.publish('disk_bytes',1024)
        _,pins=preservation_configuration(self.configuration)
        old=self.artifacts.get_evidence(self.payload.capacity_evidence_id,actor=self.actor)
        proof=self.artifacts.create_evidence(EvidenceCreate(evidence_type=old.evidence_type,
            resource_ids=old.resource_ids,source=old.source,result=old.result,summary=old.summary,
            metadata={**old.metadata,'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest(),
                      'disk_bytes':1024}),actor=self.actor)
        verification=self.artifacts.create_verification(VerificationCreate(evidence_ids=(proof.id,),
            method='Private current reduced disk budget',result=VerificationResult.VERIFIED),actor=self.independent)
        payload=self.payload.model_copy(update={'capacity_evidence_id':proof.id,'capacity_verification_id':verification.id})
        async with self.client() as client:
            with patch.object(self.local_content,'put',side_effect=AssertionError('disk refusal must precede write')):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(payload=payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded')
        self.assertFalse(self.committed());self.assert_original()

    async def test_preview_is_read_only_and_does_not_claim_capacity_or_semantic_health(self):
        async with self.client() as client:
            before=self.artifacts.store.load()
            response=await client.post('/api/recovery/storage-preservation/preview',json=self.body())
            self.assertEqual(response.status_code,200,response.text)
            result=response.json();self.assertFalse(result['mutated'])
            self.assertFalse(result['semantic_recovery_qualified'])
            self.assertGreater(result['inventory']['framed_bytes'],result['inventory']['payload_bytes'])
            self.assertNotIn('_database_directory',json.dumps(result))
            self.assertEqual(self.artifacts.store.load(),before)
        self.assert_original()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated private PostgreSQL DSN required')
class PostgresStorageOperatorTests(_PostgresFixture,StorageOperatorTests):
    async def asyncSetUp(self):
        with patch.object(intent_fixture,'SQLiteStateStore',self.state_store_factory):
            await super().asyncSetUp()
        self.addCleanup(self.sqlite.close)

    def writer_store(self):
        from tests.test_recovery_operator_postgres import PostgresRecoveryOperatorTests
        return PostgresRecoveryOperatorTests.writer_store(self)

    async def test_separate_session_revocation_before_receipt_cannot_publish_artifact(self):
        from codex_web.storage.identity_state import IdentityStateStore
        writer=IdentityStateStore(self.writer_store())
        real=self.operator.storage_provider.operations._assess
        def revoke(*args,**kwargs):
            report=real(*args,**kwargs)
            def changed(state):
                state.sessions=[s.model_copy(update={'revoked_at':time.time()})
                    if s.id==self.credentials.session_id else s for s in state.sessions]
                return state
            writer.update(changed)
            return report
        async with self.client() as client:
            with patch.object(self.operator.storage_provider.operations,'_assess',side_effect=revoke):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertNotEqual(response.status_code,500,response.text)
            if response.status_code==200:self.assertNotEqual(response.json()['item']['status'],'succeeded')
        self.assertFalse(self.committed())
        artifacts=[a for a in self.artifacts.store.load().artifacts if a.source==SOURCE]
        self.assertEqual(len(artifacts),1);self.assertIsNone(artifacts[0].content)
        self.assert_original()

    async def test_separate_binding_revocation_serializes_after_final_locked_read(self):
        from codex_web.storage.action_providers import ActionProviderStateStore
        from codex_web.services.action_providers import ActionProviderRegistry
        writer=ActionProviderRegistry(ActionProviderStateStore(self.writer_store()))
        entered=threading.Event();finished=threading.Event();threads=[]
        real=self.operator.storage_provider.operations.capacity
        def current(*args,**kwargs):
            result=real(*args,**kwargs)
            if kwargs.get('starting') is False:
                def change():
                    entered.set()
                    writer.set_enabled(self.storage_binding.id,False,actor=self.actor)
                    finished.set()
                thread=threading.Thread(target=change);threads.append(thread);thread.start()
                if not entered.wait(2):raise AssertionError('separate writer failed to enter')
                # Independent writer is blocked by the actual shared PG lock
                # while the current final-write transaction completes.
                if finished.wait(.05):raise AssertionError('canonical binding write bypassed final transaction lock')
            return result
        async with self.client() as client:
            try:
                with patch.object(self.operator.storage_provider.operations,'capacity',side_effect=current):
                    response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            finally:
                for thread in threads:
                    await asyncio.to_thread(thread.join,3)
                    self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        # Domain receipt is real even if the now-disabled binding prevents
        # final ActionIntent verification; no successful custody is fabricated.
        self.assertEqual(len(self.committed()),1,response.text)
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotEqual(response.json()['item']['status'],'succeeded')
        self.assert_original()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN') and
                     os.environ.get('CODEX_WEB_TEST_PRESERVATION_SCALE')=='1',
                     'explicit serialized private PG full-current-shape job')
class FullShapeEncryptedStorageTests(PostgresStorageOperatorTests):
    """Select ONLY this method in the separate full-scale request."""
    async def test_full_current_shape_governed_encrypted_capture_and_isolated_verify(self):
        import itertools
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        count,total,largest=118389,294508748,12373762
        ordinary,remainder=divmod(total-largest-6906,count-2)
        def rows():
            yield ('scale:000000','"'+'A'*(largest-2)+'"',1.0)
            yield ('scale:000001','{'+'?'*6905,2.0)
            for i in range(count-2):
                size=ordinary+(i<remainder)
                yield (f'scale:{i+2:06}','"'+'b'*(size-2)+'"',float(i+3))
        source=iter(rows())
        with self.sqlite._connection() as connection:
            with connection.cursor() as cursor:
                while True:
                    batch=list(itertools.islice(source,256))
                    if not batch:break
                    cursor.executemany('INSERT INTO codex_state_documents(namespace,payload,updated_at) VALUES (%s,%s,%s)',batch)
        with self.sqlite._connection() as connection:
            actual=connection.execute("SELECT COUNT(*),SUM(octet_length(payload)),MAX(octet_length(payload)) FROM codex_state_documents WHERE namespace LIKE 'scale:%'").fetchone()
        self.assertEqual(tuple(actual),(count,total,largest))
        # This configuration expresses a review budget, NOT a quota/cgroup
        # change. Root must independently reserve actual whole-job disk before
        # launching this optional request; no fitting capacity means no run.
        self.publish('disk_bytes',8*1024**3)
        self.publish('wall_seconds',85)
        _,pins=preservation_configuration(self.configuration)
        old=self.artifacts.get_evidence(self.payload.capacity_evidence_id,actor=self.actor)
        proof=self.artifacts.create_evidence(EvidenceCreate(evidence_type=old.evidence_type,
            resource_ids=old.resource_ids,source=old.source,result=old.result,summary=old.summary,
            metadata={**old.metadata,'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest(),
                      'disk_bytes':8*1024**3,'wall_seconds':85,'expires_at':time.time()+300}),actor=self.actor)
        verified=self.artifacts.create_verification(VerificationCreate(evidence_ids=(proof.id,),
            method='Private current full-scale admission',result=VerificationResult.VERIFIED),actor=self.independent)
        self.payload=self.payload.model_copy(update={'capacity_evidence_id':proof.id,'capacity_verification_id':verified.id})
        await self.test_capture_verify_same_backend_once_preserves_invalid_rows_without_health_pass()
