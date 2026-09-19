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
    DOCS / "examples",
    DOCS / "screenshots",
    DOCS / "extensions",
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
    "docs/administration/platform-administration.md",
    "docs/administration/definition-registry.md",
    "docs/operations/README.md",
    "docs/operations/upgrade-and-rollback.md",
    "docs/operations/uninstall.md",
    "docs/operations/runbooks.md",
    "docs/operations/release-readiness.md",
    "docs/reference/README.md",
    "docs/reference/documentation-versioning.md",
    "docs/reference/platform-contracts.md",
    "docs/troubleshooting/README.md",
    "docs/troubleshooting/operator-matrix.md",
    "docs/advanced-adoption/README.md",
    "docs/extensions/developer-guide.md",
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

    def test_operator_docs_cover_critical_release_readiness_topics(self) -> None:
        documents = {
            "admin": ROOT / "docs/administration/platform-administration.md",
            "definitions": ROOT / "docs/administration/definition-registry.md",
            "runbooks": ROOT / "docs/operations/runbooks.md",
            "troubleshooting": ROOT / "docs/troubleshooting/operator-matrix.md",
            "reference": ROOT / "docs/reference/platform-contracts.md",
            "extensions": ROOT / "docs/extensions/developer-guide.md",
            "readiness": ROOT / "docs/operations/release-readiness.md",
        }
        text = "\n".join(
            path.read_text(encoding="utf-8").casefold()
            for path in documents.values()
        )
        for term in (
            "definition registry",
            "mfa",
            "service identit",
            "secret",
            "encryption",
            "execution worker",
            "extension",
            "approval",
            "actionintent",
            "release",
            "incident",
            "backup",
            "restore",
            "rpo",
            "rto",
            "upgrade",
            "version skew",
            "rollback",
            "reconciliation",
            "split brain",
            "audit",
            "evidence",
        ):
            self.assertIn(term, text, term)

    def test_definition_admin_guide_documents_lifecycle_and_code_boundary(self) -> None:
        text = (
            ROOT / "docs/administration/definition-registry.md"
        ).read_text(encoding="utf-8").casefold()
        for term in (
            "draft",
            "validate",
            "publish",
            "supersede",
            "rollback",
            "definitions are data",
            "engines and structural security are code",
            "execution_contracts.py",
            "cache",
            "historical attribution",
        ):
            self.assertIn(term, text, term)

    def test_runbooks_cover_critical_verified_procedures(self) -> None:
        text = (
            ROOT / "docs/operations/runbooks.md"
        ).read_text(encoding="utf-8").casefold()
        for heading in (
            "startup and shutdown",
            "release promotion and rollback",
            "upgrade, migration and version skew",
            "backup, restore and recovery drill",
            "incident response",
            "execution worker drain",
            "extension upgrade",
            "key rotation",
            "replicated/failover mode",
            "unknown provider outcome",
        ):
            self.assertIn(heading, text, heading)

    def test_root_readme_links_product_documentation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[product documentation](docs/README.md)", readme)


if __name__ == "__main__":
    unittest.main()
