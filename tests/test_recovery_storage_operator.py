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

    async def test_tampered_envelope_public_operation_reports_no_untrusted_payload(self):
        from codex_web.services.recovery_storage_preservation import MAGIC,_frame
        import struct
        async with self.client() as client:
            item=await self.capture(client)
            record=self.committed()[0]
            artifact=self.artifacts.get_artifact(record.artifact_ids[0],actor=self.actor)
            from codex_web.artifact_content import ArtifactContentScope
            scope=ArtifactContentScope(organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
            path=self.local_content._resolve(scope,artifact.content.locator)
            wire=path.read_bytes();frames=[];offset=len(MAGIC)
            while offset<len(wire):
                size=struct.unpack('>Q',wire[offset:offset+8])[0]
                frames.append(json.loads(wire[offset+8:offset+8+size]));offset+=8+size
            sentinel='SYNTHETIC_PRIVATE_PUBLIC_BOUNDARY_SENTINEL'
            next(f for f in frames if f.get('kind')=='chunk')['envelope']={'extra':sentinel}
            # Physical corruption is injected ONLY into this synthetic private
            # fixture. Canonical immutable Artifact/receipt metadata is intact.
            path.write_bytes(MAGIC+b''.join(_frame(f) for f in frames))
            payload=self.payload.model_copy(update={'operation_id':'tamper-verify','idempotency_key':'tamper-verify',
                'artifact_id':artifact.id})
            response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded')
            self.assertNotIn(sentinel,response.text)
            history=self.service.history(response.json()['item']['id'],self.actor)
            self.assertNotIn(sentinel,json.dumps(history))
            self.assertEqual(self.committed(),[record])
        self.assert_original()

    def preservation_writer(self):
        from codex_web.services.crypto_keys import CryptoKeyService
        from codex_web.storage.crypto_keys import CryptoKeyStore
        if hasattr(self.sqlite,'dsn'):
            state=self.writer_store()
        else:
            from codex_web.storage.sqlite_state import SQLiteStateStore
            state=SQLiteStateStore(self.sqlite.path)
        self.assertIsNot(state,self.sqlite)
        return CryptoKeyService(CryptoKeyStore(state),self.recovery.crypto.backends)

    def reissue_capacity(self,**changes):
        for suffix,value in changes.items():self.publish(suffix,value)
        config,pins=preservation_configuration(self.configuration)
        old=self.artifacts.get_evidence(self.payload.capacity_evidence_id,actor=self.actor)
        evidence=self.artifacts.create_evidence(EvidenceCreate(evidence_type=old.evidence_type,
            resource_ids=old.resource_ids,source=old.source,result=old.result,summary=old.summary,
            metadata={**old.metadata,'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest(),
                'memory_max_bytes':config['memory_bytes'],'memory_bytes':config['memory_bytes'],
                'disk_bytes':config['disk_bytes'],'wall_seconds':config['wall_seconds'],
                'expires_at':time.time()+300}),actor=self.actor)
        proof=self.artifacts.create_verification(VerificationCreate(evidence_ids=(evidence.id,),
            method='Fresh private current effective budget',result=VerificationResult.VERIFIED),actor=self.independent)
        self.payload=self.payload.model_copy(update={'capacity_evidence_id':evidence.id,'capacity_verification_id':proof.id})

    def shape_row(self,*,remove=False,size=2*1024**2):
        pg=hasattr(self.sqlite,'dsn');table='codex_state_documents' if pg else 'state_documents';mark='%s' if pg else '?'
        with self.sqlite._connection() as connection:
            if remove:connection.execute(f"DELETE FROM {table} WHERE namespace='synthetic-large-shape'")
            else:connection.execute(f'INSERT INTO {table}(namespace,payload,updated_at) VALUES ({mark},{mark},{mark})',
                ('synthetic-large-shape','"'+'Z'*(size-2)+'"',7.125))

    async def test_old_capture_sizes_verify_after_live_shrink_before_any_target_write(self):
        self.shape_row()
        operations=self.operator.storage_provider.operations
        async with self.client() as client:
            item=await self.capture(client)
            receipt=(await client.get('/api/recovery/storage-preservation/receipts/'+item['id'])).json()
            self.assertGreaterEqual(receipt['report']['inventory']['max_value_bytes'],2*1024**2)
            artifact_id=receipt['artifact_ids'][0]
            self.shape_row(remove=True)
            # Both budgets cover the now-small live fixture; neither covers
            # this authentic immutable larger capture. No isolated target may
            # even be opened, and no verification receipt may be published.
            for name,limits in [('memory',{'memory_bytes':110*1024**2,'disk_bytes':512*1024**2}),
                                ('disk',{'memory_bytes':512*1024**2,'disk_bytes':32*1024**2})]:
                self.reissue_capacity(**limits)
                payload=self.payload.model_copy(update={'operation_id':'small-'+name,'idempotency_key':'small-'+name,'artifact_id':artifact_id})
                with patch.object(operations,'isolated_target',side_effect=AssertionError('unadmitted target creation')):
                    response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
                self.assertEqual(response.status_code,200,response.text)
                self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
                self.assertEqual(len(self.committed()),1)
            self.reissue_capacity(memory_bytes=512*1024**2,disk_bytes=512*1024**2)
            payload=self.payload.model_copy(update={'operation_id':'adequate-old','idempotency_key':'adequate-old','artifact_id':artifact_id})
            response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['item']['status'],'succeeded',response.text)
            result=(await client.get('/api/recovery/storage-preservation/receipts/'+response.json()['item']['id'])).json()
            self.assertEqual(result['report']['raw_stream_sha256'],receipt['report']['raw_stream_sha256'])
            self.assertEqual(result['report']['inventory'],receipt['report']['inventory'])
        self.assert_original()

    async def test_capture_growth_refuses_unadmitted_row_before_payload_fetch(self):
        self.reissue_capacity(memory_bytes=128*1024**2)
        operations=self.operator.storage_provider.operations;real=operations.inventory
        def growing():
            stats=real()
            self.assertLess(stats['max_value_bytes'],1024**2)
            self.shape_row(size=4*1024**2)
            return stats
        from codex_web.storage import storage_preservation as wire
        export=wire.export_rows;selected=[]
        def inspect_rows(header,rows,limits):
            def seen():
                for row in rows:
                    selected.append(row[1]);yield row
            yield from export(header,seen(),limits)
        async with self.client() as client:
            with patch.object(operations,'inventory',side_effect=growing), patch.object(wire,'export_rows',side_effect=inspect_rows):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
        self.assertEqual(selected,[],'metadata refusal must precede every payload row fetch')
        self.assertFalse(self.committed())
        artifacts=[a for a in self.artifacts.store.load().artifacts if a.source==SOURCE]
        self.assertEqual(len(artifacts),1);self.assertIsNone(artifacts[0].content)
        self.assertEqual(artifacts[0].metadata['state'],'reserved_unknown_until_verified')
        # The actual metadata preflight refuses before selecting large TEXT;
        # the growing source row remains byte-exact, not deleted as cleanup.
        pg=hasattr(self.sqlite,'dsn');table='codex_state_documents' if pg else 'state_documents'
        with self.sqlite._connection() as connection:
            value,ts=connection.execute(f"SELECT payload,updated_at FROM {table} WHERE namespace='synthetic-large-shape'").fetchone()
        self.assertEqual(value,'"'+'Z'*(4*1024**2-2)+'"');self.assertEqual(ts,7.125)
        self.assert_original()

    async def test_retained_capture_blocks_actual_old_version_revoke_and_decrypts(self):
        from codex_web.services.crypto_keys import CryptoKeyConflictError
        key_id=self.recovery.policy().backup_key_id;writer=self.preservation_writer()
        self.assertFalse(self.recovery.store.load().backups)
        async with self.client() as client:
            item=await self.capture(client)
            receipt=(await client.get('/api/recovery/storage-preservation/receipts/'+item['id'])).json()
            artifact=self.artifacts.get_artifact(receipt['artifact_ids'][0],actor=self.actor)
            self.assertEqual(artifact.metadata['preservation_key_id'],key_id)
            self.assertEqual(artifact.metadata['preservation_key_version'],1)
            writer.rotate(key_id,actor=self.actor)
            impact=writer.usage(key_id,self.actor,version=1)
            consumer=[x for x in impact['items'] if x['object_type']=='storage_preservation']
            self.assertEqual([x['object_id'] for x in consumer],[artifact.id])
            self.assertEqual(consumer[0]['versions'],[1])
            with self.assertRaises(CryptoKeyConflictError):writer.revoke_version(key_id,1,'retained synthetic capture',actor=self.actor)
            payload=self.payload.model_copy(update={'operation_id':'old-key-verify','idempotency_key':'old-key-verify','artifact_id':artifact.id})
            response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['item']['status'],'succeeded',response.text)
            observed=(await client.get('/api/recovery/storage-preservation/receipts/'+response.json()['item']['id'])).json()
            self.assertEqual(observed['report']['raw_stream_sha256'],receipt['report']['raw_stream_sha256'])
            # Genuine verified content destruction is the existing owning
            # lifecycle, not expiry/invalidation or a fabricated manifest.
            self.artifacts.delete_artifact_content(artifact.id,actor=self.actor)
            writer.revoke_version(key_id,1,'content genuinely tombstoned',actor=self.actor)
        self.assert_original()

    async def test_unknown_pending_capture_protects_version_despite_lifecycle_invalidation(self):
        from codex_web.services.crypto_keys import CryptoKeyConflictError
        from codex_web.crypto import KeyPurpose,ManagedKeyCreate
        key_id=self.recovery.policy().backup_key_id;writer=self.preservation_writer()
        async with self.client() as client:
            with patch.object(self.operator.storage_provider.operations,'_assess',side_effect=RuntimeError('after encrypted write')):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
        self.assertFalse(self.committed())
        artifact=next(a for a in self.artifacts.store.load().artifacts if a.source==SOURCE)
        self.assertIsNone(artifact.content);self.assertIn('pending_content_json',artifact.metadata)
        self.artifacts.invalidate_artifact(artifact.id,'synthetic invalid observer',actor=self.actor)
        writer.rotate(key_id,actor=self.actor)
        with self.assertRaises(CryptoKeyConflictError):writer.revoke_version(key_id,1,'pending unknown',actor=self.actor)
        # Unrelated key retirement is not blocked by another key's consumer.
        other=writer.create_key(ManagedKeyCreate(purpose=KeyPurpose.BACKUP,backend_type='local'),actor=self.actor)
        writer.rotate(other.id,actor=self.actor)
        writer.revoke_version(other.id,1,'unrelated key',actor=self.actor)
        # Same id in a different tenant is never projected as a consumer.
        foreign=self.actor.model_copy(update={'organization_id':'foreign-synthetic'})
        projection=writer.usage_service.snapshot(writer.get_key(key_id,self.actor),foreign,version=1)
        self.assertFalse([r for r in projection['items'] if r['object_type']=='storage_preservation'])
        self.assert_original()

    async def test_prior_separate_rotation_and_revocation_refuses_capture_reservation(self):
        writer=self.preservation_writer();key_id=self.recovery.policy().backup_key_id
        real=self.artifacts.create_artifact
        def revoke_first(*args,**kwargs):
            writer.rotate(key_id,actor=self.actor)
            writer.revoke_version(key_id,1,'before canonical reservation',actor=self.actor)
            return real(*args,**kwargs)
        async with self.client() as client:
            with patch.object(self.artifacts,'create_artifact',side_effect=revoke_first), \
                 patch.object(self.local_content,'put',side_effect=AssertionError('revoked capture write')):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
        self.assertFalse(self.committed())
        self.assertFalse([a for a in self.artifacts.store.load().artifacts if a.source==SOURCE])
        self.assert_original()

    async def test_key_writer_serializes_after_capture_reservation_and_protects_unknown(self):
        from codex_web.services.crypto_keys import CryptoKeyConflictError
        writer=self.preservation_writer();key_id=self.recovery.policy().backup_key_id
        real=self.artifacts.create_artifact;threads=[];entered=threading.Event();finished=threading.Event();outcomes=[]
        def wrapped(*args,**kwargs):
            reserve=kwargs['_commit']
            def guarded(updater):
                def while_locked(state):
                    result=updater(state)
                    def retire():
                        try:
                            entered.set();writer.rotate(key_id,actor=self.actor)
                            writer.revoke_version(key_id,1,'after actual reservation',actor=self.actor)
                        except CryptoKeyConflictError:outcomes.append('protected')
                        finally:finished.set()
                    thread=threading.Thread(target=retire);threads.append(thread);thread.start()
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(finished.wait(.05),'retirement crossed locked capture reservation')
                    return result
                return reserve(while_locked)
            kwargs['_commit']=guarded
            return real(*args,**kwargs)
        async with self.client() as client:
            try:
                with patch.object(self.artifacts,'create_artifact',side_effect=wrapped):
                    response=await client.post('/api/recovery/storage-preservation/operations',json=self.body())
            finally:
                for thread in threads:
                    await asyncio.to_thread(thread.join,3)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(outcomes,['protected']);self.assertTrue(finished.is_set())
        artifacts=[a for a in self.artifacts.store.load().artifacts if a.source==SOURCE]
        self.assertEqual(len(artifacts),1);self.assertEqual(artifacts[0].metadata['preservation_key_version'],1)
        # A racing key rotation may refuse capture's final current-key check;
        # it must never make this original retained dependency disappear.
        self.assertEqual([r['object_id'] for r in writer.usage(key_id,self.actor,version=1)['items']
                          if r['object_type']=='storage_preservation'],[artifacts[0].id])
        self.assert_original()


    async def test_separate_revocation_during_old_batch_decrypt_refuses_terminal_authentication(self):
        from codex_web.crypto import KeyPurpose,ManagedKeyCreate
        from codex_web.services.crypto_keys import CryptoKeyConflictError
        crypto=self.recovery.crypto;writer=self.preservation_writer()
        key=crypto.create_key(ManagedKeyCreate(purpose=KeyPurpose.BACKUP,backend_type='local'),actor=self.actor)
        context=self.operator.storage_provider.operations._context(self.payload,self.actor)
        chunk=context.model_copy(update={'object_id':context.object_id+':synthetic'})
        with crypto.envelope_batch(key.id,context,actor=self.actor,operation='encrypt') as batch:
            envelope=batch.apply(b'synthetic-old-version',chunk)
        writer.rotate(key.id,actor=self.actor)
        audits=lambda:[e.id for e in crypto.store.load().events if e.operation=='decrypt_batch' and e.key_id==key.id]
        before=audits()
        with self.assertRaises(CryptoKeyConflictError):
            with crypto.envelope_batch(key.id,context,actor=self.actor,operation='decrypt',key_version=1) as batch:
                self.assertEqual(batch.apply(envelope,chunk),b'synthetic-old-version')
                # Real separate owning connection/pool commits revocation
                # before the original batch's final locked recheck. No fake
                # store mutation or relaxed protected-consumer check.
                writer.revoke_version(key.id,1,'separate writer during decrypt',actor=self.actor)
        self.assertEqual(audits(),before)
        with self.assertRaisesRegex(CryptoKeyConflictError,'closed'):batch.apply(envelope,chunk)
        with self.assertRaises(CryptoKeyConflictError):
            with crypto.envelope_batch(key.id,context,actor=self.actor,operation='decrypt',key_version=1):
                self.fail('revoked old version admitted')
        self.assertFalse(self.committed());self.assert_original()

    async def test_verify_reuses_bound_capture_report_but_authenticates_content_once(self):
        operations=self.operator.storage_provider.operations
        async with self.client() as client:
            item=await self.capture(client)
            receipt=(await client.get('/api/recovery/storage-preservation/receipts/'+item['id'])).json()
            payload=self.payload.model_copy(update={'operation_id':'single-pass','idempotency_key':'single-pass','artifact_id':receipt['artifact_ids'][0]})
            content=self.artifacts.content
            with patch.object(operations,'_assess',side_effect=AssertionError('VERIFY reassessment forbidden')), \
                 patch.object(content,'open',wraps=content.open) as opened:
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['item']['status'],'succeeded',response.text)
            self.assertEqual(opened.call_count,1)
            observed=(await client.get('/api/recovery/storage-preservation/receipts/'+response.json()['item']['id'])).json()['report']
            for field in ('inventory','rows','raw_stream_sha256','semantic_status','invalid_json_rows',
                          'diagnostics','diagnostics_bounded','diagnostics_truncated'):
                self.assertEqual(observed[field],receipt['report'][field],field)
            self.assertTrue(observed['isolated_restore_verified'])
            self.assertFalse(observed['semantic_recovery_qualified'])
        self.assert_original()

    async def test_verify_rejects_public_receipt_and_changed_content_binding_before_target(self):
        operations=self.operator.storage_provider.operations
        async with self.client() as client:
            item=await self.capture(client);record=self.committed()[0]
            artifact=self.artifacts.get_artifact(record.artifact_ids[0],actor=self.actor)
            # A public Evidence producer cannot choose the private idempotent
            # receipt identity, even with copied source/report metadata.
            copied=self.artifacts.create_evidence(EvidenceCreate(evidence_type=record.evidence_type,
                artifact_ids=record.artifact_ids,resource_ids=record.resource_ids,source=SOURCE,
                result=record.result,summary='Synthetic public receipt clone',metadata=record.metadata),actor=self.actor)
            variants=[artifact.model_copy(update={'metadata':{**artifact.metadata,'capture_evidence_id':copied.id}}),
                      artifact.model_copy(update={'digest':artifact.digest.model_copy(update={'value':'0'*64})}),
                      artifact.model_copy(update={'content':artifact.content.model_copy(update={'size_bytes':artifact.content.size_bytes+1})})]
            for index,changed in enumerate(variants):
                def replace(state):
                    state.artifacts=[changed if a.id==artifact.id else a for a in state.artifacts];return state
                self.artifacts.store.update(replace)
                payload=self.payload.model_copy(update={'operation_id':'bad-binding-'+str(index),'idempotency_key':'bad-binding-'+str(index),'artifact_id':artifact.id})
                with patch.object(operations,'isolated_target',side_effect=AssertionError('untrusted report must not create target')):
                    response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
                self.assertEqual(response.status_code,200,response.text)
                self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
            self.assertEqual(self.committed(),[record,copied])
        self.assert_original()

    async def test_verify_target_hash_is_checked_against_trusted_report_after_full_restore(self):
        from contextlib import contextmanager
        operations=self.operator.storage_provider.operations
        async with self.client() as client:
            await self.capture(client);record=self.committed()[0]
            # Simulate bounded canonical report corruption. Even this exact
            # private receipt ID cannot replace actual target readback proof.
            report=json.loads(record.metadata['report_json']);report['raw_stream_sha256']='0'*64
            changed=record.model_copy(update={'metadata':{**record.metadata,'report_json':json.dumps(report)}})
            def corrupt(state):
                state.evidence=[changed if e.id==record.id else e for e in state.evidence];return state
            self.artifacts.store.update(corrupt)
            original=operations.isolated_target;restored=[];cleaned=[]
            @contextmanager
            def inspected(*args,**kwargs):
                try:
                    with original(*args,**kwargs) as target:
                        real=target.restore_storage_snapshot
                        def restore(*a,**k):
                            result=real(*a,**k);restored.append(True);return result
                        with patch.object(target,'restore_storage_snapshot',side_effect=restore):yield target
                finally:cleaned.append(True)
            payload=self.payload.model_copy(update={'operation_id':'hash-mismatch','idempotency_key':'hash-mismatch','artifact_id':record.artifact_ids[0]})
            with patch.object(operations,'isolated_target',side_effect=inspected):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
            self.assertEqual(restored,[True]);self.assertEqual(cleaned,[True])
            self.assertIn('Isolated storage row digest mismatch',response.json()['item']['last_error'])
            self.assertEqual(self.committed(),[changed])
        self.assert_original()

    async def test_separate_capture_receipt_invalidation_after_readback_denies_commit(self):
        from contextlib import contextmanager
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from codex_web.artifact_evidence import EvidenceLifecycle
        operations=self.operator.storage_provider.operations
        writer=ArtifactEvidenceService(ArtifactEvidenceStore(self.preservation_writer().store.store))
        async with self.client() as client:
            await self.capture(client);record=self.committed()[0]
            original=operations.isolated_target;post_hash=[]
            @contextmanager
            def invalidate_after_hash(*args,**kwargs):
                with original(*args,**kwargs) as target:
                    yield target
                    post_hash.append(True)
                    writer.invalidate_evidence(record.id,'separate writer after target hash',actor=self.actor)
            payload=self.payload.model_copy(update={'operation_id':'late-receipt','idempotency_key':'late-receipt','artifact_id':record.artifact_ids[0]})
            with patch.object(operations,'isolated_target',side_effect=invalidate_after_hash):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
            self.assertEqual(post_hash,[True])
            self.assertIn('Capture receipt changed before verification commit',response.json()['item']['last_error'])
            self.assertEqual(len(self.committed()),1)
            self.assertEqual(self.committed()[0].lifecycle,EvidenceLifecycle.INVALIDATED)
        self.assert_original()


    async def test_single_pass_truncated_encrypted_footer_rolls_back_target_and_refuses_receipt(self):
        from contextlib import contextmanager
        from codex_web.artifact_content import ArtifactContentScope
        operations=self.operator.storage_provider.operations
        async with self.client() as client:
            await self.capture(client);record=self.committed()[0]
            artifact=self.artifacts.get_artifact(record.artifact_ids[0],actor=self.actor)
            scope=ArtifactContentScope(organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
            path=self.local_content._resolve(scope,artifact.content.locator)
            path.write_bytes(path.read_bytes()[:-1])  # synthetic content corruption ONLY
            original=operations.isolated_target;rollback=[]
            @contextmanager
            def checked(*args,**kwargs):
                with original(*args,**kwargs) as target:
                    prefix='codex_' if hasattr(target,'dsn') else ''
                    def physical():
                        with target._connection() as connection:
                            rows=connection.execute('SELECT COUNT(*) FROM '+prefix+'state_documents').fetchone()[0]
                            metadata=connection.execute('SELECT key,value,updated_at FROM '+prefix+'state_metadata ORDER BY key').fetchall()
                            return rows,metadata
                    before=physical();real=target.restore_storage_snapshot
                    def restore(*a,**k):
                        try:return real(*a,**k)
                        finally:
                            self.assertEqual(physical(),before)
                            rollback.append(True)
                    with patch.object(target,'restore_storage_snapshot',side_effect=restore):yield target
            payload=self.payload.model_copy(update={'operation_id':'truncated-verify','idempotency_key':'truncated-verify','artifact_id':artifact.id})
            with patch.object(operations,'isolated_target',side_effect=checked):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotEqual(response.json()['item']['status'],'succeeded',response.text)
            self.assertEqual(rollback,[True]);self.assertEqual(self.committed(),[record])
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
        async with self.client() as client:
            capture_started=time.perf_counter()
            item=await self.capture(client)
            print(json.dumps({'phase':'governed_encrypted_capture','seconds':time.perf_counter()-capture_started}),flush=True)
            captured=await client.get('/api/recovery/storage-preservation/receipts/'+item['id'])
            self.assertEqual(captured.status_code,200,captured.text)
            report=captured.json()['report']
            self.assertGreaterEqual(report['rows'],count)
            self.assertEqual(report['semantic_status'],'invalid')
            # Full-size encryption may legitimately consume the original
            # admission's start-freshness window. Verification is a NEW scoped
            # operation and receives genuinely fresh independent proof.
            current=self.artifacts.create_evidence(EvidenceCreate(evidence_type=proof.evidence_type,
                resource_ids=proof.resource_ids,source=proof.source,result=proof.result,summary=proof.summary,
                metadata={**proof.metadata,'expires_at':time.time()+300}),actor=self.actor)
            verification=self.artifacts.create_verification(VerificationCreate(evidence_ids=(current.id,),
                method='Fresh private isolated-verify resource admission',result=VerificationResult.VERIFIED),actor=self.independent)
            payload=self.payload.model_copy(update={'operation_id':'verify-scale','idempotency_key':'verify-scale',
                'artifact_id':captured.json()['artifact_ids'][0],
                'capacity_evidence_id':current.id,'capacity_verification_id':verification.id})
            from contextlib import contextmanager
            original=self.operator.storage_provider.operations.isolated_target
            sql_oracle=[]
            progress={'target_entered':False,'restore_completed':False,'target_hash_completed':False,'literal_sql_completed':False}
            @contextmanager
            def checked_target(*args,**kwargs):
                with original(*args,**kwargs) as target:
                    progress['target_entered']=True
                    restore=target.restore_storage_snapshot
                    def traced_restore(*a,**k):
                        result=restore(*a,**k);progress['restore_completed']=True;return result
                    with patch.object(target,'restore_storage_snapshot',side_effect=traced_restore):yield target
                    progress['target_hash_completed']=True
                    # Existing target transaction has committed an authenticated
                    # stream, but custody is still held and cleanup has not run.
                    # Independently read literal expected seed values via SQL.
                    with target._connection(read_only_snapshot=True) as connection:
                        with connection.cursor(name='scale_oracle') as cursor:
                            cursor.execute("SELECT namespace,payload,updated_at FROM codex_state_documents WHERE namespace LIKE 'scale:%' ORDER BY namespace COLLATE \"C\"")
                            seen=0
                            while True:
                                batch=cursor.fetchmany(64)
                                if not batch:break
                                for k,v,t in batch:
                                    self.assertEqual(k,f'scale:{seen:06}')
                                    if seen==0:expected='"'+'A'*(largest-2)+'"'
                                    elif seen==1:expected='{'+'?'*6905
                                    else:
                                        size=ordinary+(seen-2<remainder)
                                        expected='"'+'b'*(size-2)+'"'
                                    self.assertEqual(v,expected)
                                    self.assertEqual(t,float(seen+1))
                                    seen+=1
                            self.assertEqual(seen,count)
                        literal=connection.execute("SELECT payload,updated_at FROM codex_state_documents WHERE namespace='synthetic-malformed-usage'").fetchone()
                        self.assertEqual(literal,(self.opaque,1.125))
                    sql_oracle.append(True);progress['literal_sql_completed']=True
            phase=time.perf_counter()
            with patch.object(self.operator.storage_provider.operations,'isolated_target',side_effect=checked_target):
                response=await client.post('/api/recovery/storage-preservation/operations',json=self.body(VERIFY,payload))
            print(json.dumps({'phase':'verify_response','http_status':response.status_code,
                'intent_status':response.json().get('item',{}).get('status'),
                'progress':progress,'full_encrypted_verify_and_independent_sql_seconds':time.perf_counter()-phase}),flush=True)
            self.assertEqual(sql_oracle,[True])
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['item']['status'],'succeeded',response.text)
            isolated=await client.get('/api/recovery/storage-preservation/receipts/'+response.json()['item']['id'])
            self.assertEqual(isolated.status_code,200,isolated.text)
            restored=isolated.json()['report']
            self.assertTrue(restored['isolated_restore_verified'])
            self.assertFalse(restored['semantic_recovery_qualified'])
            self.assertEqual(restored['raw_stream_sha256'],report['raw_stream_sha256'])
            print(json.dumps({'scale_rows':count,'scale_payload_bytes':total,'largest_value_bytes':largest,
                'snapshot_rows':report['rows'],'snapshot_sha256':report['raw_stream_sha256'],
                'semantic_status':restored['semantic_status'],'isolated_restore_verified':True}))
        self.assert_original()
