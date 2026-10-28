"""Synthetic existing-envelope parsing only; no keys, app, database or native IO."""
import base64
from dataclasses import replace
import hashlib
import json
import os
import tempfile
import unittest

from codex_web.services.recovery_envelope_stream import (
    EnvelopeStreamRefused, EnvelopeStreamUnsupported, READ_BYTES,
    scan_envelope, iter_envelope_ciphertext,
)


class _BoundedReader:
    def __init__(self, handle, short=None, after_read=None):
        self.handle, self.short, self.after_read = handle, short, after_read
        self.reads = []

    def fileno(self):
        return self.handle.fileno()

    def seek(self, offset):
        return self.handle.seek(offset)

    def read(self, size):
        if not 0 < size <= READ_BYTES:
            raise AssertionError('unbounded read')
        self.reads.append(size)
        value = self.handle.read(min(size, self.short) if self.short else size)
        if self.after_read:
            self.after_read(self)
        return value


class RecoveryEnvelopeStreamTests(unittest.TestCase):
    def file(self, raw):
        handle = tempfile.TemporaryFile('w+b')
        self.addCleanup(handle.close)
        handle.write(raw)
        handle.flush()
        return handle

    def envelope(self, ciphertext, **updates):
        metadata = dict(format_version='1.0', algorithm='aes-256-gcm', key_id='synthetic-key',
                        key_version=1, wrap_nonce_b64='YQ==', wrapped_data_key_b64='Yg==',
                        data_nonce_b64='Yw==', context_digest='0' * 64, created_at=1.25)
        metadata.update(updates)
        return dict(metadata, ciphertext_b64=base64.b64encode(ciphertext).decode())

    def arguments(self, raw):
        return dict(expected_size=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest(), checkpoint=lambda: None)

    def test_large_payload_retains_only_metadata_and_bounded_chunks(self):
        payload = bytes(range(256)) * 8192 + b'last-tag-16-bytes'
        body = self.envelope(payload)
        raw = json.dumps(body, sort_keys=True).encode()
        reader = _BoundedReader(self.file(raw))
        scan = scan_envelope(reader, **self.arguments(raw))
        self.assertEqual(scan.metadata, {key: value for key, value in body.items() if key != 'ciphertext_b64'})
        self.assertEqual((scan.ciphertext_size, scan.plaintext_size), (len(payload), len(payload) - 16))
        chunks = list(iter_envelope_ciphertext(reader, scan, **self.arguments(raw)))
        self.assertTrue(all(type(chunk) is bytes and 0 < len(chunk) <= READ_BYTES for chunk in chunks))
        self.assertEqual(b''.join(chunks), payload)
        self.assertGreater(len(reader.reads), 20)

    def test_metadata_order_whitespace_unicode_and_short_reads(self):
        for tail in (b'', b'x', b'xy'):
            with self.subTest(tail=tail):
                body = self.envelope(b'1234567890abcdef' + tail, key_id='synthetic-å')
                raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
                reader = _BoundedReader(self.file(raw), short=5)
                scan = scan_envelope(reader, **self.arguments(raw))
                self.assertEqual(scan.metadata['key_id'], 'synthetic-å')
                self.assertEqual(b''.join(iter_envelope_ciphertext(reader, scan, **self.arguments(raw))), b'1234567890abcdef' + tail)

    def test_invalid_base64_and_missing_tag_refuse_closed(self):
        for encoded in ('not!base64', 'AAAAA', 'ZQ==AAAA', '=AAA', '', 'YQ=='):
            with self.subTest(encoded=encoded):
                raw = json.dumps(dict(self.envelope(b'x' * 16), ciphertext_b64=encoded)).encode()
                with self.assertRaises(EnvelopeStreamRefused) as error:
                    scan_envelope(self.file(raw), **self.arguments(raw))
                self.assertNotIn(encoded or 'ciphertext_b64', str(error.exception))

    def test_corrupt_hash_size_json_and_eof_never_return_scan(self):
        valid = json.dumps(self.envelope(b'x' * 24)).encode()
        cases = [valid[:-1], valid + b' true', valid.replace(b'"key_id":', b'"key_id" '), b'{}', b'{"ciphertext_b64":']
        for raw in cases:
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises(EnvelopeStreamRefused):
                    scan_envelope(self.file(raw), **self.arguments(raw))
        args = self.arguments(valid)
        args['expected_sha256'] = 'f' * 64
        with self.assertRaisesRegex(EnvelopeStreamRefused, 'checksum'):
            scan_envelope(self.file(valid), **args)
        args = self.arguments(valid)
        args['expected_size'] += 1
        with self.assertRaisesRegex(EnvelopeStreamRefused, 'size'):
            scan_envelope(self.file(valid), **args)

    def test_legacy_valid_unusual_spellings_request_original_guarded_reader(self):
        body = self.envelope(b'1234567890abcdef')
        valid = json.dumps(body).encode()
        escaped = valid.replace(body['ciphertext_b64'].encode(), b'\\u004d' + body['ciphertext_b64'][1:].encode())
        spaced = json.dumps(dict(body, ciphertext_b64=' ' + body['ciphertext_b64'] + ' ')).encode()
        duplicated = valid[:-1] + b',"key_version":1}'
        large_metadata = json.dumps(self.envelope(b'x' * 16, key_id='a' * 65537)).encode()
        nonfinite = json.dumps(self.envelope(b'x' * 16, created_at=float('nan'))).encode()
        for raw in (escaped, spaced, duplicated, large_metadata, nonfinite):
            with self.subTest(length=len(raw)):
                json.loads(raw)  # valid JSON is not labelled corrupted
                with self.assertRaises(EnvelopeStreamUnsupported):
                    scan_envelope(self.file(raw), **self.arguments(raw))

    def test_changed_file_between_passes_and_different_fd_refuse(self):
        raw = json.dumps(self.envelope(b'x' * 100)).encode()
        handle = self.file(raw)
        scan = scan_envelope(handle, **self.arguments(raw))
        with self.assertRaisesRegex(EnvelopeStreamRefused, 'file_changed'):
            list(iter_envelope_ciphertext(self.file(raw), scan, **self.arguments(raw)))
        handle.seek(0)
        handle.write(raw.replace(b'synthetic-key', b'different-key'))
        handle.flush()
        with self.assertRaisesRegex(EnvelopeStreamRefused, 'file_changed'):
            list(iter_envelope_ciphertext(handle, scan, **self.arguments(raw)))

    def test_second_pass_mutation_refuses_at_exhaustion_after_bounded_yields(self):
        raw = json.dumps(self.envelope(b'x' * 200000)).encode()
        handle = self.file(raw)
        scan = scan_envelope(handle, **self.arguments(raw))
        changed = False
        def mutate(reader):
            nonlocal changed
            if not changed:
                changed = True
                os.pwrite(handle.fileno(), b' ', len(raw) - 1)
        reader = _BoundedReader(handle, after_read=mutate)
        received = []
        with self.assertRaises(EnvelopeStreamRefused):
            for part in iter_envelope_ciphertext(reader, scan, **self.arguments(raw)):
                received.append(part)
        self.assertTrue(received)  # consumer must not expose data before exhaustion/tag

    def test_scan_metadata_or_count_cannot_be_rebound_for_second_pass(self):
        raw = json.dumps(self.envelope(b'x' * 24)).encode()
        handle = self.file(raw)
        scan = scan_envelope(handle, **self.arguments(raw))
        for changed in (replace(scan, metadata=dict(scan.metadata, key_id='foreign')),
                        replace(scan, ciphertext_size=scan.ciphertext_size + 1)):
            with self.subTest(metadata=changed.metadata['key_id']):
                with self.assertRaisesRegex(EnvelopeStreamRefused, 'metadata_or_length'):
                    list(iter_envelope_ciphertext(handle, changed, **self.arguments(raw)))

    def test_checkpoint_cancellation_propagates_without_payload_diagnostics(self):
        raw = json.dumps(self.envelope(b'x' * 100000)).encode()
        calls = 0
        class Cancelled(Exception):
            pass
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise Cancelled()
        args = self.arguments(raw)
        args['checkpoint'] = checkpoint
        with self.assertRaises(Cancelled):
            scan_envelope(_BoundedReader(self.file(raw)), **args)
        self.assertEqual(calls, 5)
