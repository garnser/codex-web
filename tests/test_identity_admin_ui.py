from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IdentityAdminUiTests(unittest.TestCase):
    def test_identity_admin_uses_canonical_metadata_and_revocation_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "identity_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="identity-admin-status"', html)
        self.assertIn('id="identity-sessions"', html)
        self.assertIn('id="identity-service-tokens"', html)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/identity")', javascript)
        self.assertIn("external_links", javascript)
        self.assertIn("groups_snapshot", javascript)
        self.assertIn("actor.assurance", javascript)
        self.assertIn("step_up_until", javascript)
        self.assertIn("state.memberships", javascript)
        self.assertIn("state.teams", javascript)
        self.assertIn("state.recovery_factors", javascript)
        self.assertIn(
            '/api/identity/sessions/${encodeURIComponent(sessionId)}',
            javascript,
        )
        self.assertIn('apiRequest("/api/identity/sessions/revoke-others"', javascript)
        self.assertIn(
            '/api/identity/service-tokens/${encodeURIComponent(tokenId)}',
            javascript,
        )
        self.assertIn('method: "DELETE"', javascript)
        self.assertIn("Raw service-token material is never returned", javascript)
        self.assertNotIn("token_hash", javascript)
        self.assertNotIn("session_token_hash", javascript)
        self.assertNotIn("refresh_token_hash", javascript)
        self.assertIn("codex:identity-state-rendered", javascript)


if __name__ == "__main__":
    unittest.main()
