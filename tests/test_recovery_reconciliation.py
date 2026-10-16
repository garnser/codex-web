"""Actual Scheduler/Recovery/Evidence and independent SQLite connections; fixtures only."""
import asyncio
import copy
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from codex_web.artifact_evidence import EvidenceCreate, EvidenceType, EvidenceResult, VerificationCreate, VerificationResult
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.recovery import RecoveryReconcileRequest
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_reconciliation import RecoveryReconciliationService, fingerprint, PROOF_CONTRACT
from tests import test_recovery as fixtures


class RecoveryReconciliationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledRecoveryTests.setUp
    tearDown = fixtures.ScheduledRecoveryTests.tearDown
    asyncTearDown = fixtures.ScheduledRecoveryTests.asyncTearDown

    async def orphan(self, failed=False):
        self.clock.value = 1061.0
        destination = self.service._destination('local')
        # Actual admission/effect path. Only the known I/O or settlement failure
        # is injected; no raw receipt insert or safety predicate is bypassed.
        with patch.object(self.service._scheduled_admission,'finish',side_effect=OSError('settlement unavailable')):
            # Scheduler awaits the admitted executor and propagates its terminal
            # failure. Assert that exact failure before inspecting retained state;
            # swallowing arbitrary exceptions would hide a broken admission path.
            with self.assertRaisesRegex(OSError, '^settlement unavailable$'):
                if failed:
                    with patch.object(destination,'put',side_effect=OSError('fixture destination failure')):
                        await self.scheduler.run_due()
                else:
                    await self.scheduler.run_due()
        state=self.service.store.load()
        firing=next(iter(state.scheduled_firings.values()))
        self.assertEqual(firing.status,'running')
        self.assertEqual(self.service._scheduled_executor.status()['inflight_work'],0)
        self.assertEqual(self.service._scheduled_executor.status()['terminal_observations'][-1]['error_class'],'OSError')
        await self.service.stop()
        self.clock.value=time.time()+10
        return firing

    def proof(self,firing,*,outcome='failed',extra=None):
        state=self.service.store.load();schedule=self.scheduler.store.load().schedules[firing.schedule_id]
        metadata={'contract':PROOF_CONTRACT,'event_id':firing.event_id,'owner_id':firing.owner_id,
            'effect_id':firing.effect_id,'firing_sha256':fingerprint(firing),
            'schedule_sha256':fingerprint(schedule),'policy_fingerprint':state.policy.fingerprint(),
            'writers_quiescent':True,'effects_settled':True,'retention_settled':True,'effect_outcome':outcome}
        metadata.update(extra or {})
        evidence=self.evidence.create_evidence(EvidenceCreate(evidence_type=EvidenceType.RUNTIME_RESULT,
            result=EvidenceResult.PASS,observed_at=time.time(),metadata=metadata),actor=self.actor)
        verifier=self.actor.model_copy(update={'identity_id':'independent-fixture-verifier'})
        verification=self.evidence.create_verification(VerificationCreate(evidence_ids=(evidence.id,),
            method=PROOF_CONTRACT,result=VerificationResult.VERIFIED),actor=verifier)
        return RecoveryReconcileRequest(event_id=firing.event_id,expected_firing_sha256=fingerprint(firing),
            expected_schedule_sha256=fingerprint(schedule),expected_policy_fingerprint=state.policy.fingerprint(),
            evidence_id=evidence.id,verification_id=verification.id,idempotency_key='fixture-'+evidence.id)

    def second_service(self):
        connection=SQLiteStateStore(Path(self.temp.name)/'state.sqlite3')
        second=copy.copy(self.service);second.store=RecoveryStore(connection)
        second.crypto=copy.copy(self.crypto);second.crypto.store=CryptoKeyStore(connection)
        second.evidence=ArtifactEvidenceService(ArtifactEvidenceStore(connection))
        second.scheduler=copy.copy(self.scheduler);second.scheduler.store=SchedulerStore(connection)
        return second

    async def test_known_failed_orphan_preserves_identity_and_needs_actual_fresh_restore(self):
        firing=await self.orphan(failed=True);payload=self.proof(firing)
        before=self.service.store.load();sentinel={'business_paused':True,'held_input':'never-replay','usage':123}
        self.state.put('fixture-custody-sentinel',sentinel)
        record=RecoveryReconciliationService(self.service,lambda identity:self.actor).reconcile(payload,actor=self.actor)
        state=self.service.store.load();self.assertEqual(record.original,firing)
        self.assertEqual(state.scheduled_firings[firing.event_id].status,'unknown')
        self.assertEqual(state.scheduled_watermarks,before.scheduled_watermarks)
        self.assertEqual(state.backups,before.backups)
        self.assertEqual(self.state.get('fixture-custody-sentinel'),sentinel)
        self.assertFalse(self.service.health(actor=self.actor).recovery_qualified)
        self.clock.value+=1;backup=self.service.create_backup(actor=self.actor)
        self.assertFalse(self.service.health(actor=self.actor).recovery_qualified)
        self.clock.value+=1;self.service.verify_restore(backup.id,actor=self.actor,publish_evidence=True)
        self.assertTrue(self.service.health(actor=self.actor).recovery_qualified)
        self.assertEqual(self.service.store.load().scheduled_firings[firing.event_id].effect_id,firing.effect_id)

    async def test_unknown_foreign_active_or_stale_proof_refuses_without_effect_replay(self):
        firing=await self.orphan(failed=True);engine=RecoveryReconciliationService(self.service,lambda identity:self.actor)
        for extra in [{'effect_outcome':'unknown'},{'writers_quiescent':False},{'owner_id':'foreign'},
                {'effect_id':'foreign'},{'writers_quiescent':1},{'firing_sha256':'f'*64}]:
            with self.subTest(extra=extra):
                payload=self.proof(firing,extra=extra)
                with self.assertRaises(RecoveryConflictError):engine.reconcile(payload,actor=self.actor)
                self.assertEqual(self.service.store.load().scheduled_firings[firing.event_id],firing)
        payload=self.proof(firing)
        with self.assertRaises(RecoveryConflictError):
            engine.reconcile(payload.model_copy(update={'expected_firing_sha256':'0'*64}),actor=self.actor)
        # No remote-owner inference from local queue0: it still needs exact
        # independent proof. Active original local owner must remain refused.
        self.service._scheduled_executor._closed=False
        with self.assertRaisesRegex(RecoveryConflictError,'not drained'):
            engine.reconcile(payload,actor=self.actor)
        self.service._scheduled_executor.close_admission()

    async def test_separate_connection_cas_and_lost_response_never_repeat_effect(self):
        firing=await self.orphan();payload=self.proof(firing,outcome='succeeded')
        engines=[RecoveryReconciliationService(self.service,lambda identity:self.actor),RecoveryReconciliationService(self.second_service(),lambda identity:self.actor)]
        before=self.service.store.load().backups
        def operation(engine):return engine.reconcile(payload,actor=self.actor)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(operation,engines))
        self.assertEqual(results[0],results[1]);self.assertEqual(len(self.service.store.load().reconciliations),1)
        self.assertEqual(self.service.store.load().backups,before)
        # Model response loss after committed operation; read-only receipt path.
        def lost_response():
            engines[0].reconcile(payload,actor=self.actor)
            raise TimeoutError('fixture response loss after commit')
        with self.assertRaises(TimeoutError):lost_response()
        self.assertEqual(engines[1].inspect(firing.event_id,actor=self.actor),results[0])
        self.assertEqual(engines[1].reconcile(payload,actor=self.actor),results[0])
        with self.assertRaises(RecoveryConflictError):
            self.service._scheduled_admission.finish(firing,succeeded=True)
        self.assertEqual(self.service.store.load().backups,before)
        self.assertFalse(self.service.health(actor=self.actor).recovery_qualified)

    async def test_evidence_invalidation_and_nonindependence_remain_blocked(self):
        firing=await self.orphan(failed=True);payload=self.proof(firing)
        self_verification=self.evidence.create_verification(VerificationCreate(
            evidence_ids=(payload.evidence_id,),method=PROOF_CONTRACT,result=VerificationResult.VERIFIED),actor=self.actor)
        self.assertFalse(self_verification.independent)
        with self.assertRaises(RecoveryConflictError):
            RecoveryReconciliationService(self.service,lambda identity:self.actor).reconcile(
                payload.model_copy(update={"verification_id":self_verification.id}),actor=self.actor)
        # Real Evidence lifecycle operation; no direct state tampering.
        self.evidence.invalidate_evidence(payload.evidence_id,actor=self.actor,reason="fixture withdrawn")
        with self.assertRaises(RecoveryConflictError):
            RecoveryReconciliationService(self.service,lambda identity:self.actor).reconcile(payload,actor=self.actor)

    async def test_provider_uses_canonical_receipt_verification_without_effect_replay(self):
        from codex_web.action_providers import ActionRequest,ActionProviderBinding
        from codex_web.services.recovery_action_provider import RecoveryActionProvider
        firing=await self.orphan(failed=True);payload=self.proof(firing)
        provider=RecoveryActionProvider(self.service,lambda identity:self.actor)
        binding=ActionProviderBinding(id="recovery-fixture",organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,provider_type="recovery",provider_instance="canonical-recovery")
        request=ActionRequest(action_id="recovery.scheduled-firing.reconcile",organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,requested_by=self.actor.identity_id,
            idempotency_key=payload.idempotency_key,parameters=payload.model_dump(mode="json"))
        await provider.prepare(request,binding=binding)
        result=await provider.execute(request,binding=binding)
        verification=await provider.verify(result,binding=binding)
        self.assertTrue(verification.verified)
        self.assertEqual((await provider.execute(request,binding=binding)).external_id,result.external_id)
        self.assertEqual(len(self.service.store.load().reconciliations),1)

    async def test_current_operator_revocation_and_contract_downgrade_refuse(self):
        from codex_web.identity import MembershipRole
        from codex_web.services.identity import AuthorizationError
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        firing=await self.orphan(failed=True);payload=self.proof(firing)
        current=self.actor
        def resolve(identity):return current
        engine=RecoveryReconciliationService(self.service,resolve)
        current=self.actor.model_copy(update={"roles":(MembershipRole.MEMBER,)})
        with self.assertRaises(AuthorizationError):engine.reconcile(payload,actor=self.actor)
        self.assertEqual(self.service.store.load().scheduled_firings[firing.event_id],firing)
        current=self.actor
        engine.reconcile(payload,actor=self.actor)
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec("recovery-state","1.2",("1.2",)).require(self.service.store.load().schema_version)
