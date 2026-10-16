"""Final write races against canonical SQLite services; no sleeps or live state."""
import unittest
from codex_web.artifact_evidence import EvidenceResult
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.crypto import KeyStatus
from codex_web.services.recovery import RecoveryConflictError
from tests.test_recovery import RecoveryTests

class FinalSettlementTests(RecoveryTests):
    def setUp(self):
        super().setUp()
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        self.service.evidence = self.evidence

    def race(self, mutate, lost=False):
        backup = self.service.create_backup(actor=self.actor)
        writer = SQLiteStateStore(self.service.store.store.path)
        original = self.evidence.create_evidence
        calls = []
        def gated(payload, **kwargs):
            calls.append(payload)
            mutate(writer)
            result = original(payload, **kwargs)
            if lost:
                raise ConnectionError("synthetic response lost")
            return result
        self.evidence.create_evidence = gated
        with self.assertRaises((RecoveryConflictError, ConnectionError)):
            self.service.verify_restore(backup.id, actor=self.actor)
        self.assertEqual(len(calls), 1)
        self.assertFalse([e for e in self.evidence.list_evidence(actor=self.actor) if e.result == EvidenceResult.PASS])
        self.assertFalse(self.service.store.load().verifications)

    def revoke(self, writer):
        from codex_web.services.crypto_keys import CryptoKeyService
        CryptoKeyService(CryptoKeyStore(writer), self.crypto.backends).revoke_key(
            self.backup_key.id, "synthetic final-write race", actor=self.actor)

    def test_required_backup_key_revoked_at_actual_evidence_write(self):
        self.race(self.revoke)

    def test_required_key_revocation_lost_response_path(self):
        self.race(self.revoke, lost=True)
