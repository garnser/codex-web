"""Semantic Recovery fixtures for bounded envelope decryption, no application."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_recovery as fixtures
from codex_web.services import recovery as module
from codex_web.services.recovery import RecoveryMemoryConflictError


class RecoveryStreamVerificationTests(unittest.TestCase):
    setUp = fixtures.RecoveryTests.setUp
    tearDown = fixtures.RecoveryTests.tearDown

    def backup(self):
        # Large enough that bounded decryption is cheaper than the old envelope
        # estimate, while keeping the fixture small on shared development hosts.
        self.state.put('synthetic_stream_fixture', {'value': 'x' * (2 * 1024**2)})
        return self.service.create_backup(actor=self.actor)

    def test_streamed_and_original_reader_have_identical_semantic_results(self):
        manifest = self.backup()
        with patch.object(self.destination, 'get_bounded', side_effect=AssertionError('whole read')), \
             patch.object(self.crypto, 'decrypt', side_effect=AssertionError('whole decrypt')):
            streamed = self.service.verify_restore(manifest.id, actor=self.actor)
        with patch.object(self.destination, 'open_bounded', None):
            original = self.service.verify_restore(manifest.id, actor=self.actor)
        for field in ('status', 'checksum_valid', 'schema_compatible', 'key_manifest_valid',
                      'audit_continuity_valid', 'document_count', 'restored_checksum',
                      'key_missing_refs', 'key_revoked_refs', 'blockers', 'isolated',
                      'side_effects_enabled'):
            with self.subTest(field=field):
                self.assertEqual(getattr(streamed, field), getattr(original, field))
        self.assertEqual(streamed.status.value, 'pass')

    def test_decode_capacity_refusal_still_has_no_verification_or_evidence(self):
        manifest = self.backup()
        before = self.service.store.load()
        evidence_count = len(self.evidence.items)
        calls = []
        original_admit = module._memory_admit
        def refuse_decode(stage, size):
            calls.append((stage, size))
            if stage == 'restore_decode':
                raise RecoveryMemoryConflictError('fixture restore_decode capacity refusal')
            return original_admit(stage, size)
        with patch.object(module, '_memory_admit', side_effect=refuse_decode):
            with self.assertRaisesRegex(RecoveryMemoryConflictError, 'restore_decode'):
                self.service.verify_restore(manifest.id, actor=self.actor)
        self.assertEqual(self.service.store.load(), before)
        self.assertEqual(len(self.evidence.items), evidence_count)
        stream_size = next(size for stage, size in calls if stage == 'restore_envelope_stream')
        decode_size = next(size for stage, size in calls if stage == 'restore_decode')
        self.assertEqual(decode_size, (stream_size - module.ENVELOPE_STREAM_WORK_BYTES) * 6)
        self.assertFalse(any(stage == 'restore_snapshot' for stage, _ in calls))

    def test_changed_checksum_does_not_decrypt_or_report_success(self):
        manifest = self.backup()
        path = self.destination._path(manifest.destination_ref)
        with path.open('r+b') as handle:
            handle.seek(100)
            value = handle.read(1)
            handle.seek(100)
            handle.write(bytes([value[0] ^ 1]))
        with patch.object(self.crypto, 'decrypt_envelope_stream') as decrypt:
            result = self.service.verify_restore(manifest.id, actor=self.actor)
        decrypt.assert_not_called()
        self.assertEqual(result.status.value, 'fail')
        self.assertFalse(result.checksum_valid)

    def test_legacy_escaped_ciphertext_uses_original_guarded_reader(self):
        manifest = self.backup()
        path = self.destination._path(manifest.destination_ref)
        envelope = json.loads(path.read_bytes())
        raw = json.dumps(envelope, separators=(',', ':')).encode()
        marker = b'"ciphertext_b64":"'
        index = raw.index(marker) + len(marker)
        # An equivalent JSON spelling accepted by the original model/crypto.
        raw = raw[:index] + ('\\u%04x' % raw[index]).encode() + raw[index + 1:]
        path.write_bytes(raw)
        changed = manifest.model_copy(update={'envelope_sha256': hashlib.sha256(raw).hexdigest()})
        def update(state):
            state.backups[manifest.id] = changed
            return state
        self.service.store.update(update)
        with patch.object(self.crypto, 'decrypt_envelope_stream', side_effect=AssertionError('stream used')), \
             patch.object(module, '_memory_admit', wraps=module._memory_admit) as admit:
            result = self.service.verify_restore(manifest.id, actor=self.actor)
        self.assertEqual(result.status.value, 'pass')
        self.assertIn(unittest.mock.call('restore_snapshot', len(raw) * 8), admit.call_args_list)

    def test_open_pins_private_regular_file_and_refuses_changed_size(self):
        manifest = self.backup()
        size = self.destination.size(manifest.destination_ref)
        with self.destination.open_bounded(manifest.destination_ref, size) as handle:
            self.assertEqual(handle.read(1), b'{')
        with self.assertRaisesRegex(RecoveryMemoryConflictError, 'size_changed'):
            with self.destination.open_bounded(manifest.destination_ref, size + 1):
                self.fail('changed size was opened')
        path = self.destination._path(manifest.destination_ref)
        other = path.with_name('link.json')
        other.symlink_to(path.name)
        with self.assertRaises(OSError):
            with self.destination.open_bounded('file://' + str(other), size):
                self.fail('symlink was opened')


if __name__ == '__main__':
    unittest.main()
