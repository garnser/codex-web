"""Canonical final-write races. Synchronisation gates precede transaction entry."""
import copy
import unittest
from codex_web.artifact_evidence import EvidenceResult
from codex_web.crypto import KeyPurpose, KeyStatus, KeyVersionStatus, ManagedKeyCreate
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.crypto_keys import CryptoKeyService, CryptoKeyConflictError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_recovery as fixtures


class CanonicalSettlementMixin:
    def writer_store(self):
        return SQLiteStateStore(self.state.path)

    def writer_service(self):
        # Same canonical services, a separate actual backend connection facade.
        writer = self.writer_store()
        service = copy.copy(self.service)
        service.store = RecoveryStore(writer)
        service.state_store = writer
        service.crypto = CryptoKeyService(CryptoKeyStore(writer), self.crypto.backends)
        service.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(writer))
        service.scheduler = copy.copy(self.scheduler) if hasattr(self, "scheduler") else None
        if service.scheduler is not None:
            service.scheduler.store = SchedulerStore(writer)
        return service

    def assert_link(self, verification):
        persisted = self.service.store.load().verifications[verification.id]
        self.assertEqual(persisted, verification)
        evidence = self.evidence.list_evidence(actor=self.actor)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(verification.evidence_id, evidence[0].id)
        self.assertEqual(evidence[0].metadata["verification_id"], verification.id)
        self.assertEqual(evidence[0].result, EvidenceResult.PASS)

    def at_commit(self, action, *, lose_after=False, fail_prepare=False):
        original = self.evidence.create_evidence
        self.commit_calls = 0
        def gated(payload, **kwargs):
            commit = kwargs["_commit"]
            def final(apply):
                self.commit_calls += 1
                action()
                def prepare(state):
                    updated = apply(state)
                    if fail_prepare:
                        raise RuntimeError("synthetic prepared mutation failure")
                    return updated
                try:
                    commit(prepare)
                except RecoveryConflictError as exc:
                    if lose_after:
                        raise ConnectionError("synthetic lost rejection response") from exc
                    raise
                if lose_after:
                    raise ConnectionError("synthetic lost response after committed success")
            return original(payload, **{**kwargs, "_commit": final})
        self.evidence.create_evidence = gated


class FinalSettlementTests(CanonicalSettlementMixin, unittest.TestCase):
    tearDown = fixtures.RecoveryTests.tearDown
    def setUp(self):
        fixtures.RecoveryTests.setUp(self)
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        self.service.evidence = self.evidence

    def test_returned_and_persisted_evidence_pointer(self):
        backup = self.service.create_backup(actor=self.actor)
        verification = self.service.verify_restore(backup.id, actor=self.actor)
        self.assert_link(verification)

    def check_auxiliary_consumer_guard(self, version=False):
        key = self.crypto.create_key(ManagedKeyCreate(
            purpose=KeyPurpose.APPLICATION_DATA, backend_type="local"), actor=self.actor)
        if version:
            self.crypto.rotate(key.id, actor=self.actor)
        backup = self.service.create_backup(actor=self.actor)
        required = next(e for e in backup.key_manifest if e.key_id == key.id)
        self.assertIn(1, required.versions)
        if version:
            self.assertIn(2, required.versions)
        writer = self.writer_service()
        def attempt():
            # These are DENIED genuine service calls, not successful revocations.
            with self.assertRaises(CryptoKeyConflictError):
                if version:
                    writer.crypto.revoke_version(key.id, 1, "synthetic race", actor=self.actor)
                else:
                    writer.crypto.revoke_key(key.id, "synthetic race", actor=self.actor)
        self.at_commit(attempt)
        verification = self.service.verify_restore(backup.id, actor=self.actor)
        self.assert_link(verification)
        self.assertEqual(self.commit_calls, 1)
        current = writer.crypto.get_key(key.id, self.actor)
        self.assertEqual(current.status, KeyStatus.ACTIVE)
        if version:
            self.assertEqual(next(v.status for v in current.versions if v.version == 1), KeyVersionStatus.DECRYPT_ONLY)
        self.assertEqual(writer.crypto.get_key(self.backup_key.id, self.actor).status, KeyStatus.ACTIVE)

    def test_auxiliary_key_revocation_denied_by_frozen_backup_consumer(self):
        self.check_auxiliary_consumer_guard()

    def test_noncurrent_required_version_revocation_denied_by_frozen_backup_consumer(self):
        self.check_auxiliary_consumer_guard(version=True)

    def test_failed_prepared_append_rolls_back_both_documents(self):
        backup = self.service.create_backup(actor=self.actor)
        self.at_commit(lambda: None, fail_prepare=True)
        with self.assertRaises(RuntimeError):
            self.service.verify_restore(backup.id, actor=self.actor)
        self.assertFalse(self.service.store.load().verifications)
        self.assertFalse(self.evidence.list_evidence(actor=self.actor))


