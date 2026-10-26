"""Physical-row coverage remains complete when canonical audit reads are reused."""
from collections import Counter
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_web.autonomy_audit import (
    AuditIntegrityStatus, AutonomyAuditKind, AutonomyAuditPayload,
    AutonomyAuditRecord, AutonomyAuditState, GENESIS_HASH,
)
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.recovery_snapshot_capture import SnapshotCapture
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import state_documents_checksum, state_record_storage_key


class LiveAuditReadCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteStateStore(Path(self.tmp.name) / 'fixture.sqlite3')
        self.audit = AutonomyAuditService(AutonomyAuditStore(self.state))
        previous, self.rows = {}, []
        for index, org in enumerate(('org-a', 'org-b', 'org-a', 'org-b', 'org-a')):
            old = previous.get(org)
            item = AutonomyAuditRecord.build(AutonomyAuditPayload(
                kind=AutonomyAuditKind.INTEGRITY_VERIFICATION,
                organization_id=org, workspace_id='ws-a', occurred_at=float(index + 1),
                reason_code='coverage', details={'unicode': '雪/🚀'}),
                sequence=old.sequence + 1 if old else 1,
                previous_hash=old.record_hash if old else GENESIS_HASH,
                created_at=float(index + 1))
            previous[org] = item
            self.rows.append(item)
        self.raw = AutonomyAuditStore._encode(AutonomyAuditState(records=self.rows))
        self.write()

    def write(self):
        self.state.record_replace(AutonomyAuditStore.namespace, self.raw)

    def verify(self, capture):
        return capture.audit_integrity(self.audit, AutonomyAuditStore.namespace,
                                       organization_id='org-a', workspace_id='ws-a')

    def test_exact_read_coverage_and_canonical_bytes(self):
        self.raw['ignored-but-json-validated'] = {'v': 'é'}
        self.write()
        expected = self.audit.verify_snapshot(self.raw, organization_id='org-a', workspace_id='ws-a')
        documents = self.state.documents()
        counts = Counter()
        original = SnapshotCapture.values
        def counted(capture, keys):
            keys = tuple(keys)
            counts.update(keys)
            yield from original(capture, keys)
        with patch.object(SnapshotCapture, 'values', new=counted):
            with SnapshotCapture(self.state, lambda *_: None,
                                 only_namespace=AutonomyAuditStore.namespace) as capture:
                result = self.verify(capture)
                self.assertTrue(capture._validated_all)
        self.assertEqual(result.model_dump(exclude={'verified_at'}),
                         expected.model_dump(exclude={'verified_at'}))
        for key, value in self.raw.items():
            physical = state_record_storage_key(AutonomyAuditStore.namespace, key)
            selected = key.startswith('record:') and value['partition_id'] == self.rows[0].partition_id
            self.assertEqual(counts[physical], 2 if selected else 1, key)
        # Full capture still serializes/checksums the exact original documents.
        metadata = {'created_at': 1., 'format': 'unchanged'}
        with SnapshotCapture(self.state, lambda *_: None) as capture:
            self.verify(capture)
            self.assertEqual(capture.checksum(), state_documents_checksum(documents))
            self.assertEqual(capture.plaintext(metadata), json.dumps(
                {**metadata, 'documents': documents}, sort_keys=True,
                separators=(',', ':')).encode())

    def test_malformed_ignored_selected_row_still_refuses(self):
        key = state_record_storage_key(AutonomyAuditStore.namespace, 'ignored')
        with self.state._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                               (key, '{malformed'))
        with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
            with self.assertRaises(ValueError):
                self.verify(capture)
            self.assertFalse(getattr(capture, '_validated_all', False))

    def test_malformed_shadowed_physical_alias_still_refuses(self):
        key = state_record_storage_key(AutonomyAuditStore.namespace, 'record:00000000000000000000')
        alias = key.replace('/k/record', '/k/%72ecord')
        self.assertNotEqual(alias, key)
        with self.state._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                               (alias, '{malformed'))
        with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
            with self.assertRaises(ValueError):
                self.verify(capture)

    def test_foreign_schema_invalid_record_still_refuses_before_filter(self):
        foreign = next(key for key, value in self.raw.items()
                       if key.startswith('record:') and value['partition_id'] != self.rows[0].partition_id)
        self.raw[foreign]['unexpected_field'] = True
        self.write()
        with self.assertRaises(ValueError):
            self.audit.verify_snapshot(self.raw, organization_id='org-a', workspace_id='ws-a')
        with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
            with self.assertRaises(ValueError):
                self.verify(capture)
            self.assertFalse(getattr(capture, '_validated_all', False))

    def test_invalid_control_marker_still_refuses(self):
        self.raw[AutonomyAuditStore.meta_key]['schema_version'] = 'unsupported'
        self.write()
        with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
            with self.assertRaises(ValueError):
                self.verify(capture)

    def test_legacy_fallback_retains_full_validation(self):
        legacy = AutonomyAuditState(records=self.rows).model_dump(mode='json')
        self.state.put(AutonomyAuditStore.namespace, legacy)
        expected = self.audit.verify_snapshot(legacy, organization_id='org-a', workspace_id='ws-a')
        for singleton in (False, True):
            with self.subTest(physical_singleton=singleton):
                if singleton:
                    # Retain the valid keyed legacy rows as shadowed data;
                    # a genuine physical singleton must take precedence.
                    with self.state._connection() as connection:
                        connection.execute('INSERT OR REPLACE INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                                           (AutonomyAuditStore.namespace, json.dumps(legacy)))
                with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
                    keys = capture.record_keys(AutonomyAuditStore.namespace)
                    if singleton:
                        self.assertIsNone(keys)
                    else:
                        self.assertNotIn(AutonomyAuditStore.meta_key, keys)
                    result = self.verify(capture)
                self.assertEqual(result.model_dump(exclude={'verified_at'}), expected.model_dump(exclude={'verified_at'}))
        with self.state._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                               (state_record_storage_key(AutonomyAuditStore.namespace, 'shadowed-invalid'), '{malformed'))
        with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
            self.assertIsNone(capture.record_keys(AutonomyAuditStore.namespace))
            with self.assertRaises(ValueError):
                self.verify(capture)

    def test_corrupt_hash_and_reordered_storage_match_canonical_verifier(self):
        keys = sorted(key for key in self.raw if key.startswith('record:'))
        values = [self.raw[key] for key in keys][::-1]
        for key, value in zip(keys, values):
            self.raw[key] = value
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    next(value for key, value in self.raw.items() if key.startswith('record:')
                         and value['partition_id'] == self.rows[0].partition_id)['payload']['reason_code'] = 'tampered'
                self.write()
                expected = self.audit.verify_snapshot(self.raw, organization_id='org-a', workspace_id='ws-a')
                with SnapshotCapture(self.state, lambda *_: None, only_namespace=AutonomyAuditStore.namespace) as capture:
                    result = self.verify(capture)
                self.assertEqual(result.model_dump(exclude={'verified_at'}), expected.model_dump(exclude={'verified_at'}))
                self.assertEqual(result.status, AuditIntegrityStatus.FAILED if corrupt else AuditIntegrityStatus.VERIFIED)
