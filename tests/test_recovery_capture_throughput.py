"""Bounded synthetic semantic capture throughput with an exact legacy oracle."""
import cProfile
import gc
import tracemalloc
import hashlib
import json
import os
import math
import pstats
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.autonomy_audit import AutonomyAuditRecord
from codex_web.canonical_json import canonical_json_bytes
from codex_web.storage.state_store import (
    StorageOperationBudget, storage_operation_budget, RECOVERY_CAPTURE_STAGES,
    enter_recovery_capture_stage, check_operation_budget, state_record_storage_key,
)
from codex_web.recovery import RestoreVerificationStatus
from codex_web.services import recovery_snapshot_capture as capture_module
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_recovery as fixture
from tests import test_recovery_snapshot_capture_memory as memory_fixture


class CaptureThroughputTests(unittest.TestCase):
    setUp = fixture.RecoveryTests.setUp
    tearDown = fixture.RecoveryTests.tearDown
    legacy_oracle = memory_fixture.RecoverySnapshotCaptureMemoryTests.legacy_oracle

    def test_real_capture_matches_legacy_bytes_and_full_restore(self):
        rows = 2048
        self.state.record_replace('throughput-data', {
            f'{i:06d}/雪': {'blob': 'a' * 2048, 'index': i,
                          'nested': [None, True, False, -0.0, {'unicode': '雪/🚀/é'}]}
            for i in range(rows)
        })
        state = self.audit_store.load()
        records = list(state.records)
        for i in range(rows):
            previous = records[-1]
            records.append(AutonomyAuditRecord.build(previous.payload,
                sequence=previous.sequence + 1, previous_hash=previous.record_hash,
                created_at=float(i + 1)))
        self.audit_store.update(lambda _: state.model_copy(update={'records': records}))
        del state, records
        expected, checksum, namespaces, keys, root, checkpoint = self.legacy_oracle()
        check = capture_module.check_operation_budget
        checks = 0

        def checked_budget():
            nonlocal checks
            checks += 1
            return check()

        encrypt = self.crypto.encrypt

        def exact_encrypt(key_id, plaintext, context, *, actor):
            self.assertEqual(plaintext, expected)
            return encrypt(key_id, plaintext, context, actor=actor)

        profile = cProfile.Profile() if os.environ.get('CODEX_WEB_TEST_PROFILE_CAPTURE') == '1' else None
        started = time.monotonic()
        cpu = time.process_time()
        budget = StorageOperationBudget(120)
        budget_token = storage_operation_budget.set(budget)
        self.addCleanup(storage_operation_budget.reset, budget_token)
        with patch.object(capture_module, 'check_operation_budget', new=checked_budget), \
                patch.object(self.crypto, 'encrypt', new=exact_encrypt):
            if profile is not None:
                profile.enable()
            try:
                manifest = self.service.create_backup(actor=self.actor)
            finally:
                if profile is not None:
                    profile.disable()
        elapsed = time.monotonic() - started
        cpu = time.process_time() - cpu
        # Bound work by rows/bytes rather than JSON punctuation tokens. The old
        # encoder needed >500,000 checks on this same fixture; deadlines remain
        # active at each row and every <=64KiB encoded output chunk.
        self.assertLess(checks, rows * 40)
        self.assertEqual(manifest.plaintext_checksum, checksum)
        self.assertEqual(manifest.namespaces, namespaces)
        self.assertEqual(manifest.key_manifest, keys)
        self.assertEqual((manifest.audit_root_hash, manifest.audit_checkpoint_id), (root, checkpoint))
        timing = budget.recovery_capture_timings()
        self.assertEqual(timing['stage'], 'envelope')
        self.assertEqual(set(timing['stage_seconds']), RECOVERY_CAPTURE_STAGES)
        self.assertTrue(all(math.isfinite(value) and value >= 0
                            for value in timing['stage_seconds'].values()))
        metrics = {'data_rows': rows, 'audit_records': rows + 1,
                   'capture_timings': timing,
                   'legacy_plaintext_bytes': len(expected), 'capture_seconds': elapsed,
                   'capture_cpu_seconds': cpu, 'capture_budget_checks': checks,
                   'plaintext_sha256': hashlib.sha256(expected).hexdigest()}
        if profile is not None:
            wanted = {'values', '_lengths', 'require_snapshot_string_bound', 'observe_snapshot_string_bound',
                      'namespace_chunks', 'checksum', 'plaintext', '_iterencode_dict', '_iterencode_list',
                      'write', 'iterencode', '_verify_ordered_records', '_expected_record_hash', 'loads', 'dumps'}
            costs = {}
            for (_, _, name), (_, calls, own, cumulative, _) in pstats.Stats(profile).stats.items():
                if name not in wanted:
                    continue
                row = costs.setdefault(name, {'calls': 0, 'own_seconds': 0., 'cumulative_seconds': 0.})
                row['calls'] += calls
                row['own_seconds'] += own
                row['cumulative_seconds'] += cumulative
            metrics['profile_closed_functions'] = costs
        print(json.dumps({'semantic_capture_throughput': metrics}), flush=True)
        verified = self.service.verify_restore(manifest.id, actor=self.actor)
        self.assertEqual(verified.status, RestoreVerificationStatus.PASS)
        self.assertTrue(verified.checksum_valid and verified.key_manifest_valid and verified.audit_continuity_valid)
        target = SQLiteStateStore(Path(self.temp.name) / 'throughput-isolated.sqlite3')
        restored = self.service.restore_into_isolated(manifest.id, target, actor=self.actor)
        self.assertEqual(restored.status, RestoreVerificationStatus.PASS)
        for namespace, payload in json.loads(expected)['documents'].items():
            self.assertEqual(target.get(namespace),
                self.service._sanitize_restored_documents({namespace: payload})[namespace], namespace)