class ScheduledFinalSettlementTests(CanonicalSettlementMixin, unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledRecoveryTests.setUp
    tearDown = fixtures.ScheduledRecoveryTests.tearDown
    asyncTearDown = fixtures.ScheduledRecoveryTests.asyncTearDown

    async def asyncSetUp(self):
        self.service._unsubscribe()
        self.events = []
        self.bus.subscribe(self.events.append)
        self.backup = self.service.create_backup(actor=self.actor)
        self.clock.value = 1301
        await self.scheduler.run_due()
        self.event = next(e for e in self.events if e.payload['trigger_type'] == self.service.VERIFY_TRIGGER)
        self.admission = self.service._scheduled_admission
        self.firing = self.admission.admit(self.event)
        self.assertIsNotNone(self.firing)

    def verify(self):
        return self.service.verify_restore(self.backup.id, actor=self.actor, _firing=self.firing)

    def assert_unknown_no_replay(self):
        state = self.service.store.load()
        self.assertFalse(state.verifications)
        self.assertFalse(self.evidence.list_evidence(actor=self.actor))
        self.admission.finish(self.firing, succeeded=False, error_code="synthetic final-write conflict")
        self.assertEqual(self.service.store.load().scheduled_firings[self.event.event_id].status, "unknown")
        self.assertIsNone(self.admission.admit(self.event))
        self.assertEqual(self.commit_calls, 1)

    def check_revocation(self, policy=False, lose=False):
        writer = self.writer_service()
        def revoke():
            if policy:
                old = writer.policy()
                writer.configure(old.model_copy(update={"retention_count":old.retention_count+1}),
                    actor=self.actor, expected_fingerprint=old.fingerprint())
            else:
                writer.scheduler.pause(self.firing.schedule_id, actor_id=self.actor.identity_id)
        self.at_commit(revoke, lose_after=lose)
        with self.assertRaises(ConnectionError if lose else RecoveryConflictError):
            self.verify()
        self.assert_unknown_no_replay()

    async def test_schedule_revoked_before_final_evidence_write(self):
        self.check_revocation()

    async def test_schedule_revoked_before_write_with_lost_response_requested(self):
        self.check_revocation(lose=True)

    async def test_policy_changed_before_final_evidence_write(self):
        self.check_revocation(policy=True)

    async def test_policy_changed_before_write_with_lost_response_requested(self):
        self.check_revocation(policy=True, lose=True)

    async def test_success_pointer_and_terminal_firing_share_commit(self):
        self.at_commit(lambda: None)
        verification = self.verify()
        self.assert_link(verification)
        self.assertEqual(self.service.store.load().scheduled_firings[self.event.event_id].status, "succeeded")
        self.admission.finish(self.firing, succeeded=True)
        self.assertIsNone(self.admission.admit(self.event))

    async def test_lost_response_after_valid_commit_retains_exact_success(self):
        self.at_commit(lambda: None, lose_after=True)
        with self.assertRaises(ConnectionError):
            self.verify()
        # Read-only reconciliation of the immutable firing effect identity.
        verification = self.service.store.load().verifications[self.firing.effect_id]
        self.assert_link(verification)
        self.admission.finish(self.firing, succeeded=False, error_code="observer_response_lost")
        self.assertEqual(self.service.store.load().scheduled_firings[self.event.event_id].status, "succeeded")
        self.assertIsNone(self.admission.admit(self.event))
        self.assertEqual(self.commit_calls, 1)

    async def test_failed_preparation_keeps_unknown_diagnostic_and_no_pass(self):
        self.at_commit(lambda: None, fail_prepare=True)
        with self.assertRaises(RuntimeError):
            self.verify()
        self.assert_unknown_no_replay()
