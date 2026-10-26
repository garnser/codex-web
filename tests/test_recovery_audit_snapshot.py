"""Real backup/restore race and audit corruption at the capture boundary."""
import unittest
from unittest.mock import patch

from codex_web.autonomy_audit import AuditIntegrityStatus, AutonomyAuditKind, AutonomyAuditPayload
from codex_web.recovery import RestoreVerificationStatus
from codex_web.services.recovery import RecoveryConflictError
from codex_web.storage.state_store import state_documents_checksum
from codex_web.services.recovery_snapshot_capture import SnapshotCapture
from tests.test_autonomy_audit import _Signer
from tests import test_recovery as fixture


class RecoveryAuditSnapshotTests(unittest.TestCase):
    setUp = fixture.RecoveryTests.setUp
    tearDown = fixture.RecoveryTests.tearDown

    def integrity(self):
        return self.audit.verify(organization_id="org-a", workspace_id="ws-a")

    def append(self):
        self.audit_store.append(AutonomyAuditPayload(
            kind=AutonomyAuditKind.INTEGRITY_VERIFICATION,
            organization_id="org-a", workspace_id="ws-a", outcome="verified",
            reason_code="concurrent_valid_append",
        ))

    def test_valid_append_and_checkpoint_after_export_keep_exact_snapshot_anchor(self):
        self.audit.signer = _Signer()
        self.audit.checkpoint(organization_id="org-a", workspace_id="ws-a")
        for checkpoint in (False, True):
            with self.subTest(checkpoint=checkpoint):
                frozen = self.integrity()

                snapshot_checksum = SnapshotCapture.checksum
                def concurrent_write(documents):
                    checksum = snapshot_checksum(documents)
                    self.append()
                    if checkpoint:
                        self.audit.checkpoint(organization_id="org-a", workspace_id="ws-a")
                    return checksum

                with patch.object(SnapshotCapture, "checksum", new=concurrent_write):
                    manifest = self.service.create_backup(actor=self.actor)
                current = self.integrity()
                self.assertEqual(current.status, AuditIntegrityStatus.VERIFIED)
                self.assertNotEqual(current.root_hash, frozen.root_hash)
                verification = self.service.verify_restore(manifest.id, actor=self.actor)
                self.assertEqual(verification.status, RestoreVerificationStatus.PASS, verification.blockers)
                self.assertEqual(manifest.audit_root_hash, frozen.root_hash)
                self.assertEqual(manifest.audit_checkpoint_id, frozen.checkpoint_id)
                self.assertTrue(verification.audit_continuity_valid)
                self.assertTrue(verification.checksum_valid)

    def test_corrupt_live_audit_after_valid_export_still_refuses_backup(self):
        snapshot_checksum = SnapshotCapture.checksum
        def corrupt_live(documents):
            checksum = snapshot_checksum(documents)
            raw = self.audit_store.load().model_dump(mode="json")
            raw["records"][0]["payload"]["reason_code"] = "tampered"
            self.state.put("autonomy_audit", raw)
            return checksum

        with patch.object(SnapshotCapture, "checksum", new=corrupt_live):
            with self.assertRaises(RecoveryConflictError):
                self.service.create_backup(actor=self.actor)
        self.assertEqual(self.integrity().status, AuditIntegrityStatus.FAILED)
        self.assertFalse(self.service.store.load().backups)
        self.assertFalse(list(self.destination.directory.iterdir()))

    def test_first_valid_append_after_absent_audit_export_preserves_absent_anchor(self):
        self.state.delete("autonomy_audit")

        snapshot_checksum = SnapshotCapture.checksum
        def concurrent_first_append(documents):
            self.assertIsNone(documents.get("autonomy_audit"))
            checksum = snapshot_checksum(documents)
            self.append()
            return checksum

        with patch.object(SnapshotCapture, "checksum", new=concurrent_first_append):
            manifest = self.service.create_backup(actor=self.actor)
        self.assertEqual(self.integrity().status, AuditIntegrityStatus.VERIFIED)
        self.assertIsNone(manifest.audit_root_hash)
        self.assertIsNone(manifest.audit_checkpoint_id)
        verification = self.service.verify_restore(manifest.id, actor=self.actor)
        self.assertEqual(verification.status, RestoreVerificationStatus.PASS)
        self.assertTrue(verification.audit_continuity_valid)

    def test_corrupt_snapshot_payload_refuses_even_when_live_audit_is_valid(self):
        read_items = SnapshotCapture.record_items
        def corrupt_snapshot(capture, namespace, keys):
            for key, row in read_items(capture, namespace, keys):
                if capture.only_namespace is None and namespace == 'autonomy_audit' and key.startswith('record:'):
                    row['payload']['reason_code'] = 'tampered snapshot'
                yield key, row
        with patch.object(SnapshotCapture, 'record_items', new=corrupt_snapshot):
            with self.assertRaises(RecoveryConflictError):
                self.service.create_backup(actor=self.actor)
        self.assertEqual(self.integrity().status, AuditIntegrityStatus.VERIFIED)
        self.assertFalse(self.service.store.load().backups)
        self.assertFalse(list(self.destination.directory.iterdir()))

    def test_corrupt_snapshot_checkpoint_signature_refuses_backup(self):
        self.audit.signer = _Signer()
        self.audit.checkpoint(organization_id="org-a", workspace_id="ws-a")
        read_items = SnapshotCapture.record_items
        def corrupt_snapshot(capture, namespace, keys):
            for key, row in read_items(capture, namespace, keys):
                if capture.only_namespace is None and namespace == 'autonomy_audit' and key == 'checkpoints':
                    row[0]['signature'] = 'tampered signature'
                yield key, row
        with patch.object(SnapshotCapture, 'record_items', new=corrupt_snapshot):
            with self.assertRaises(RecoveryConflictError):
                self.service.create_backup(actor=self.actor)
        self.assertEqual(self.integrity().status, AuditIntegrityStatus.VERIFIED)
        self.assertFalse(self.service.store.load().backups)

    def test_live_verification_keeps_records_and_checkpoints_from_one_read(self):
        frozen = self.integrity()
        load = self.audit_store.load
        first = True

        def append_after_read():
            nonlocal first
            state = load()
            if first:
                first = False
                self.append()
                self.audit.checkpoint(organization_id="org-a", workspace_id="ws-a")
            return state

        with patch.object(self.audit_store, "load", side_effect=append_after_read):
            result = self.integrity()
        self.assertEqual(result.status, AuditIntegrityStatus.VERIFIED)
        self.assertEqual(result.root_hash, frozen.root_hash)
        self.assertNotEqual(self.integrity().root_hash, frozen.root_hash)
