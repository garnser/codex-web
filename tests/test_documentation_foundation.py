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
    "docs/reference/api-authorization.md",
    "docs/troubleshooting/README.md",
    "docs/troubleshooting/operator-matrix.md",
    "docs/advanced-adoption/README.md",
    "docs/extensions/developer-guide.md",
    "docs/extensions/business-data-source-guide.md",
    "docs/advanced-adoption/business-operations.md",
    "docs/examples/business-operations-crm-billing.md",
    "docs/examples/business-executive-roles.md",
)

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
INTERNAL_ISSUE_REF_RE = re.compile(
    r"(?<![\\w./-])#\\d+\\b|https?://github\\.com/garnser/codex-web/issues/\\d+\\b",
    re.IGNORECASE,
)


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

    def test_documentation_has_no_internal_issue_references(self) -> None:
        references: list[str] = []
        sources = sorted(DOCS.rglob("*.md")) + [
            ROOT / "README.md",
            ROOT / "EXECUTIVE.md",
            ROOT / "DOCKER.md",
        ]
        for source in sources:
            text = source.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if INTERNAL_ISSUE_REF_RE.search(line):
                    references.append(
                        f"{source.relative_to(ROOT)}:{line_number}: {line.strip()}"
                    )
        self.assertEqual(
            references,
            [],
            "internal GitHub issue references in user documentation:\\n"
            + "\\n".join(references),
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
        text = " ".join(
            (
                ROOT / "docs/administration/definition-registry.md"
            ).read_text(encoding="utf-8").casefold().split()
        )
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

    def test_business_operations_adoption_package_is_complete_and_safe(self) -> None:
        documents = {
            "connector": ROOT / "docs/extensions/business-data-source-guide.md",
            "scenario": ROOT / "docs/examples/business-operations-crm-billing.md",
            "roles": ROOT / "docs/examples/business-executive-roles.md",
            "migration": ROOT / "docs/advanced-adoption/business-operations.md",
        }
        text = {
            key: path.read_text(encoding="utf-8").casefold()
            for key, path in documents.items()
        }
        for term in (
            "businessdatasource",
            "actionprovider",
            "actionintent",
            "secretreference",
            "cursor",
            "tombstone",
            "conformance",
            "read/sync",
        ):
            self.assertIn(term, text["connector"], term)
        for term in (
            "stale source",
            "provider disagreement",
            "revoked credential",
            "provider outage",
            "rate limit",
            "denied action authority",
            "expired approval",
            "unknown action result",
        ):
            self.assertIn(term, text["scenario"], term)
        for term in (
            "cfo",
            "cro",
            "cmo",
            "cpo",
            "customer success",
            "coo",
            "chief of staff",
        ):
            self.assertIn(term, text["roles"], term)
        for term in (
            "engineering-only",
            "businessdatasource",
            "business kpi",
            "advisory executive",
            "actionprovider/actionintent",
            "production qualification",
        ):
            self.assertIn(term, text["migration"], term)

    def test_business_operations_fixtures_are_synthetic_and_credential_free(self) -> None:
        for relative in (
            "tests/fixtures/company_operations_synthetic.json",
            "tests/fixtures/company_operations_healthy_synthetic.json",
        ):
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            text = path.read_text(encoding="utf-8")
            lowered = text.casefold()
            self.assertIn('"synthetic": true', lowered, relative)
            self.assertNotIn("sk-live-", text, relative)
            self.assertNotIn("ghp_", text, relative)
            self.assertNotIn("glpat-", text, relative)
            self.assertNotIn("private key", lowered, relative)
        degraded = (
            ROOT / "tests/fixtures/company_operations_synthetic.json"
        ).read_text(encoding="utf-8").casefold()
        healthy = (
            ROOT / "tests/fixtures/company_operations_healthy_synthetic.json"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn('"readiness": "partial"', degraded)
        self.assertIn('"provider_capacity": "throttled"', degraded)
        self.assertIn('"readiness": "current"', healthy)
        self.assertIn('"provider_capacity": "available"', healthy)

    def test_reference_business_data_source_example_is_read_only(self) -> None:
        root = ROOT / "examples/extensions/reference_business_data_source"
        for name in (
            "README.md",
            "adapter.py",
            "manifest.template.json",
            "build.py",
        ):
            self.assertTrue((root / name).is_file(), name)
        adapter = (root / "adapter.py").read_text(encoding="utf-8")
        for forbidden in (
            "async def write(",
            "async def create(",
            "async def update(",
            "async def delete(",
        ):
            self.assertNotIn(forbidden, adapter)
        readme = (root / "README.md").read_text(encoding="utf-8").casefold()
        self.assertIn("read/sync only", readme)
        self.assertIn("no provider-write method", readme)

    def test_root_readme_links_product_documentation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[product documentation](docs/README.md)", readme)


if __name__ == "__main__":
    unittest.main()
