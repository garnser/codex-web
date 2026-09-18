from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class ExtensionAuthorityUiTests(unittest.TestCase):
    def test_extension_mutation_authority_uses_canonical_actor_metadata_only(self) -> None:
        path = STATIC / "extension_authority.js"
        source = path.read_text(encoding="utf-8")

        self.assertLessEqual(path.stat().st_size, 3_000)
        self.assertIn('actor.principal_kind === "service"', source)
        self.assertIn('includes("extensions:admin")', source)
        self.assertIn('["mfa", "local_trusted"].includes(actor.assurance)', source)
        self.assertIn('["owner", "admin"].includes(role)', source)
        self.assertIn("current assurance", source)
        self.assertIn("extensions:admin service scope required", source)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("localStorage", source)


if __name__ == "__main__":
    unittest.main()
