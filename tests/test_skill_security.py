from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_web.definitions import DefinitionReference
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.skill_scanners import SkillScannerRegistry, SkillSpectorCliProvider
from codex_web.services.skill_security import SkillSecurityBlocked, SkillSecurityService
from codex_web.services.skill_catalog import SkillCatalogService
from codex_web.services.skills import SkillService
from codex_web.extensions import ExtensionEntrypoints, ExtensionManifest, ExtensionPublisher, ExtensionProvenance, ExtensionCompatibility, ExtensionType
from codex_web.skill_security import (
    SkillFindingSeverity,
    SkillScanMode,
    SkillScanProviderResult,
    SkillScanRequest,
    SkillSecurityBaselineRequest,
    SkillSecurityFinding,
    SkillSecurityOverrideRequest,
    SkillSecurityPolicyUpdate,
)
from codex_web.skills import SkillCreate, SkillOrigin, SkillProvenance, SkillPublish, SkillUpdate
from codex_web.skill_catalog import SkillSourceCreate, SkillSourceSyncRequest, SkillSourceTrust
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.skill_catalog import SkillSourceStore
from codex_web.storage.skill_security import SkillSecurityStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def admin() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="workspace-a",
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class FakeScanner:
    provider_id = "nvidia-skillspector"

    def __init__(self) -> None:
        self.result = SkillScanProviderResult(scanner_version="1.2.3", risk_score=0)
        self.calls = []

    def metadata(self):
        return {"providerId": self.provider_id, "transport": "test"}

    def scan(self, *, content, mode):
        self.calls.append((content, mode))
        return self.result


class SkillSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.state = state
        definitions = DefinitionRegistryService(DefinitionRegistryStore(state))
        registry = SkillScannerRegistry()
        self.scanner = FakeScanner()
        registry.register(self.scanner)
        self.security = SkillSecurityService(SkillSecurityStore(state), definitions, registry, clock=lambda: 100.0)
        self.skills = SkillService(definitions, clock=lambda: 100.0)
        self.skills.bind_security(self.security)
        self.actor = admin()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def imported(self, skill_id="external-review"):
        return self.skills.create(
            SkillCreate(
                skill_id=skill_id,
                name="External review",
                instructions="Review the current change.",
                provenance=SkillProvenance(
                    source_type="catalog_bundle",
                    source_id="external",
                    upstream_id="review/SKILL.md",
                    source_revision="abc123",
                    origin=SkillOrigin.IMPORTED,
                ),
            ),
            actor=self.actor,
        )

    def test_imported_revision_requires_current_passing_scan_for_publish_and_assignment(self) -> None:
        draft = self.imported()
        with self.assertRaisesRegex(SkillSecurityBlocked, "current_revision_not_scanned"):
            self.skills.publish(draft["skillId"], draft["recordId"], SkillPublish(), actor=self.actor)

        self.assertEqual(self.security.scan(draft, SkillScanRequest(), actor=self.actor)["status"], "passed")
        published = self.skills.publish(draft["skillId"], draft["recordId"], SkillPublish(), actor=self.actor)
        self.skills.validate_reference(DefinitionReference.model_validate(published["definitionReference"]), actor=self.actor)

        changed = self.skills.update(
            draft["skillId"],
            SkillUpdate(instructions="Changed upstream instructions.", reason="sync"),
            actor=self.actor,
        )
        self.assertEqual(changed["security"]["status"], "stale")
        with self.assertRaisesRegex(SkillSecurityBlocked, "current_revision_not_scanned"):
            self.skills.publish(
                changed["skillId"],
                changed["recordId"],
                SkillPublish(expected_active_revision=published["revision"]),
                actor=self.actor,
            )

    def test_findings_are_queryable_baseline_is_audited_and_risk_quarantines(self) -> None:
        finding = SkillSecurityFinding(
            rule_id="prompt-injection",
            severity=SkillFindingSeverity.CRITICAL,
            category="prompt-injection",
            message="Instruction attempts to override system policy.",
        )
        self.scanner.result = SkillScanProviderResult(scanner_version="1.2.3", risk_score=95, findings=(finding,))
        draft = self.imported("risky")
        summary = self.security.scan(draft, SkillScanRequest(), actor=self.actor)
        self.assertEqual(summary["status"], "quarantined")
        self.assertEqual(summary["scan"]["findings"][0]["rule_id"], "prompt-injection")
        self.security.set_baseline(
            draft,
            SkillSecurityBaselineRequest(finding_fingerprints=(finding.fingerprint,), reason="reviewed false positive"),
            actor=self.actor,
        )
        self.assertEqual(self.security.scan(draft, SkillScanRequest(), actor=self.actor)["status"], "quarantined")
        report = self.security.report(draft, actor=self.actor)
        self.assertEqual(report["baseline"]["finding_fingerprints"], [finding.fingerprint])
        self.assertIn("skill_security_baseline_changed", {item["event_type"] for item in report["audit"]})

    def test_warning_and_extension_manifest_scanner_type(self) -> None:
        self.scanner.result = SkillScanProviderResult(
            scanner_version="1.2.3",
            risk_score=12,
            findings=(SkillSecurityFinding(rule_id="review", severity=SkillFindingSeverity.MEDIUM, message="Review tool scope."),),
        )
        self.assertEqual(self.security.scan(self.imported("warning"), SkillScanRequest(), actor=self.actor)["status"], "warning")
        manifest = ExtensionManifest(
            id="com.nvidia.skillspector",
            version="1.0.0",
            publisher=ExtensionPublisher(id="nvidia", name="NVIDIA"),
            provenance=ExtensionProvenance(source="oci://scanner", digest="sha256:" + "a" * 64),
            compatibility=ExtensionCompatibility(codex_web=">=3.0.0 <4.0.0"),
            types=(ExtensionType.SKILL_SCANNER,),
            entrypoints=ExtensionEntrypoints(skill_scanner="skillspector:provider"),
        )
        self.assertEqual(manifest.types, (ExtensionType.SKILL_SCANNER,))

    def test_scanner_error_fails_closed_and_override_requires_explicit_policy(self) -> None:
        class Broken(FakeScanner):
            def scan(self, **_kwargs):
                raise RuntimeError("scanner unavailable")

        self.security.providers.unregister("nvidia-skillspector")
        self.security.providers.register(Broken())
        draft = self.imported("broken")
        self.assertEqual(self.security.scan(draft, SkillScanRequest(), actor=self.actor)["status"], "scan_error")
        self.security.create_override(draft, SkillSecurityOverrideRequest(reason="incident exception", expires_at=200), actor=self.actor)
        self.assertFalse(self.security.decision(draft, actor=self.actor, phase="publish")["allowed"])
        current = self.security.policy(self.actor).model_dump(mode="python")
        self.security.update_policy(
            SkillSecurityPolicyUpdate(**{**current, "allow_audited_override": True}, reason="enable audited emergency exceptions"),
            actor=self.actor,
        )
        self.assertTrue(self.security.decision(draft, actor=self.actor, phase="publish")["overridden"])

    def test_cli_adapter_uses_bounded_static_json_transport_without_llm(self) -> None:
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout='{"version":"2.0.0","risk_score":12,"findings":[]}', stderr="")

        result = SkillSpectorCliProvider(run=run).scan(content="# Safe skill", mode=SkillScanMode.STATIC)
        self.assertEqual(result.scanner_version, "2.0.0")
        self.assertIn("--static-only", calls[0][0])
        self.assertNotIn("shell", calls[0][1])
        self.assertEqual(calls[0][1]["env"], {"PATH": "/usr/local/bin:/usr/bin:/bin"})

    def test_external_catalog_ingestion_invokes_static_scan_before_publication(self) -> None:
        catalog = SkillCatalogService(SkillSourceStore(self.state), self.skills, clock=lambda: 100.0)
        catalog.create(
            SkillSourceCreate(
                source_id="external",
                name="External",
                location="bundle:external",
                trust=SkillSourceTrust.APPROVED,
            ),
            actor=self.actor,
        )
        payload = SkillSourceSyncRequest.model_validate(
            {
                "source_revision": "abc123",
                "entries": [
                    {
                        "upstream_id": "review/SKILL.md",
                        "manifest": {
                            "skill_id": "catalog-review",
                            "name": "Catalog review",
                            "instructions": "Review changes.",
                        },
                    }
                ],
            }
        )
        result = catalog.sync("external", payload, actor=self.actor)
        self.assertEqual(result["items"][0]["security"]["status"], "passed")
        self.assertEqual(self.scanner.calls[-1][1], SkillScanMode.STATIC)
        draft = self.skills.get("catalog-review", actor=self.actor)
        self.assertEqual(draft["definitionLifecycle"], "draft")


if __name__ == "__main__":
    unittest.main()
