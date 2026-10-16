"""Composed real ActionIntent/Recovery/provider stores; synthetic data only."""
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_action_intents as intent_fixtures
from tests.test_recovery import _Clock
from tests import test_recovery_reconciliation as recovery_fixtures
from codex_web.action_intents import ActionIntentCreate, ActionIntentReconcileRequest, ActionIntentStatus, ActionDecisionSnapshot, ActionDecisionOutcome
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.recovery import RecoveryPolicy
from codex_web.crypto import ManagedKeyCreate, KeyPurpose
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.services.recovery import RecoveryService, LocalBackupDestination
from codex_web.storage.recovery import RecoveryStore
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.recovery_action_provider import RecoveryActionProvider
from codex_web.services.action_intents import ActionIntentConflictError
from codex_web.identity import AuthenticationAssurance
from codex_web.services.identity import AuthenticationError


class RecoveryActionIntentTests(unittest.IsolatedAsyncioTestCase):
    _execute = intent_fixtures.ActionIntentTests._execute

    async def asyncSetUp(self):
        await intent_fixtures.ActionIntentTests.asyncSetUp(self)
        self.worker_actor = self.identity.bootstrap_service_actor(
            identity_id='recovery-fixture-worker',name='Recovery fixture worker',
            scope=self.actor.tenant,service_scopes=('action-intent:worker',))
        root=Path(self.temp.name)
        self.clock=_Clock(1000.)
        crypto=CryptoKeyService(CryptoKeyStore(self.sqlite),{'local':LocalFileKeyBackend(root/'keys')})
        key=crypto.create_key(ManagedKeyCreate(purpose=KeyPurpose.BACKUP,backend_type='local'),actor=self.actor)
        ingestion=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(self.sqlite)))
        self.scheduler=SchedulerService(SchedulerStore(self.sqlite),ingestion,clock=self.clock,owner_id='fixture-scheduler')
        self.recovery=RecoveryService(RecoveryStore(self.sqlite),state_store=self.sqlite,
            crypto=crypto,evidence=self.artifacts,scheduler=self.scheduler,canonical_events=ingestion,
            service_actor=self.actor,destinations=(LocalBackupDestination(root/'backups'),),clock=self.clock)
        self.recovery.configure(RecoveryPolicy(backup_key_id=key.id,destination_id='local',
            backup_interval_seconds=60,restore_verification_interval_seconds=300,
            retention_count=5,require_audit_integrity=False),actor=self.actor)
        # Resolve current authenticated scope/assurance WITHOUT bootstrap/touch
        # writes inside Recovery/ActionIntent transactions. Credentials are
        # synthetic fixture-only and never enter requests, reports or logs.
        credentials=self.identity.create_session(identity_id=self.actor.identity_id,
            scope=self.actor.tenant,assurance=AuthenticationAssurance.MFA)
        def resolve(identity_id):
            current=self.identity.authenticate_session(credentials.session_token,touch=False).actor
            if current.identity_id != identity_id:
                raise AuthenticationError('fixture identity mismatch')
            return current
        self.provider=RecoveryActionProvider(self.recovery,resolve)
        identity_before=self.identity.store.load()
        self.assertEqual(resolve(self.actor.identity_id).assurance,AuthenticationAssurance.MFA)
        self.assertEqual(self.identity.store.load(),identity_before)
        self.registry.register(self.provider)
        self.recovery_binding=self.registry.bind(ActionProviderBindingCreate(
            provider_type='recovery',provider_instance='canonical-recovery'),actor=self.actor,resources=self.resources)
        self.clock.value=1061.
        with patch.object(self.recovery._scheduled_admission,'finish',side_effect=OSError('settlement unavailable')):
            with patch.object(self.recovery._destination('local'),'put',side_effect=OSError('fixture I/O')):
                with self.assertRaisesRegex(OSError,'^settlement unavailable$'):
                    await self.scheduler.run_due()
        await self.recovery.stop()
        original_owner = self.recovery._scheduled_admission.owner_id
        # Fresh actual executor/incarnation after the original writer drained;
        # do not submit new provider work to an already closed executor.
        self.recovery.start()
        self.assertNotEqual(original_owner, self.recovery._scheduled_admission.owner_id)
        self.clock.value=time.time()+10
        firing=next(iter(self.recovery.store.load().scheduled_firings.values()))
        fixture=SimpleNamespace(service=self.recovery,scheduler=self.scheduler,evidence=self.artifacts,actor=self.actor)
        self.payload=recovery_fixtures.RecoveryReconciliationTests.proof(fixture,firing,outcome='unknown')

    async def asyncTearDown(self):
        await self.recovery.stop()
        await intent_fixtures.ActionIntentTests.asyncTearDown(self)

    def intent(self):
        return self.service.create(ActionIntentCreate(binding_id=self.recovery_binding.id,
            request=ActionRequest(action_id='recovery.scheduled-firing.reconcile',organization_id='local',
                workspace_id='default',requested_by=self.actor.identity_id,
                idempotency_key=self.payload.idempotency_key,parameters=self.payload.model_dump(mode='json')),
            policy_decision=ActionDecisionSnapshot(outcome=ActionDecisionOutcome.ALLOW,
                source='policy:fixture',reason='Synthetic scoped qualification')),
            actor=self.actor)

    async def lose_response(self, *, before_commit=False):
        intent=self.intent()
        target=self.provider.reconcile if before_commit else self.provider
        method='reconcile' if before_commit else '_result'
        before=self.recovery.store.load()
        with patch.object(target,method,side_effect=RuntimeError('fixture lost response')) as fault:
            unknown=await self._execute(intent)
        self.assertEqual(fault.call_count,1,unknown.last_error)
        self.assertEqual(unknown.attempt,1)
        self.assertIn(unknown.status,{ActionIntentStatus.UNCERTAIN,ActionIntentStatus.REQUIRES_RECONCILIATION})
        self.assertIn('RuntimeError',unknown.last_error or '')
        if before_commit:
            self.assertEqual(self.recovery.store.load(),before)
        else:
            # A failure anywhere before this commit is NOT the lost-result
            # scenario and must fail setup, rather than yielding vacuous tests.
            self.assertEqual(len(self.recovery.store.load().reconciliations),1,unknown.last_error)
        return unknown

    async def test_committed_domain_receipt_settles_existing_intent_without_execute(self):
        from codex_web.services.action_providers import ActionRequirementError
        before=self.recovery.store.load()
        binding=self.registry.binding(self.recovery_binding.id,self.actor)
        request=ActionRequest(action_id='recovery.scheduled-firing.reconcile',
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            requested_by=self.actor.identity_id,idempotency_key=self.payload.idempotency_key,
            parameters=self.payload.model_dump(mode='json'))
        with self.assertRaisesRegex(ActionRequirementError,'Canonical Recovery mutation executor unavailable'):
            await self.provider.execute(request,binding=binding)
        self.assertEqual(self.recovery.store.load(),before)
        # Positive execution goes through the actual canonical worker and
        # ActionIntent service, never a fabricated process-local context.
        intent=await self.lose_response()
        committed=self.recovery.store.load()
        evidence_before=self.artifacts.store.load()
        with patch.object(self.provider,'execute',side_effect=AssertionError('provider replay')), \
             patch.object(self.provider.reconcile,'reconcile',side_effect=AssertionError('domain replay')):
            result=await self.service.reconcile(intent.id,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
            self.assertEqual(result.status,ActionIntentStatus.SUCCEEDED)
            self.assertEqual(result.attempt,1)
            history=self.service.history(intent.id,self.actor)
            self.assertTrue(history['verifications'][-1]['verified'])
            self.assertEqual(history['receipts'][-1]['details']['operation'],'recovery-state-reconciliation')
            self.assertFalse(history['receipts'][-1]['details']['provider_mutation_replayed'])
            self.assertEqual((await self.service.reconcile(intent.id,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)).status,ActionIntentStatus.SUCCEEDED)
        self.assertEqual(self.recovery.store.load(),committed)
        self.assertEqual(self.artifacts.store.load(),evidence_before)
        self.assertFalse(self.recovery.health(actor=self.actor).recovery_qualified)

    async def test_missing_domain_receipt_stays_unknown_and_never_requeues(self):
        intent=await self.lose_response(before_commit=True)
        self.assertFalse(self.recovery.store.load().reconciliations)
        with patch.object(self.provider,'execute',side_effect=AssertionError('provider replay')):
            result=await self.service.reconcile(intent.id,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
            self.assertEqual(result.status,ActionIntentStatus.REQUIRES_RECONCILIATION)
            with self.assertRaises(ActionIntentConflictError):
                await self.service.reconcile(intent.id,ActionIntentReconcileRequest(retry_if_idempotent=True),actor=self.worker_actor)
        self.assertFalse(self.recovery.store.load().reconciliations)

    async def test_revoked_binding_after_observation_retains_receipt_without_success(self):
        intent=await self.lose_response()
        original=self.provider.verify
        async def observe_then_disable(result,**kwargs):
            observed=await original(result,**kwargs)
            self.registry.set_enabled(self.recovery_binding.id,False,actor=self.actor)
            return observed
        with patch.object(self.provider,'verify',side_effect=observe_then_disable):
            result=await self.service.reconcile(intent.id,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
        self.assertEqual(result.status,ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(len(self.recovery.store.load().reconciliations),1)
        self.assertTrue(any(r['result'] is not None for r in self.service.history(intent.id,self.actor)['receipts']))

    async def test_mismatched_immutable_requests_cannot_claim_another_receipt(self):
        await self.lose_response()
        committed=self.recovery.store.load()
        original_payload=self.payload
        for index, changed in enumerate((
            {'expected_firing_sha256':'0'*64},
            {'expected_policy_fingerprint':'foreign-policy'},
            {'event_id':'foreign-event'},
            {'expected_state_sha256':'0'*64},
        )):
            with self.subTest(changed=changed):
                # A separately admitted canonical intent, never a raw intent
                # rewrite, must not adopt the prior operation's result.
                self.payload=original_payload.model_copy(update={
                    **changed,'idempotency_key':'different-request-'+str(index)})
                intent=await self.lose_response(before_commit=True)
                with patch.object(self.provider,'execute',side_effect=AssertionError('replay')):
                    result=await self.service.reconcile(intent.id,
                        ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
                self.assertEqual(result.status,ActionIntentStatus.REQUIRES_RECONCILIATION)
                self.assertEqual(self.recovery.store.load(),committed)
        self.payload=original_payload

    async def test_requester_membership_revoked_during_verify_cannot_settle(self):
        intent=await self.lose_response()
        original=self.provider.verify
        membership=next(m for m in self.identity.store.load().memberships
            if m.identity_id==self.actor.identity_id and m.organization_id==self.actor.organization_id
            and m.workspace_id==self.actor.workspace_id and m.revoked_at is None)
        async def observe_then_revoke(result,**kwargs):
            observed=await original(result,**kwargs)
            self.identity.revoke_membership(membership.id,actor=self.actor)
            return observed
        committed=self.recovery.store.load()
        with patch.object(self.provider,'verify',side_effect=observe_then_revoke):
            result=await self.service.reconcile(intent.id,
                ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
        self.assertEqual(result.status,ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(self.recovery.store.load(),committed)
        self.assertTrue(any(r['result'] is not None
            for r in self.service.history(intent.id,self.actor)['receipts']))
