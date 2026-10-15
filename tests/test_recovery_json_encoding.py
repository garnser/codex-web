"""The recovery wire bytes and existing checksum contract cannot change."""
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.canonical_json import canonical_json_bytes
from codex_web.services.recovery import RecoveryService
from codex_web.storage.state_store import state_documents_checksum


class RecoveryEncodingTests(unittest.TestCase):
    def test_recovery_bytes_match_existing_encoder(self):
        samples = [None, True, False, 0, -1, -0.0, 1.5e-31,
                   float('inf'), float('-inf'), float('nan'),
                   'quote" slash\\ control\n\u0000 雪 🚀 \ud800',
                   {'z': [None, True, 3.2], 'a': {'unicode': 'Å雪'}},
                   [{'index': i, 'nested': [i, str(i)]} for i in range(2000)]]
        for value in samples:
            with self.subTest(type=type(value).__name__):
                expected = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
                self.assertEqual(RecoveryService._canonical_bytes(value), expected)

    def test_namespace_checksums_preserve_legacy_framing_and_default(self):
        documents = {'z/雪': {'path': Path('/fixture/path'), 'items': list(range(1000))},
                     'a': {'value': -0.0}, 'empty': {}}
        previous = hashlib.sha256()
        for namespace in sorted(documents):
            encoded = json.dumps(documents[namespace], sort_keys=True,
                                 separators=(',', ':'), default=str).encode('utf-8')
            previous.update(namespace.encode('utf-8') + b'\x00')
            previous.update(hashlib.sha256(encoded).digest() + b'\x00')
        self.assertEqual(state_documents_checksum(documents), previous.hexdigest())

    def test_structural_encoding_does_not_enter_one_shot_c_encoder(self):
        with patch('json.encoder.c_make_encoder', side_effect=AssertionError('one-shot C traversal')):
            value = {'records': [{'number': i} for i in range(1000)]}
            encoded = canonical_json_bytes(value)
            self.assertEqual(json.loads(encoded), value)

    def test_invalid_payloads_still_fail(self):
        with self.assertRaises(TypeError):
            canonical_json_bytes({'unsupported': object()})
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(ValueError):
            canonical_json_bytes(cycle)
