from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CryptoKeyAdminUiTests(unittest.TestCase):
    def test_key_admin_uses_reference_only_crypto_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "crypto_key_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="crypto-key-list"', html)
        self.assertIn('id="crypto-backend-health"', html)
        self.assertIn('apiRequest("/api/crypto/keys")', javascript)
        self.assertIn('apiRequest("/api/crypto/backend-health")', javascript)
        self.assertIn('apiRequest("/api/crypto/manifest")', javascript)
        self.assertIn('apiRequest("/api/crypto/events")', javascript)
        self.assertIn("version.backend_ref", javascript)
        self.assertIn("key.scope?.organization_id", javascript)
        self.assertIn("key.scope?.workspace_id", javascript)
        self.assertIn("key.current_version", javascript)
        self.assertIn(
            '/api/crypto/keys/${encodeURIComponent(keyId)}/rotate',
            javascript,
        )
        self.assertIn(
            '/api/crypto/keys/${encodeURIComponent(keyId)}/versions/${encodeURIComponent(version)}/revoke',
            javascript,
        )
        self.assertIn(
            '/api/crypto/keys/${encodeURIComponent(keyId)}/revoke',
            javascript,
        )
        self.assertIn("becomes decrypt-only", javascript)
        self.assertIn("may become undecryptable", javascript)
        self.assertIn("Key material is never exposed", javascript)
        self.assertNotIn("ciphertext_b64", javascript)
        self.assertNotIn("wrapped_data_key_b64", javascript)
        self.assertNotIn("localStorage", javascript)


if __name__ == "__main__":
    unittest.main()
