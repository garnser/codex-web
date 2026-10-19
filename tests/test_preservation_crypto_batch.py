"""KMS batch pins and final admission; no raw material leaves its service."""
from tests import test_crypto_keys as crypto_fixture
from codex_web.services.crypto_keys import CryptoKeyConflictError, CryptoDecryptError
from codex_web.crypto import ManagedKeyCreate, KeyPurpose
import unittest


class PreservationCryptoBatchTests(unittest.TestCase):
    def setUp(self):
        crypto_fixture.CryptoKeyServiceTests.setUp(self)
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

    def encrypt_one(self):
        ctx=self.context.model_copy(update={'object_id':self.context.object_id+':retained'})
        with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='encrypt') as batch:
            envelope=batch.apply(b'retained-byte-exact',ctx)
        return ctx,envelope

    def successful_batch_audits(self,operation):
        return [e.id for e in self.service.store.load().events
                if e.operation==operation+'_batch' and e.outcome=='success']

    def test_rotated_decrypt_only_version_roundtrips_and_records_exact_version(self):
        from codex_web.crypto import KeyVersionStatus
        ctx,envelope=self.encrypt_one()
        self.service.rotate(self.key.id,actor=self.actor)
        old=self.service._version(self.service.get_key(self.key.id,self.actor),1)
        self.assertEqual(old.status,KeyVersionStatus.DECRYPT_ONLY)
        self.assertEqual(self.service.decrypt(envelope,ctx,actor=self.actor),b'retained-byte-exact')
        before=self.successful_batch_audits('decrypt')
        with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='decrypt',key_version=1) as batch:
            self.assertEqual(batch.apply(envelope,ctx),b'retained-byte-exact')
        events=[e for e in self.service.store.load().events if e.operation=='decrypt_batch' and e.id not in before]
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].key_version,1)
        with self.assertRaisesRegex(CryptoKeyConflictError,'closed'):batch.apply(envelope,ctx)
        # Old retained material is not permission to encrypt with a retired key.
        with self.assertRaises(CryptoKeyConflictError):
            with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='encrypt',key_version=1):
                self.fail('retired encryption admitted')

    def test_rotation_during_active_decrypt_allows_terminal_decrypt_only_audit(self):
        ctx,envelope=self.encrypt_one()
        before=self.successful_batch_audits('decrypt')
        with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='decrypt',key_version=1) as batch:
            self.assertEqual(batch.apply(envelope,ctx),b'retained-byte-exact')
            self.service.rotate(self.key.id,actor=self.actor)
        self.assertEqual(len(self.successful_batch_audits('decrypt')),len(before)+1)

    def test_rotation_during_encrypt_refuses_terminal_audit(self):
        ctx=self.context.model_copy(update={'object_id':self.context.object_id+':rotation'})
        before=self.successful_batch_audits('encrypt')
        with self.assertRaises(CryptoKeyConflictError):
            with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='encrypt') as batch:
                batch.apply(b'must-not-publish',ctx)
                self.service.rotate(self.key.id,actor=self.actor)
        self.assertEqual(self.successful_batch_audits('encrypt'),before)

    def test_revoked_key_or_selected_old_version_refuses_entry_and_terminal_audit(self):
        from codex_web.crypto import ManagedKeyCreate,KeyPurpose
        # Distinct genuine keys make each revocation's initial and final gate
        # independent. No consumer exists, so the real lifecycle allows revoke.
        for whole_key in (False,True):
            with self.subTest(whole_key=whole_key):
                self.key=self.service.create_key(ManagedKeyCreate(project_id='project-a',purpose=KeyPurpose.DECISION),actor=self.actor)
                ctx,envelope=self.encrypt_one()
                self.service.rotate(self.key.id,actor=self.actor)
                before=self.successful_batch_audits('decrypt')
                with self.assertRaises(CryptoKeyConflictError):
                    with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='decrypt',key_version=1) as batch:
                        self.assertEqual(batch.apply(envelope,ctx),b'retained-byte-exact')
                        if whole_key:self.service.revoke_key(self.key.id,'synthetic terminal revoke',actor=self.actor)
                        else:self.service.revoke_version(self.key.id,1,'synthetic terminal revoke',actor=self.actor)
                self.assertEqual(self.successful_batch_audits('decrypt'),before)
                with self.assertRaisesRegex(CryptoKeyConflictError,'closed'):batch.apply(envelope,ctx)
                with self.assertRaises(CryptoKeyConflictError):
                    with self.service.envelope_batch(self.key.id,self.context,actor=self.actor,operation='decrypt',key_version=1):
                        self.fail('revoked material admitted')
