"""Capacity refusals happen before the heavy canonical Recovery allocations."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.services.recovery import RecoveryMemoryConflictError
from codex_web.recovery import BackupDestinationError
from codex_web.services.recovery_memory import (
    MemoryHeadroom, RecoveryMemoryRefused, admit, memory_headroom, snapshot_size,
)
from tests import test_recovery as recovery_fixtures


class RecoveryMemoryProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.cg = self.root / 'cgroup'
        (self.proc / 'self').mkdir(parents=True)
        (self.cg / 'parent' / 'service').mkdir(parents=True)
        (self.proc / 'meminfo').write_text('MemAvailable: 8192000 kB\nMemTotal: 16384000 kB\n')
        (self.proc / 'self/cgroup').write_text('0::/parent/service\n')
        (self.proc / 'self/mountinfo').write_text(f'1 0 0:1 / {self.cg} rw - cgroup2 cgroup rw\n')
        for path, maximum, used in [(self.cg / 'parent', 6*1024**3, 4*1024**3),
                                    (self.cg / 'parent/service', 5*1024**3, 2*1024**3)]:
            (path / 'memory.max').write_text(str(maximum))
            (path / 'memory.current').write_text(str(used))

    def probe(self):
        return memory_headroom(proc=self.proc, cgroup=self.cg)

    def test_limiting_ancestor_and_host_are_both_respected_without_swap_credit(self):
        self.assertEqual(self.probe().available_bytes, 2*1024**3)
        (self.proc / 'meminfo').write_text('MemAvailable: 1024 kB\nMemTotal: 16384000 kB\nSwapFree: 99999999 kB\n')
        self.assertEqual(self.probe().available_bytes, 1024**2)

    def test_unknown_current_malformed_limit_or_missing_host_source_refuses(self):
        for file in [self.cg / 'parent/memory.current', self.cg / 'parent/memory.max', self.proc / 'meminfo']:
            original = file.read_text()
            file.write_text('not measured')
            with self.assertRaisesRegex(RecoveryMemoryRefused, 'capacity_unknown'):
                self.probe()
            file.write_text(original)
        (self.cg / 'parent/memory.max').unlink()
        with self.assertRaises(RecoveryMemoryRefused):
            self.probe()

    def test_cgroup_namespace_mount_root_and_unlimited_host_limit(self):
        (self.proc / 'self/cgroup').write_text('0::/tenant/service\n')
        (self.proc / 'self/mountinfo').write_text(f'1 0 0:1 /tenant {self.cg} rw - cgroup2 cgroup rw\n')
        (self.cg / 'service').mkdir()
        (self.cg / 'service/memory.max').write_text('max')
        self.assertEqual(self.probe().available_bytes, 8192000*1024)

    def test_refusal_is_explicit_estimate_and_unknown_sizing_never_means_unlimited(self):
        with patch('codex_web.services.recovery_memory.memory_headroom', return_value=MemoryHeadroom(100, 'fixture')):
            with self.assertRaisesRegex(RecoveryMemoryRefused, 'estimate_not_reservation'):
                admit('fixture', 200)
        with self.assertRaisesRegex(RecoveryMemoryRefused, 'size_unknown'):
            snapshot_size(object())


class RecoveryMemoryIntegrationTests(unittest.TestCase):
    setUp = recovery_fixtures.RecoveryTests.setUp
    tearDown = recovery_fixtures.RecoveryTests.tearDown

    def test_warm_capture_refuses_before_documents_or_crypto_or_publication(self):
        with patch('codex_web.services.recovery_memory.snapshot_size', return_value=(161864, 391916926)), \
             patch('codex_web.services.recovery_memory.memory_headroom', return_value=MemoryHeadroom(2*1024**3, 'fixture')), \
             patch.object(self.state, 'documents') as export, \
             patch.object(self.crypto, 'encrypt') as encrypt, \
             patch.object(self.destination, 'put') as publish:
            with self.assertRaisesRegex(RecoveryMemoryConflictError, 'capture_snapshot.*estimated_required_bytes'):
                self.service.create_backup(actor=self.actor)
        export.assert_not_called()
        encrypt.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(self.service.store.load().backups, {})

    def test_unknown_inventory_refuses_before_export(self):
        with patch('codex_web.services.recovery_memory.snapshot_size', side_effect=RecoveryMemoryRefused('size_unknown')), \
             patch.object(self.state, 'documents') as export:
            with self.assertRaisesRegex(RecoveryMemoryConflictError, 'size_unknown'):
                self.service.create_backup(actor=self.actor)
        export.assert_not_called()

    def test_real_envelope_refuses_before_read_decrypt_without_minting_verification(self):
        manifest = self.service.create_backup(actor=self.actor)
        before = self.service.store.load()
        with patch('codex_web.services.recovery_memory.memory_headroom', return_value=MemoryHeadroom(0, 'fixture')), \
             patch.object(self.destination, 'get_bounded') as read, \
             patch.object(self.crypto, 'decrypt') as decrypt:
            with self.assertRaisesRegex(RecoveryMemoryConflictError, 'restore_snapshot'):
                self.service.verify_restore(manifest.id, actor=self.actor)
        read.assert_not_called()
        decrypt.assert_not_called()
        self.assertEqual(self.service.store.load(), before)
        self.assertEqual(self.service.verify_restore(manifest.id, actor=self.actor).status.value, 'pass')

    def test_bounded_destination_rejects_growth_and_scoped_pointer_escape(self):
        manifest = self.service.create_backup(actor=self.actor)
        size = self.destination.size(manifest.destination_ref)
        with self.destination._path(manifest.destination_ref).open('ab') as handle:
            handle.write(b'changed')
        with self.assertRaisesRegex(RecoveryMemoryConflictError, 'size_changed'):
            self.destination.get_bounded(manifest.destination_ref, size)
        with self.assertRaises(BackupDestinationError):
            self.destination.size('file:///outside/tenant/backup.json')

    def test_exact_stage_refusal_before_encryption_and_no_backup_effect(self):
        def changed_headroom():
            changed_headroom.calls += 1
            return MemoryHeadroom(100*1024**3 if changed_headroom.calls == 1 else 0, 'fixture')
        changed_headroom.calls = 0
        with patch('codex_web.services.recovery_memory.memory_headroom', side_effect=changed_headroom), \
             patch.object(self.crypto, 'encrypt') as encrypt:
            with self.assertRaisesRegex(RecoveryMemoryConflictError, 'capture_encrypt'):
                self.service.create_backup(actor=self.actor)
        encrypt.assert_not_called()
        self.assertEqual(list(self.destination.directory.iterdir()), [])

    def test_metadata_size_counts_real_encoded_records_without_export(self):
        self.state.put('sizing-fixture', {'unicode': '雪', 'many': [1, 2, 3]})
        with patch.object(self.state, 'documents', side_effect=AssertionError('full export')), \
             patch('json.loads', side_effect=AssertionError('JSON decode during metadata sizing')):
            rows, size = snapshot_size(self.state)
        self.assertGreater(rows, 0)
        self.assertGreater(size, 0)
