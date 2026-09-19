from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

USER_DOC_ROOTS = (
    DOCS / "README.md",
    DOCS / "guide-template.md",
    DOCS / "getting-started",
    DOCS / "core-concepts",
    DOCS / "tutorials",
    DOCS / "how-to",
    DOCS / "administration",
    DOCS / "operations",
    DOCS / "reference",
    DOCS / "troubleshooting",
    DOCS / "advanced-adoption",
)

REQUIRED_DOCS = (
    "docs/README.md",
    "docs/guide-template.md",
    "docs/getting-started/README.md",
    "docs/getting-started/installation.md",
    "docs/getting-started/first-run.md",
    "docs/core-concepts/README.md",
    "docs/tutorials/README.md",
    "docs/tutorials/first-successful-task.md",
    "docs/tutorials/first-task-source.md",
    "docs/how-to/README.md",
    "docs/administration/README.md",
    "docs/administration/trust-and-credentials.md",
    "docs/operations/README.md",
    "docs/operations/upgrade-and-rollback.md",
    "docs/operations/uninstall.md",
    "docs/reference/README.md",
    "docs/reference/documentation-versioning.md",
    "docs/troubleshooting/README.md",
    "docs/advanced-adoption/README.md",
)

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in USER_DOC_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
    return sorted(set(files))


class DocumentationFoundationTests(unittest.TestCase):
    def test_required_m12_information_architecture_exists(self) -> None:
        missing = [
            path
            for path in REQUIRED_DOCS
            if not (ROOT / path).is_file()
        ]
        self.assertEqual(missing, [], f"missing M12 documentation: {missing}")

    def test_user_documentation_relative_links_resolve(self) -> None:
        broken: list[str] = []
        for source in _markdown_files():
            text = source.read_text(encoding="utf-8")
            for raw_target in LINK_RE.findall(text):
                target = raw_target.strip()
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                # Markdown destinations may contain a title after whitespace.
                target = target.split()[0].strip("<>")
                path_part = unquote(target.split("#", 1)[0].split("?", 1)[0])
                if not path_part:
                    continue
                resolved = (source.parent / path_part).resolve()
                try:
                    resolved.relative_to(ROOT.resolve())
                except ValueError:
                    broken.append(
                        f"{source.relative_to(ROOT)} -> {raw_target} (escapes repository)"
                    )
                    continue
                if not resolved.exists():
                    broken.append(
                        f"{source.relative_to(ROOT)} -> {raw_target}"
                    )
        self.assertEqual(
            broken,
            [],
            "broken relative documentation links:\n" + "\n".join(broken),
        )

    def test_getting_started_documents_verification_and_recovery(self) -> None:
        for relative in (
            "docs/getting-started/installation.md",
            "docs/getting-started/first-run.md",
            "docs/tutorials/first-successful-task.md",
            "docs/tutorials/first-task-source.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8").casefold()
            self.assertIn("verify", text, relative)
            self.assertIn("recovery", text, relative)

    def test_root_readme_links_product_documentation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[product documentation](docs/README.md)", readme)


if __name__ == "__main__":
    unittest.main()
