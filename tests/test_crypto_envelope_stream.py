"""Synthetic existing-envelope authentication; no app, provider or shared state."""
import asyncio
import base64
import unittest
from unittest.mock import patch

from tests import test_crypto_keys as fixtures
from codex_web.services.crypto_keys import CryptoDecryptError


class CryptoEnvelopeStreamTests(unittest.TestCase):
    def setUp(self):
        fixtures.CryptoKeyServiceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.key = fixtures.CryptoKeyServiceTests._key(self)

    def envelope(self, value=b"synthetic plaintext" * 100):
        envelope = self.service.encrypt(self.key.id, value, self.context, actor=self.actor)
        metadata = envelope.model_dump(mode="json", exclude={"ciphertext_b64"})
        return value, envelope, metadata, base64.b64decode(envelope.ciphertext_b64)

    def decrypt(self, metadata, chunks, size, **kwargs):
        return self.service.decrypt_envelope_stream(metadata, chunks, size, self.context,
            actor=self.actor, checkpoint=kwargs.pop("checkpoint", lambda: None), **kwargs)

    def successes(self):
        return [event.id for event in self.service.store.load().events
                if event.operation == "decrypt" and event.outcome == "success"]

    def test_existing_envelopes_differential_roundtrip_empty_and_chunk_boundaries(self):
        for length in (0, 1, 15, 16, 17, 65535, 65536, 131073):
            with self.subTest(length=length):
                value, envelope, metadata, raw = self.envelope(b"x" * length)
                before = self.successes()
                def chunks():
                    for start in range(0, len(raw), 65536):
                        yield raw[start:start + 65536]
                recovered = self.decrypt(metadata, chunks(), len(raw))
                self.assertIs(type(recovered), bytearray)
                self.assertEqual(recovered, value)
                self.assertEqual(len(self.successes()), len(before) + 1)
                self.assertEqual(self.service.decrypt(envelope, self.context, actor=self.actor), recovered)

    def test_ciphertext_tag_wrapped_key_and_nonce_corruption_refuse_without_success(self):
        _, _, metadata, raw = self.envelope()
        for field in ("ciphertext", "tag", "wrapped_data_key_b64", "data_nonce_b64"):
            with self.subTest(field=field):
                changed, data = dict(metadata), raw
                if field in {"ciphertext", "tag"}:
                    offset = 0 if field == "ciphertext" else len(raw) - 1
                    data = raw[:offset] + bytes([raw[offset] ^ 1]) + raw[offset + 1:]
                else:
                    binary = base64.b64decode(changed[field])
                    changed[field] = base64.b64encode(bytes([binary[0] ^ 1]) + binary[1:]).decode()
                before = self.successes()
                with self.assertRaises(CryptoDecryptError):
                    self.decrypt(changed, (data,), len(data))
                self.assertEqual(self.successes(), before)

    def test_iterator_must_exhaust_even_after_exact_count_and_before_audit(self):
        value, _, metadata, raw = self.envelope()
        before = self.successes()
        def late_failure():
            yield raw
            self.assertEqual(self.successes(), before)
            raise ValueError("synthetic final source digest mismatch")
        with self.assertRaises(CryptoDecryptError) as caught:
            self.decrypt(metadata, late_failure(), len(raw))
        self.assertNotIn("source digest", str(caught.exception))
        self.assertEqual(self.successes(), before)
        self.assertEqual(value, b"synthetic plaintext" * 100)

    def test_exact_size_types_missing_extra_and_oversized_chunks_refused(self):
        _, _, metadata, raw = self.envelope()
        cases = ((raw[:-1],), (raw + b"x",), (b"",), (bytearray(raw),), (b"x" * 65537,))
        before = self.successes()
        for chunks in cases:
            with self.subTest(chunk_type=type(chunks[0]), size=len(chunks[0])):
                with self.assertRaises(CryptoDecryptError):
                    self.decrypt(metadata, chunks, len(raw))
        for size in (True, 15, -1, "100"):
            with self.assertRaises(CryptoDecryptError):
                self.decrypt(metadata, (raw,), size)
        self.assertEqual(self.successes(), before)

    def test_context_tenant_metadata_and_key_reference_fail_closed(self):
        from codex_web.services.identity import TenantIsolationError
        from codex_web.services.crypto_keys import CryptoKeyNotFoundError
        _, _, metadata, raw = self.envelope()
        before = self.successes()
        for changed in ({**metadata, "context_digest": "0" * 64},
                        {**metadata, "ciphertext_b64": "AA=="},
                        {**metadata, "format_version": "2.0"},
                        {**metadata, "extra": "not permitted"}):
            with self.assertRaises(CryptoDecryptError):
                self.decrypt(changed, (raw,), len(raw))
        with self.assertRaises(TenantIsolationError):
            self.service.decrypt_envelope_stream(metadata, (raw,), len(raw), self.context,
                actor=self.other, checkpoint=lambda: None)
        with self.assertRaises(CryptoKeyNotFoundError):
            self.decrypt({**metadata, "key_id": "unknown"}, (raw,), len(raw))
        self.assertEqual(self.successes(), before)

    def test_rotation_retains_old_version_but_terminal_revocation_refuses(self):
        value, _, metadata, raw = self.envelope()
        def rotating():
            yield raw
            self.service.rotate(self.key.id, actor=self.actor)
        self.assertEqual(self.decrypt(metadata, rotating(), len(raw)), value)
        before = self.successes()
        def revoking():
            yield raw
            self.service.revoke_version(self.key.id, 1, "synthetic test", actor=self.actor)
        with self.assertRaises(CryptoDecryptError):
            self.decrypt(metadata, revoking(), len(raw))
        self.assertEqual(self.successes(), before)
        with self.assertRaises(CryptoDecryptError):
            self.decrypt(metadata, (raw,), len(raw))

    def test_cancellation_clears_private_buffer_and_no_success_audit(self):
        _, _, metadata, raw = self.envelope()
        allocations = []
        real_bytearray = bytearray
        def allocate(size):
            buffer = real_bytearray(size)
            allocations.append(buffer)
            return buffer
        def cancel_after_plaintext():
            yield raw
            self.assertTrue(any(allocations[0]))
            raise asyncio.CancelledError()
        before = self.successes()
        with patch("codex_web.services.crypto_keys.bytearray", side_effect=allocate, create=True):
            with self.assertRaises(asyncio.CancelledError):
                self.decrypt(metadata, cancel_after_plaintext(), len(raw))
        self.assertEqual(allocations[0], bytes(len(allocations[0])))
        self.assertEqual(self.successes(), before)

    def test_checkpoint_cancellation_and_audit_failure_never_return_plaintext(self):
        _, _, metadata, raw = self.envelope()
        before = self.successes()
        def cancelled():
            raise TimeoutError("synthetic storage retirement")
        with self.assertRaises(TimeoutError):
            self.decrypt(metadata, (raw,), len(raw), checkpoint=cancelled)
        with patch.object(self.service.store, "update", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaises(CryptoDecryptError) as caught:
                self.decrypt(metadata, (raw,), len(raw))
            self.assertNotIn("audit failure", str(caught.exception))
        self.assertEqual(self.successes(), before)
