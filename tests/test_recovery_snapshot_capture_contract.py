"""Focused bounded capture/ordered audit contracts; private SQLite fixtures only."""
import gc
import json
import tracemalloc
import unittest
from unittest.mock import patch

from codex_web.autonomy_audit import AutonomyAuditKind, AutonomyAuditPayload
from codex_web.services.recovery import RecoveryConflictError, RecoveryMemoryConflictError
from codex_web.services.recovery_snapshot_capture import SnapshotCapture, SnapshotCaptureRefused
from codex_web.services.recovery_snapshot_strings import SnapshotStringMetadataChanged
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.state_store import state_documents_checksum, state_record_storage_key
from tests import test_recovery as fixture
from tests.test_autonomy_audit import _Signer


class SnapshotCaptureContractTests(unittest.TestCase):
    setUp = fixture.RecoveryTests.setUp
    tearDown = fixture.RecoveryTests.tearDown

    def test_consistent_snapshot_key_order_legacy_and_empty_parity(self):
        self.state.record_replace('雪/%', {'a/b': [None, -0.0], 'a b': {}, '🚀': 'é'})
        self.state.record_replace('empty', {})
        expected = self.state.documents()
        metadata = {'created_at': 1.0, 'name': 'canonical'}
        with SnapshotCapture(self.state, lambda *_: None) as capture:
            # A separate committed writer must not change any later payload read.
            self.state.record_mutate('雪/%', ('a/b',), lambda _: {'a/b': ['later']})
            checksum = capture.checksum()
            plaintext = capture.plaintext(metadata)
        self.assertEqual(checksum, state_documents_checksum(expected))
        self.assertEqual(plaintext, json.dumps({**metadata, 'documents': expected},
            sort_keys=True, separators=(',', ':')).encode())

    def test_row_and_metadata_bounds_refuse_before_payload_decoding(self):
        self.state.put('larger', {'value': 'x' * 100})
        for constant, limit in (('MAX_ROW_BYTES', 10), ('MAX_METADATA_BYTES', 1)):
            with self.subTest(bound=constant), patch(
                'codex_web.services.recovery_snapshot_capture.' + constant, limit
            ), patch('codex_web.services.recovery_snapshot_capture.json.loads',
                     side_effect=AssertionError('payload read before admission')):
                with self.assertRaises(SnapshotCaptureRefused):
                    with SnapshotCapture(self.state, lambda *_: None):
                        self.fail('invalid inventory admitted')

    def test_shadowed_malformed_record_still_refuses(self):
        self.state.put('legacy', {'winner': True})
        with self.state._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                (state_record_storage_key('legacy', 'shadow'), '{malformed'))
        with SnapshotCapture(self.state, lambda *_: None) as capture:
            with self.assertRaises(ValueError):
                capture.checksum()

    def test_string_metadata_and_late_capacity_refuse_before_encryption(self):
        for failure in ('string', 'capacity'):
            with self.subTest(failure=failure):
                def admission(stage, allocation):
                    if stage == 'capture_serialize':
                        raise RecoveryMemoryConflictError('fixture capacity refusal')
                context = (patch('codex_web.services.recovery_snapshot_capture.require_snapshot_string_bound',
                    side_effect=SnapshotStringMetadataChanged('Snapshot string metadata changed'))
                    if failure == 'string' else patch('codex_web.services.recovery._memory_admit', new=admission))
                with context, patch.object(self.crypto, 'encrypt', side_effect=AssertionError('encrypt after refusal')):
                    with self.assertRaises(RecoveryConflictError):
                        self.service.create_backup(actor=self.actor)
                self.assertFalse(self.service.store.load().backups)
                self.assertFalse(list(self.destination.directory.iterdir()))

    def test_metadata_estimate_covers_unicode_and_long_key_inventory(self):
        self.state.record_replace('metadata/雪', {('🚀é/%' * 12) + str(i): {'v': i} for i in range(4096)})
        gc.collect()
        tracemalloc.start()
        try:
            with SnapshotCapture(self.state, lambda *_: None) as capture:
                _, peak = tracemalloc.get_traced_memory()
                self.assertLess(peak, capture.metadata_bound + 32 * 1024**2)
        finally:
            tracemalloc.stop()

    def audit_parts(self, raw):
        keys = tuple(sorted(key for key in raw if key.startswith('record:')))
        control = {key: value for key, value in raw.items() if not key.startswith(('record:', 'tail:'))}
        return control, keys, lambda selected: ((key, raw[key]) for key in selected)

    def test_audit_stream_matches_existing_reordered_valid_and_negative_results(self):
        self.audit.signer = _Signer()
        for _ in range(2):
            self.audit_store.append(AutonomyAuditPayload(kind=AutonomyAuditKind.INTEGRITY_VERIFICATION,
                organization_id='org-a', workspace_id='ws-a', outcome='verified', reason_code='ordered'))
        # Multiple checkpoints at the same sequence retain append order/signatures.
        self.audit.checkpoint(organization_id='org-a', workspace_id='ws-a')
        self.audit.checkpoint(organization_id='org-a', workspace_id='ws-a')
        original = AutonomyAuditStore._encode(self.audit_store.load())
        keys = sorted(key for key in original if key.startswith('record:'))
        reordered = json.loads(json.dumps(original))
        values = [reordered[key] for key in keys][::-1]
        for key, value in zip(keys, values):
            reordered[key] = value
        variants = [('valid_reordered', reordered)]
        for name in ('gap', 'duplicate', 'payload', 'checkpoint'):
            changed = json.loads(json.dumps(original))
            if name == 'gap':
                changed.pop(keys[0])
            elif name == 'duplicate':
                changed[keys[1]] = changed[keys[0]]
            elif name == 'payload':
                changed[keys[0]]['payload']['reason_code'] = 'tampered'
            else:
                changed['checkpoints'][0]['signature'] = 'tampered'
            variants.append((name, changed))
        for name, raw in variants:
            with self.subTest(case=name):
                expected = self.audit.verify_snapshot(raw, organization_id='org-a', workspace_id='ws-a')
                actual = self.audit.verify_snapshot_records(*self.audit_parts(raw),
                    organization_id='org-a', workspace_id='ws-a')
                # Each independent verification has its own observation time.
                # Compare every integrity field without replacing its clock.
                self.assertEqual(actual.model_dump(exclude={'verified_at'}),
                                 expected.model_dump(exclude={'verified_at'}))
                self.assertGreater(actual.verified_at, 0)
                self.assertGreater(expected.verified_at, 0)
        foreign = json.loads(json.dumps(original))
        foreign['record:foreign'] = {'partition_id': 'other', 'sequence': 1, 'payload': {}}
        with self.assertRaises(ValueError):
            self.audit.verify_snapshot(foreign, organization_id='org-a', workspace_id='ws-a')
        with self.assertRaises(ValueError):
            self.audit.verify_snapshot_records(*self.audit_parts(foreign),
                organization_id='org-a', workspace_id='ws-a')