class BoundedRowEncodingTests(unittest.TestCase):
    def test_native_row_bytes_match_legacy_with_bounded_chunks_and_real_cancellation(self):
        capture = capture_module.SnapshotCapture(None, None)
        value = {'unicode': '雪🚀\x00é' * 20000, 'floats': [-0.0, 1e-300, 1e300,
                 float('inf'), float('-inf'), float('nan')],
                 'nested': [None, True, False, {}, [], {'z': 1, 'a': '\ud800'}]}
        chunks = list(capture.row_chunks(value))
        self.assertEqual(b''.join(chunks), canonical_json_bytes(value))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= capture_module.ENCODED_CHUNK_BYTES for chunk in chunks))
        budget = StorageOperationBudget(30)
        token = storage_operation_budget.set(budget)
        try:
            stream = capture.row_chunks(value)
            next(stream)
            budget.cancelled.set()
            with self.assertRaisesRegex(TimeoutError, 'storage operation deadline exceeded'):
                next(stream)
            stream.close()
        finally:
            storage_operation_budget.reset(token)


class PhysicalRowValidationTests(unittest.TestCase):
    setUp = fixture.RecoveryTests.setUp
    tearDown = fixture.RecoveryTests.tearDown

    def test_shadowed_alias_and_overridden_original_rows_cannot_hide_bad_json(self):
        for case in ('legacy_shadow', 'key_alias', 'override'):
            with self.subTest(case=case):
                namespace = 'malformed-' + case
                if case == 'legacy_shadow':
                    self.state.put(namespace, {'winner': True})
                    physical = state_record_storage_key(namespace, 'shadow')
                elif case == 'key_alias':
                    self.state.record_replace(namespace, {'a': 'winner'})
                    physical = state_record_storage_key(namespace, 'a')[:-1] + '%61'
                else:
                    self.state.put(namespace, {'old': 'replaced'})
                    physical = namespace
                with self.state._connection() as connection:
                    connection.execute('INSERT OR REPLACE INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                                       (physical, '{malformed'))
                try:
                    with capture_module.SnapshotCapture(self.state, lambda *_: None) as capture:
                        if case == 'override':
                            capture.replace(namespace, {'new': 'valid'})
                        with self.assertRaises(ValueError):
                            capture.checksum()
                        self.assertFalse(getattr(capture, '_validated_all', False))
                finally:
                    with self.state._connection() as connection:
                        connection.execute('DELETE FROM state_documents WHERE namespace IN (?,?)', (physical, namespace))

    def test_failed_selected_row_and_audit_only_full_validation_cannot_mark_catalog_valid(self):
        self.state.record_replace('bad-selected', {'row': {'valid': True}})
        physical = state_record_storage_key('bad-selected', 'row')
        with self.state._connection() as connection:
            connection.execute('UPDATE state_documents SET payload=? WHERE namespace=?', ('{malformed', physical))
        with capture_module.SnapshotCapture(self.state, lambda *_: None) as capture:
            with self.assertRaises(ValueError):
                capture.checksum()
            self.assertFalse(getattr(capture, '_validated_all', False))
            with self.assertRaises(ValueError):
                capture.validate_physical_rows()
            self.assertFalse(getattr(capture, '_validated_all', False))

    def test_maximum_utf8_row_ascii_expansion_stays_within_existing_row_reservation(self):
        # An unescaped supplementary character needs four source bytes but
        # twelve canonical ASCII bytes. Exercise the actual 32MiB row boundary,
        # not a reduced limit or the already-escaped default SQLite writer.
        count = (capture_module.MAX_ROW_BYTES - 2) // 4
        value = '\U00010000' * count
        raw = json.dumps(value, ensure_ascii=False)
        raw_bytes = len(raw.encode('utf-8'))
        expected = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        with self.state._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                               ('utf8-maximum-row', raw))
        del raw, value
        gc.collect()
        with capture_module.SnapshotCapture(self.state, lambda *_: None) as capture:
            self.assertEqual(capture.largest, raw_bytes)
            digest, encoded_bytes = hashlib.sha256(), 0
            tracemalloc.start()
            try:
                for chunk in capture.namespace_chunks('utf8-maximum-row'):
                    self.assertLessEqual(len(chunk), capture_module.ENCODED_CHUNK_BYTES)
                    encoded_bytes += len(chunk)
                    digest.update(chunk)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(digest.hexdigest(), expected)
            self.assertEqual(encoded_bytes, count * 12 + 2)
            self.assertLess(peak, capture.largest * 18)
        print(json.dumps({'semantic_capture_row_encoding': {
            'raw_row_bytes': raw_bytes, 'canonical_row_bytes': encoded_bytes,
            'peak_traced_bytes': peak, 'existing_row_work_reservation': raw_bytes * 18}}), flush=True)

    def test_each_selected_and_shadowed_value_keeps_both_string_observations(self):
        namespace = 'selection-string-guards'
        self.state.put(namespace, {'selected': 'unchanged'})
        physical = state_record_storage_key(namespace, 'shadowed')
        with self.state._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES(?,?,0)',
                               (physical, json.dumps({'shadowed': 'also unchanged'})))
        observed, required = [], []
        observe = capture_module.observe_snapshot_string_bound
        require = capture_module.require_snapshot_string_bound

        def observing(value):
            observed.append(value)
            return observe(value)

        def requiring(value, bound):
            required.append(value)
            return require(value, bound)

        with patch.object(capture_module, 'observe_snapshot_string_bound', new=observing), \
                patch.object(capture_module, 'require_snapshot_string_bound', new=requiring):
            with capture_module.SnapshotCapture(self.state, lambda *_: None) as capture:
                capture.checksum()
                capture.plaintext({'version': '1.0'})
        self.assertEqual(observed, required)
        self.assertEqual(observed.count({'selected': 'unchanged'}), 2)
        self.assertEqual(observed.count({'shadowed': 'also unchanged'}), 1)


