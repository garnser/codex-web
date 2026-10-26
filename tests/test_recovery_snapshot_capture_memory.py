"""Whole semantic capture memory and compatibility, using disposable local state."""
import gc
import hashlib
import json
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.recovery import BackupSnapshot, RestoreVerificationStatus
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import (
    state_documents_checksum, state_record_marker, state_record_storage_key,
)
from tests import test_recovery as recovery_fixture


class RecoverySnapshotCaptureMemoryTests(unittest.TestCase):
    setUp = recovery_fixture.RecoveryTests.setUp
    tearDown = recovery_fixture.RecoveryTests.tearDown

    def seed_object_dense_catalog(self):
        # Small nested containers expose decoded-object inflation. A giant
        # ASCII value would mostly measure required output bytes instead.
        def record(index):
            return {
                'index': index,
                'unicode': '雪/é/🚀',
                'negative_zero': -0.0,
                'entries': [
                    {'nested': [[], {}, [], {}, [], {}],
                     'flags': [None, True, False, 0, 1, 2],
                     'meta': {'a': {}, 'b': {}, 'c': []}}
                    for _ in range(24)
                ],
            }

        self.state.record_replace('capture-objects', {
            f'{index:05d}/雪': record(index) for index in range(2048)
        })
        self.state.record_replace('empty-keyed', {})
        self.state.put('legacy-wins', {'winner': 'legacy', 'value': [None, -0.0]})
        # Seed the historical coexisting representations directly in this
        # fixture's private SQLite database: ordinary writes migrate them.
        with self.state._connection() as connection:
            for namespace, value in (
                (state_record_marker('legacy-wins'), {'schemaVersion': 1}),
                (state_record_storage_key('legacy-wins', 'shadow'),
                 {'winner': 'must-not-be-exported'}),
            ):
                connection.execute(
                    'INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,?)',
                    (namespace, json.dumps(value), 1.0),
                )
        recovery_fixture.RecoveryTests._seed_executable_state(self)

    def legacy_oracle(self):
        # Construct and release the old decoded representation BEFORE tracing.
        # The expected wire bytes survive, not the large Python object graph.
        documents = self.state.documents()
        recovery = RecoveryStore._decode(documents[RecoveryStore.namespace])
        documents[RecoveryStore.namespace] = RecoveryStore._encode(
            recovery.model_copy(update={'backups': {}, 'verifications': {}})
        )
        checksum = state_documents_checksum(documents)
        keys = self.service._snapshot_key_manifest(documents, self.actor)
        audit_root, checkpoint = self.service._audit_anchor(self.actor, documents)
        status = self.state.status()
        snapshot = BackupSnapshot(
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            state_backend=status['backend'],
            state_schema_version=status['schemaVersion'],
            created_at=self.clock(),
            documents=documents,
            documents_checksum=checksum,
            key_manifest=keys,
            audit_root_hash=audit_root,
            audit_checkpoint_id=checkpoint,
        )
        wire = json.dumps(snapshot.model_dump(mode='json'), sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
        return wire, checksum, tuple(sorted(documents)), keys, audit_root, checkpoint

    def test_real_capture_peak_and_legacy_bytes_full_isolated_restore(self):
        self.seed_object_dense_catalog()
        expected, checksum, namespaces, keys, audit_root, checkpoint = self.legacy_oracle()
        gc.collect()
        encrypt = self.crypto.encrypt
        calls = []

        def checked_encrypt(key_id, plaintext, context, *, actor):
            # Do not decode the plaintext here: test-created JSON objects would
            # contaminate the capture peak. Equality checks every legacy byte.
            self.assertEqual(plaintext, expected)
            calls.append(hashlib.sha256(plaintext).hexdigest())
            return encrypt(key_id, plaintext, context, actor=actor)

        # A Mock would retain its plaintext call arguments through publication
        # and introduce an artificial whole-buffer overlap into this measure.
        with patch.object(self.crypto, 'encrypt', new=checked_encrypt):
            tracemalloc.start()
            try:
                manifest = self.service.create_backup(actor=self.actor)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest.plaintext_checksum, checksum)
        self.assertEqual(manifest.namespaces, namespaces)
        self.assertEqual(manifest.document_count, len(namespaces))
        self.assertEqual(manifest.key_manifest, keys)
        self.assertEqual(manifest.audit_root_hash, audit_root)
        self.assertEqual(manifest.audit_checkpoint_id, checkpoint)
        # Whole create_backup(), including real encryption and destination
        # publication, is measured. This deliberately generous limit permits
        # required wire/envelope buffers but not the decoded catalog alongside
        # them. Old/new private measurements must accompany this assertion.
        bound = len(expected) * 6 + 8 * 1024**2
        print(json.dumps({'semantic_capture_memory': {
            'keyed_rows': 2048, 'nested_entries_per_row': 24,
            'legacy_plaintext_bytes': len(expected), 'capture_peak_bytes': peak,
            'capture_peak_bound_bytes': bound, 'plaintext_sha256': calls[0],
        }}), flush=True)
        self.assertLess(peak, bound, 'complete decoded catalog overlaps capture buffers')

        verified = self.service.verify_restore(manifest.id, actor=self.actor)
        self.assertEqual(verified.status, RestoreVerificationStatus.PASS)
        self.assertTrue(verified.checksum_valid)
        self.assertTrue(verified.schema_compatible)
        self.assertTrue(verified.key_manifest_valid)
        self.assertTrue(verified.audit_continuity_valid)
        self.assertTrue(verified.isolated)
        self.assertFalse(verified.side_effects_enabled)

        target = SQLiteStateStore(Path(self.temp.name) / 'full-restored.sqlite3')
        restored = self.service.restore_into_isolated(manifest.id, target, actor=self.actor)
        self.assertEqual(restored.status, RestoreVerificationStatus.PASS)
        source_documents = json.loads(expected)['documents']
        self.assertEqual(set(target.documents()), set(source_documents))
        for namespace in tuple(source_documents):
            payload = source_documents.pop(namespace)
            sanitized = self.service._sanitize_restored_documents({namespace: payload})
            self.assertEqual(target.get(namespace), sanitized[namespace], namespace)
            del payload, sanitized
        self.assertEqual(target.get('legacy-wins'),
                         {'winner': 'legacy', 'value': [None, -0.0]})
        self.assertEqual(target.get('empty-keyed'), {})
        self.assertEqual(target.get('autonomy')['control']['mode'], 'paused')
        self.assertEqual(target.get('scheduler')['schedules']['schedule-a']['status'], 'paused')
        self.assertEqual(target.get('action_intents')['intents'][0]['status'],
                         'requires_reconciliation')


if __name__ == '__main__':
    unittest.main()
