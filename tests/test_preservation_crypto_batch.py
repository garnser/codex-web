"""KMS batch pins and final admission; no raw material leaves its service."""
from tests.test_crypto_keys import CryptoKeyServiceTests
from codex_web.services.crypto_keys import CryptoKeyConflictError, CryptoDecryptError
from codex_web.crypto import ManagedKeyCreate, KeyPurpose
import unittest


class PreservationCryptoBatchTests(unittest.TestCase):
    def setUp(self):
        CryptoKeyServiceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.key = self.service.create_key(ManagedKeyCreate(project_id='project-a',purpose=KeyPurpose.DECISION),actor=self.actor)

    def test_same_envelopes_roundtrip_one_terminal_batch_audit(self):
        ctx = self.context.model_copy(update={'object_id':self.context.object_id+':0'})
        before = len(self.service.store.load().events)
        with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='encrypt') as batch:
            envelope = batch.apply(b'opaque-not-json',ctx)
            # Deferred audited store mutation solves source cursor/pool1 seam.
            self.assertEqual(len(self.service.store.load().events),before)
        self.assertEqual(len(self.service.store.load().events),before+1)
        self.assertEqual(self.service.decrypt(envelope,ctx,actor=self.actor),b'opaque-not-json')
        with self.assertRaisesRegex(CryptoKeyConflictError,'closed'):
            batch.apply(b'late',ctx)

    def test_revocation_before_batch_commit_refuses_original_current_key(self):
        ctx = self.context.model_copy(update={'object_id':self.context.object_id+':0'})
        with self.assertRaises(CryptoKeyConflictError):
            with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='encrypt') as batch:
                batch.apply(b'opaque',ctx)
                self.service.revoke_key(self.key.id,'synthetic batch revocation',actor=self.actor)

    def test_foreign_context_or_key_cannot_be_rewrapped(self):
        wrong = self.context.model_copy(update={'object_id':'foreign:0'})
        with self.assertRaises(CryptoKeyConflictError):
            with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='encrypt') as batch:
                batch.apply(b'opaque',wrong)
