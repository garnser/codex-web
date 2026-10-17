import unittest
from pydantic import ValidationError
from codex_web.recovery import BackupSnapshot
from codex_web.storage.storage_preservation import StateStorageSnapshotV1, canonical


class StoragePreservationCompatibilityTests(unittest.TestCase):
    def test_legacy_semantic_bytes_unchanged_and_future_version_refused(self):
        payload = dict(format_version='1.0',organization_id='org',workspace_id='ws',
                       state_backend='sqlite',state_schema_version=1,created_at=1.0,
                       documents={'opaque_valid':{'unknown':'é','number':1.0}},
                       documents_checksum='a'*64,key_manifest=[])
        legacy = BackupSnapshot.model_validate(payload)
        import json
        expected = json.dumps({**payload,'audit_root_hash':None,'audit_checkpoint_id':None},
                              sort_keys=True,separators=(',',':')).encode('utf8')
        self.assertEqual(canonical(legacy.model_dump(mode='json')),expected)
        with self.assertRaises(ValidationError):
            BackupSnapshot.model_validate({**payload,'format_version':'99.0'})

    def test_preservation_not_accepted_as_semantic_snapshot(self):
        raw = StateStorageSnapshotV1(backend='sqlite',storage_schema_version=1).model_dump(mode='json')
        self.assertNotIn('documents',raw)
        with self.assertRaises(ValidationError):
            BackupSnapshot.model_validate(raw)
        with self.assertRaises(ValidationError):
            StateStorageSnapshotV1.model_validate({**raw,'format_version':'2.0'})
