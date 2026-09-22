from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TOKEN_FILE = STATIC / "design_tokens.css"
INDEX_FILE = STATIC / "index.html"

TOKEN_DEFINITION = re.compile(r"(--cw-[a-z0-9-]+)\s*:")
TOKEN_REFERENCE = re.compile(r"var\(\s*(--cw-[a-z0-9-]+)")


class DesignTokenTests(unittest.TestCase):
    def test_foundation_tokens_are_defined_and_loaded_first(self) -> None:
        token_css = TOKEN_FILE.read_text(encoding="utf-8")
        defined = set(TOKEN_DEFINITION.findall(token_css))
        required = {
            "--cw-font-sans",
            "--cw-font-size-md",
            "--cw-space-1",
            "--cw-space-4",
            "--cw-control-md",
            "--cw-radius-md",
            "--cw-shadow-sm",
            "--cw-color-bg",
            "--cw-color-surface",
            "--cw-color-border",
            "--cw-color-text",
            "--cw-color-accent",
            "--cw-color-success",
            "--cw-color-warning",
            "--cw-color-danger",
            "--cw-focus-ring",
            "--cw-disabled-opacity",
            "--cw-transition-fast",
            "--cw-breakpoint-phone",
            "--cw-breakpoint-tablet",
            "--cw-breakpoint-wide",
        }
        self.assertEqual(required - defined, set())

        index = INDEX_FILE.read_text(encoding="utf-8")
        token_link = 'href="static/design_tokens.css"'
        app_link = 'href="static/styles.css"'
        self.assertIn(token_link, index)
        self.assertIn(app_link, index)
        self.assertLess(index.index(token_link), index.index(app_link))

    def test_all_shared_token_references_resolve(self) -> None:
        defined = set(
            TOKEN_DEFINITION.findall(TOKEN_FILE.read_text(encoding="utf-8"))
        )
        references: set[str] = set()
        for css_path in STATIC.glob("*.css"):
            references.update(
                TOKEN_REFERENCE.findall(css_path.read_text(encoding="utf-8"))
            )

        self.assertEqual(
            references - defined,
            set(),
            "shared CSS references an undefined --cw-* token",
        )

    def test_theme_and_reduced_motion_contracts_are_present(self) -> None:
        token_css = TOKEN_FILE.read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="dark"]', token_css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", token_css)
        self.assertIn("--cw-transition-fast: 0ms", token_css)
        self.assertIn("--cw-transition-standard: 0ms", token_css)


if __name__ == "__main__":
    unittest.main()