class CaptureTimingBudgetTests(unittest.TestCase):
    def test_closed_stage_keys_accumulate_monotonic_durations_without_changing_deadline(self):
        budget = StorageOperationBudget(30)
        deadline = budget.deadline
        self.assertIsNone(budget.recovery_capture_timings())
        with patch('codex_web.storage.state_store.time.monotonic', side_effect=(10., 12., 15., 18.)):
            budget.enter_recovery_capture_stage('inventory')
            budget.enter_recovery_capture_stage('checksum')
            budget.enter_recovery_capture_stage('inventory')
            self.assertEqual(budget.recovery_capture_timings(), {
                'stage': 'inventory', 'stage_seconds': {'inventory': 5., 'checksum': 3.}})
        self.assertEqual(budget.deadline, deadline)
        for invalid in ('payload-or-credential', None, [], 1):
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(ValueError):
                budget.enter_recovery_capture_stage(invalid)
        self.assertEqual(set(budget.recovery_capture_timings()['stage_seconds']), {'inventory', 'checksum'})


from tests import test_preinstall_backup as executable_fixture


class CaptureTimingExecutableTests(unittest.IsolatedAsyncioTestCase):
    profile = executable_fixture.OfflineExecutableTests.profile
    create_plan = executable_fixture.OfflineExecutableTests.create_plan
    asyncSetUp = executable_fixture.OfflineExecutableTests.asyncSetUp
    asyncTearDown = executable_fixture.OfflineExecutableTests.asyncTearDown
    runtime = executable_fixture.OfflineExecutableTests.runtime
    invoke = executable_fixture.OfflineExecutableTests.invoke

    async def test_success_receipt_retains_only_closed_numeric_capture_stages(self):
        import asyncio
        original = self.recovery.create_backup

        def capture(**kwargs):
            for stage in ('inventory', 'shadowed_validation', 'checksum', 'captured_audit',
                          'plaintext', 'live_audit', 'encrypt', 'envelope'):
                enter_recovery_capture_stage(stage)
            return original(**kwargs)

        self.recovery.create_backup = capture
        self.assertEqual(await asyncio.to_thread(self.invoke, True), 0)
        receipt = json.loads((Path(self.temp.name) / 'journal/capture-completed.json').read_text())
        timing = receipt['capture_timings']
        self.assertEqual(timing['stage'], 'envelope')
        self.assertEqual(set(timing['stage_seconds']), RECOVERY_CAPTURE_STAGES)
        self.assertTrue(all(type(value) is float and math.isfinite(value) and value >= 0
                            for value in timing['stage_seconds'].values()))

    async def test_real_budget_timeout_receipt_retains_last_stage_without_authorizing_retry(self):
        import asyncio

        def capture(**kwargs):
            enter_recovery_capture_stage('inventory')
            enter_recovery_capture_stage('checksum')
            storage_operation_budget.get().deadline = time.monotonic() - 1
            check_operation_budget()
            self.fail('expired budget admitted capture')

        self.recovery.create_backup = capture
        self.assertEqual(await asyncio.to_thread(self.invoke, True), 2)
        receipt = json.loads((Path(self.temp.name) / 'journal/stopped.json').read_text())
        self.assertEqual(receipt['exception_type'], 'TimeoutError')
        self.assertEqual(receipt['capture_timings']['stage'], 'checksum')
        self.assertEqual(set(receipt['capture_timings']['stage_seconds']), {'inventory', 'checksum'})
        self.assertFalse(receipt['retry_authorized'])
        self.assertEqual(self.recovery.backup_count, 0)

    async def test_preflight_failure_has_no_uninitialized_capture_budget(self):
        import asyncio
        self.assertEqual(await asyncio.to_thread(self.invoke, False), 2)
        receipt = json.loads((Path(self.temp.name) / 'journal/stopped.json').read_text())
        self.assertEqual(receipt['stage'], 'waiting_for_operator_clean_stop')
        self.assertNotIn('capture_timings', receipt)
