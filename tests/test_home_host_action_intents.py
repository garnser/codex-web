"""Actual private ActionIntent/worker/crypto/content composition, fake host process."""
import asyncio
from dataclasses import replace
import json
import threading
import time
import unittest
from tests import test_home_host_worker_operation as fixture
from codex_web.action_intents import (ActionIntentCreate,ActionIntentClaimRequest,ActionIntentExecuteRequest,ActionDecisionSnapshot)
from codex_web.action_providers import ActionRequest,ActionProviderBindingCreate
from codex_web.crypto import ManagedKeyCreate,EncryptedEnvelope
from codex_web.resources import ResourceCreate
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.security import ExecutionSecurityPolicy,NetworkEgressPolicy,FilesystemBoundaryPolicy,ProcessBoundaryPolicy
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.services.artifact_content import ArtifactContentService,ArtifactContentRegistry
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.services.action_providers import ActionProviderRegistry,ActionExecutionService
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.services.action_intents import ActionIntentService,ActionIntentLeaseError,ActionIntentConflictError
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.services.home_host_action_provider import HomeHostActionProvider,HOST_COMMAND_ACTION

class HomeHostActionIntentTests(fixture.HomeHostWorkerOperationTests):
    def publish(self,payload):
        if not hasattr(self,'host_resource_id'):
            self.host_resource_id=self.f.resources.create(ResourceCreate(resource_type='environment',name='Private fixture host'),actor=self.actor).id
            self.crypto=CryptoKeyService(CryptoKeyStore(self.f.sqlite),{'local':LocalFileKeyBackend(self.root/'test-keys')})
            self.output_key=self.crypto.create_key(ManagedKeyCreate(project_id='home',resource_id=self.host_resource_id,purpose='artifact'),actor=self.actor)
            self.host_options={'host_output_key_id':self.output_key.id}
        return super().publish(payload)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.worker_actor=self.f.identity.bootstrap_service_actor(identity_id=self.f.worker.service_identity_id,
            name='Fixture worker with explicit action and artifact scopes',scope=self.actor.tenant,
            service_scopes=('execution-worker:run','action-intent:worker','artifact-evidence:admin'))
        self.runtime.worker_actor=self.worker_actor
        self.host_backend.probe=lambda: type('Status',(),{'ready':True})()
        contents=ArtifactContentRegistry();contents.register(LocalArtifactContentStore(self.root/'protected-content'))
        self.artifacts=ArtifactEvidenceService(ArtifactEvidenceStore(self.f.sqlite),resources=self.f.resources,
            content=ArtifactContentService(contents),governance=DataGovernanceService(DataGovernanceStore(self.f.sqlite)))
        self.provider=HomeHostActionProvider(home_host=self.service.home_operator_host,worker_runtime=self.runtime,
            artifacts=self.artifacts,crypto=self.crypto)
        registry=ActionProviderRegistry(ActionProviderStateStore(self.f.sqlite));registry.register(self.provider)
        execution=ActionExecutionService(registry,self.f.resources)
        self.intents=ActionIntentService(ActionIntentStore(self.f.sqlite),execution,artifact_evidence=self.artifacts,
            authority=self.authority,identity=self.f.identity,execution_workers=self.f.workers,execution_profiles=self.f.execution_profiles)
        self.service.action_intents=self.intents
        self.action_binding=registry.bind(ActionProviderBindingCreate(provider_type=self.provider.provider_type,
            provider_instance=self.provider.provider_instance,project_id='home',resource_ids=(self.host_resource_id,),
            security_policy=ExecutionSecurityPolicy(network=NetworkEgressPolicy(enabled=True),
                filesystem=FilesystemBoundaryPolicy(allowed_read_roots=('/',),allowed_write_roots=('/',)),
                process=ProcessBoundaryPolicy(allow_shell=True))),actor=self.actor,resources=self.f.resources)
        self.entered=threading.Event();self.exited=threading.Event();self.block=False

    def effect(self,operation,*,admission_guard,**kwargs):
        admission_guard();self.effects+=1;self.entered.set()
        try:
            while self.block:
                admission_guard();time.sleep(.01)
            self.after_spawn();admission_guard()
            return replace(super_result(),stdout='synthetic-sensitive-output')
        finally:self.exited.set()

    def create(self):
        request=ActionRequest(action_id=HOST_COMMAND_ACTION,organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,project_id='home',resource_ids=(self.host_resource_id,),
            requested_by=self.actor.identity_id,parameters={'assignment_id':self.assignment.id,'fence':self.assignment.fence,'argv':['/usr/bin/true']})
        intent=self.intents.create(ActionIntentCreate(binding_id=self.action_binding.id,request=request,
            execution_id=self.assignment.execution_id,policy_decision=ActionDecisionSnapshot(outcome='allow',source='policy:fixture',reason='explicit private host operation')),
            actor=self.actor,assignment_id=self.assignment.id)
        claimed=self.intents.claim(ActionIntentClaimRequest(worker_id='fixture-host-action',lease_seconds=120),actor=self.worker_actor,intent_id=intent.id)
        self.assertIsNotNone(claimed, f'fixture intent not claimable: {intent.status}, {intent.last_error}')
        return intent

    async def execute(self,intent):
        return await self.intents.execute_claimed(intent.id,'fixture-host-action',actor=self.worker_actor)

    async def test_canonical_intent_receipt_encrypts_output_and_preserves_home(self):
        intent=self.create();await self.execute(intent)
        current=self.intents.get(intent.id,self.actor)
        self.assertEqual(current.status,'succeeded');self.assertEqual(current.attempt,1);self.assertEqual(self.effects,1)
        history=self.intents.history(intent.id,self.actor)
        receipt=history['receipts'][0]
        encoded=json.dumps(receipt)
        self.assertNotIn('synthetic-sensitive-output',encoded)
        artifact=self.artifacts.get_artifact(receipt['result']['output']['output_artifact_id'],actor=self.actor)
        governed=self.artifacts.governance.get_record(artifact.governance_record_id,self.actor)
        self.assertTrue(governed.deny_model_context);self.assertEqual(governed.classification,'secret')
        _,chunks=self.artifacts.open_artifact_content(artifact.id,actor=self.actor)
        cipher=b''.join(chunks);self.assertNotIn(b'synthetic-sensitive-output',cipher)
        clear=self.crypto.decrypt(EncryptedEnvelope.model_validate_json(cipher),self.provider._output_context(self.assignment,self.host_resource_id,artifact.id),actor=self.actor)
        self.assertIn(b'synthetic-sensitive-output',clear)
        self.assertEqual(self.current().status,'running');self.assertEqual(self.current().fence,self.assignment.fence)
        with self.assertRaises((ActionIntentLeaseError,ActionIntentConflictError)):await self.execute(intent)
        self.assertEqual(self.effects,1)

    async def test_post_effect_custody_loss_retains_unknown_and_no_retry(self):
        self.after_spawn=lambda:self.mutate_assignment(fence=self.assignment.fence+1)
        intent=self.create();await self.execute(intent)
        current=self.intents.get(intent.id,self.actor)
        self.assertIn(current.status,{'uncertain','requires_reconciliation'})
        self.assertEqual(self.effects,1);self.assertEqual(current.attempt,1)
        with self.assertRaises((ActionIntentLeaseError,ActionIntentConflictError)):await self.execute(intent)
        self.assertEqual(self.effects,1)

    async def test_same_thread_selection_revision_change_after_effect_is_unknown(self):
        self.after_spawn=lambda:self.select('operator-thread',1)
        intent=self.create();await self.execute(intent)
        current=self.intents.get(intent.id,self.actor)
        self.assertIn(current.status,{'uncertain','requires_reconciliation'})
        self.assertEqual(self.effects,1)
        with self.assertRaises((ActionIntentLeaseError,ActionIntentConflictError)):await self.execute(intent)
        self.assertEqual(self.effects,1)

    async def test_missing_output_governance_refuses_before_host_effect(self):
        intent=self.create();self.artifacts.governance=None
        await self.execute(intent)
        self.assertEqual(self.effects,0)
        self.assertNotEqual(self.intents.get(intent.id,self.actor).status,'succeeded')

    async def test_cancel_waits_for_actual_owned_operation(self):
        self.block=True;intent=self.create();task=asyncio.create_task(self.execute(intent))
        deadline=time.monotonic()+5
        while not self.entered.is_set() and not task.done() and time.monotonic()<deadline:await asyncio.sleep(.01)
        if task.done():await task
        self.assertTrue(self.entered.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(self.exited.is_set());self.assertEqual(self.effects,1)
        self.assertEqual(self.current().status,'running')


def super_result():
    from codex_web.local_execution_backend import LocalExecutionResult
    return LocalExecutionResult('true','sha256:'+'a'*64,0,'','',.01)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HomeHostActionIntentTests(n) for n in HomeHostActionIntentTests.__dict__ if n.startswith('test_'))
