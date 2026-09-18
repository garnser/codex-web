from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceResult,
    EvidenceType,
)
from codex_web.extensions import (
    ExtensionConfigureRequest,
    ExtensionDeploymentMode,
    ExtensionGrantRequest,
    ExtensionHealthReport,
    ExtensionHealthStatus,
    ExtensionInstallRequest,
    ExtensionLifecycleState,
    ExtensionManifest,
    ExtensionRemoveRequest,
    ExtensionType,
    ExtensionUpgradeRequest,
)
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.extension_conformance import (
    ExtensionConformanceSuite,
    ExtensionRuntimeDescriptor,
)
from codex_web.services.extensions import (
    ExtensionAuthorizationError,
    ExtensionConflictError,
    ExtensionIntegrityError,
    ExtensionService,
    extension_version_satisfies,
)
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def manifest(
    extension_type: ExtensionType,
    *,
    extension_id: str | None = None,
    version: str = "1.2.3",
    compatibility: str = ">=3.0.0 <4.0.0",
    requested: tuple[str, ...] = ("extension.read",),
    mandatory: tuple[str, ...] = ("extension.read",),
    signature: str | None = None,
    secret_refs: tuple[str, ...] = (),
    config_schema: str | None = None,
    migration_entrypoint: str | None = None,
) -> ExtensionManifest:
    extension_id = extension_id or f"com.example.{extension_type.value.replace('_', '-')}"
    configuration: dict[str, object] = {
        "secret_refs": list(secret_refs),
    }
    if config_schema is not None:
        configuration["schema"] = config_schema
    migrations = (
        {"entrypoint": migration_entrypoint}
        if migration_entrypoint
        else {}
    )
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": version,
            "publisher": {
                "id": "com.example",
                "name": "Example",
            },
            "provenance": {
                "source": f"oci://registry.example/{extension_id}:{version}",
                "digest": "sha256:" + "a" * 64,
                "signature": signature,
            },
            "compatibility": {
                "codex_web": compatibility,
            },
            "types": [extension_type.value],
            "capabilities": {
                "requested": list(requested),
                "mandatory": list(mandatory),
            },
            "configuration": configuration,
            "migrations": migrations,
            "entrypoints": {
                extension_type.value: f"extension:{extension_type.value}",
            },
        }
    )


class ExtensionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.secrets = SecretBroker(
            SecretStateStore(self.sqlite),
            {"local": LocalFileSecretBackend(root / "secrets")},
        )
        self.artifact_evidence = ArtifactEvidenceService(
            ArtifactEvidenceStore(self.sqlite)
        )
        self.service = ExtensionService(
            ExtensionStateStore(self.sqlite),
            secrets=self.secrets,
            resources=self.resources,
            artifact_evidence=self.artifact_evidence,
            unhealthy_quarantine_threshold=3,
        )
        self.conformance = ExtensionConformanceSuite()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _install(
        self,
        extension_type: ExtensionType = ExtensionType.TASK_SOURCE,
        **manifest_overrides,
    ):
        item_manifest = manifest(extension_type, **manifest_overrides)
        return self.service.install(
            ExtensionInstallRequest(
                manifest=item_manifest,
                observed_digest=item_manifest.provenance.digest,
            ),
            actor=self.actor,
        )

    def _grant_and_enable(self, installation, *capabilities: str):
        grants = self.service.grant(
            installation.id,
            ExtensionGrantRequest(capabilities=capabilities),
            actor=self.actor,
        )
        enabled = self.service.enable(installation.id, actor=self.actor)
        return grants, enabled

    def test_install_enable_and_authorize_are_distinct_for_two_extension_types(self) -> None:
        for extension_type in (
            ExtensionType.TASK_SOURCE,
            ExtensionType.ACTION_PROVIDER,
        ):
            with self.subTest(extension_type=extension_type):
                installation = self._install(
                    extension_type,
                    extension_id=(
                        "com.example."
                        + extension_type.value.replace("_", "-")
                        + "-shared-lifecycle"
                    ),
                )
                self.assertEqual(
                    installation.lifecycle,
                    ExtensionLifecycleState.INSTALLED,
                )
                with self.assertRaises(ExtensionAuthorizationError):
                    self.service.enable(installation.id, actor=self.actor)

                grants, enabled = self._grant_and_enable(
                    installation,
                    "extension.read",
                )
                self.assertEqual(len(grants), 1)
                self.assertEqual(
                    enabled.lifecycle,
                    ExtensionLifecycleState.ENABLED,
                )

                descriptor = ExtensionRuntimeDescriptor(
                    extension_id=enabled.manifest.id,
                    version=enabled.manifest.version,
                    extension_type=extension_type,
                    capabilities=("extension.read",),
                )
                authorized = self.conformance.authorize_runtime(
                    self.service,
                    enabled.id,
                    descriptor,
                    actor=self.actor,
                )
                self.assertEqual(
                    tuple(item.capability for item in authorized),
                    ("extension.read",),
                )

    def test_extension_cannot_self_expand_beyond_declared_capabilities(self) -> None:
        installation = self._install()

        with self.assertRaises(ExtensionAuthorizationError):
            self.service.grant(
                installation.id,
                ExtensionGrantRequest(capabilities=("repository.write",)),
                actor=self.actor,
            )

        descriptor = ExtensionRuntimeDescriptor(
            extension_id=installation.manifest.id,
            version=installation.manifest.version,
            extension_type=ExtensionType.TASK_SOURCE,
            capabilities=("repository.write",),
        )
        with self.assertRaises(ExtensionAuthorizationError):
            self.conformance.validate_descriptor(
                installation.manifest,
                descriptor,
            )

    def test_resource_scoped_grant_is_enforced_at_runtime(self) -> None:
        resource = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.OTHER,
                name="Allowed target",
            ),
            actor=self.actor,
        )
        other = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.OTHER,
                name="Denied target",
            ),
            actor=self.actor,
        )
        installation = self._install()
        self.service.grant(
            installation.id,
            ExtensionGrantRequest(
                capabilities=("extension.read",),
                resource_ids=(resource.id,),
            ),
            actor=self.actor,
        )
        self.service.enable(installation.id, actor=self.actor)

        self.service.require_runtime_capability(
            installation.id,
            "extension.read",
            actor=self.actor,
            resource_ids=(resource.id,),
        )
        with self.assertRaises(ExtensionAuthorizationError):
            self.service.require_runtime_capability(
                installation.id,
                "extension.read",
                actor=self.actor,
                resource_ids=(other.id,),
            )

        with self.assertRaises(ExtensionAuthorizationError):
            self.service.require_runtime_capability(
                installation.id,
                "extension.read",
                actor=self.actor,
                resource_ids=(),
            )

    def test_secret_configuration_persists_reference_only(self) -> None:
        installation = self._install(
            secret_refs=("github_token",),
        )
        secret = self.secrets.create(
            SecretCreate(
                name="GitHub token",
                value="raw-extension-secret",
            ),
            actor=self.actor,
        )
        configured = self.service.configure(
            installation.id,
            ExtensionConfigureRequest(
                secret_bindings={"github_token": secret.id},
            ),
            actor=self.actor,
        )

        self.assertEqual(
            configured.secret_bindings,
            {"github_token": secret.id},
        )
        serialized = configured.model_dump_json()
        self.assertNotIn("raw-extension-secret", serialized)

        self.service.grant(
            installation.id,
            ExtensionGrantRequest(capabilities=("extension.read",)),
            actor=self.actor,
        )
        enabled = self.service.enable(installation.id, actor=self.actor)
        self.assertEqual(enabled.lifecycle, ExtensionLifecycleState.ENABLED)

    def test_unknown_secret_slot_is_rejected(self) -> None:
        installation = self._install(secret_refs=("github_token",))
        with self.assertRaises(ExtensionConflictError):
            self.service.configure(
                installation.id,
                ExtensionConfigureRequest(
                    secret_bindings={"aws_token": "secret-does-not-matter"},
                ),
                actor=self.actor,
            )

    def test_hosted_mode_fails_closed_without_real_signature_verifier(self) -> None:
        signed = manifest(
            ExtensionType.ACTION_PROVIDER,
            signature="sigstore:placeholder",
        )
        with self.assertRaises(ExtensionIntegrityError):
            self.service.install(
                ExtensionInstallRequest(
                    manifest=signed,
                    observed_digest=signed.provenance.digest,
                    deployment_mode=ExtensionDeploymentMode.HOSTED,
                ),
                actor=self.actor,
            )

    def test_digest_mismatch_is_rejected_before_install(self) -> None:
        item_manifest = manifest(ExtensionType.TASK_SOURCE)
        with self.assertRaises(ExtensionIntegrityError):
            self.service.install(
                ExtensionInstallRequest(
                    manifest=item_manifest,
                    observed_digest="sha256:" + "b" * 64,
                ),
                actor=self.actor,
            )
        self.assertEqual(self.service.list(self.actor), [])

    def test_incompatible_extension_is_persisted_but_cannot_enable(self) -> None:
        installation = self._install(
            compatibility=">=4.0.0 <5.0.0",
        )
        self.assertEqual(
            installation.lifecycle,
            ExtensionLifecycleState.INCOMPATIBLE,
        )
        self.assertIsNotNone(installation.incompatible_reason)
        with self.assertRaises(ExtensionConflictError):
            self.service.enable(installation.id, actor=self.actor)

    def test_configure_requires_disable_before_mutating_enabled_extension(self) -> None:
        installation = self._install()
        self._grant_and_enable(installation, "extension.read")

        with self.assertRaises(ExtensionConflictError):
            self.service.configure(
                installation.id,
                ExtensionConfigureRequest(),
                actor=self.actor,
            )

        current = self.service.get(installation.id, self.actor)
        self.assertEqual(
            current.lifecycle,
            ExtensionLifecycleState.ENABLED,
        )

    def test_three_unhealthy_reports_trip_circuit_breaker(self) -> None:
        installation = self._install()
        self._grant_and_enable(installation, "extension.read")

        current = None
        for attempt in range(3):
            current = self.service.report_health(
                installation.id,
                ExtensionHealthReport(
                    status=ExtensionHealthStatus.UNHEALTHY,
                    detail=f"failure {attempt + 1}",
                ),
                actor=self.actor,
            )

        assert current is not None
        self.assertEqual(
            current.lifecycle,
            ExtensionLifecycleState.QUARANTINED,
        )
        self.assertEqual(current.consecutive_health_failures, 3)
        with self.assertRaises(ExtensionAuthorizationError):
            self.service.require_runtime_capability(
                installation.id,
                "extension.read",
                actor=self.actor,
            )

        cleared = self.service.clear_quarantine(
            installation.id,
            actor=self.actor,
            reason="operator reviewed incident",
        )
        self.assertEqual(cleared.lifecycle, ExtensionLifecycleState.DISABLED)

    def test_revoking_mandatory_grant_quarantines_enabled_extension(self) -> None:
        installation = self._install()
        grants, _ = self._grant_and_enable(
            installation,
            "extension.read",
        )

        self.service.revoke_grant(
            installation.id,
            grants[0].id,
            actor=self.actor,
            reason="authority withdrawn",
        )

        current = self.service.get(installation.id, self.actor)
        self.assertEqual(
            current.lifecycle,
            ExtensionLifecycleState.QUARANTINED,
        )
        self.assertIn("mandatory capability revoked", current.quarantine_reason)

    def test_upgrade_requires_disable_and_preserves_manifest_history(self) -> None:
        installation = self._install(
            requested=("extension.read", "extension.legacy"),
            mandatory=("extension.read",),
        )
        self.service.grant(
            installation.id,
            ExtensionGrantRequest(
                capabilities=("extension.read", "extension.legacy"),
            ),
            actor=self.actor,
        )
        self.service.enable(installation.id, actor=self.actor)

        next_manifest = manifest(
            ExtensionType.TASK_SOURCE,
            extension_id=installation.manifest.id,
            version="2.0.0",
            requested=("extension.read",),
            mandatory=("extension.read",),
        )
        with self.assertRaises(ExtensionConflictError):
            self.service.upgrade(
                installation.id,
                ExtensionUpgradeRequest(
                    manifest=next_manifest,
                    observed_digest=next_manifest.provenance.digest,
                ),
                actor=self.actor,
            )

        self.service.disable(
            installation.id,
            actor=self.actor,
            reason="upgrade",
        )
        upgraded = self.service.upgrade(
            installation.id,
            ExtensionUpgradeRequest(
                manifest=next_manifest,
                observed_digest=next_manifest.provenance.digest,
            ),
            actor=self.actor,
        )

        self.assertEqual(upgraded.version, "2.0.0")
        self.assertEqual(upgraded.manifest_history[-1].version, "1.2.3")
        legacy = next(
            item
            for item in self.service.grants(installation.id, self.actor)
            if item.capability == "extension.legacy"
        )
        self.assertFalse(legacy.active)

    def test_declared_migration_requires_matching_canonical_evidence(self) -> None:
        installation = self._install()
        next_manifest = manifest(
            ExtensionType.TASK_SOURCE,
            extension_id=installation.manifest.id,
            version="1.3.0",
            migration_entrypoint="extension:migrate",
        )

        with self.assertRaises(ExtensionConflictError):
            self.service.upgrade(
                installation.id,
                ExtensionUpgradeRequest(
                    manifest=next_manifest,
                    observed_digest=next_manifest.provenance.digest,
                ),
                actor=self.actor,
            )

        wrong = self.artifact_evidence.create_evidence(
            EvidenceCreate(
                evidence_type=EvidenceType.POLICY_EVALUATION,
                result=EvidenceResult.PASS,
                metadata={
                    "extension_id": installation.manifest.id,
                    "to_version": "9.9.9",
                },
            ),
            actor=self.actor,
        )
        with self.assertRaises(ExtensionConflictError):
            self.service.upgrade(
                installation.id,
                ExtensionUpgradeRequest(
                    manifest=next_manifest,
                    observed_digest=next_manifest.provenance.digest,
                    migration_evidence_id=wrong.id,
                ),
                actor=self.actor,
            )

        evidence = self.artifact_evidence.create_evidence(
            EvidenceCreate(
                evidence_type=EvidenceType.POLICY_EVALUATION,
                result=EvidenceResult.PASS,
                metadata={
                    "extension_id": installation.manifest.id,
                    "to_version": next_manifest.version,
                },
            ),
            actor=self.actor,
        )
        upgraded = self.service.upgrade(
            installation.id,
            ExtensionUpgradeRequest(
                manifest=next_manifest,
                observed_digest=next_manifest.provenance.digest,
                migration_evidence_id=evidence.id,
            ),
            actor=self.actor,
        )
        self.assertEqual(upgraded.version, "1.3.0")

    def test_remove_is_tombstoned_and_revokes_grants(self) -> None:
        installation = self._install()
        grants = self.service.grant(
            installation.id,
            ExtensionGrantRequest(capabilities=("extension.read",)),
            actor=self.actor,
        )

        removed = self.service.remove(
            installation.id,
            ExtensionRemoveRequest(reason="retired"),
            actor=self.actor,
        )

        self.assertEqual(removed.lifecycle, ExtensionLifecycleState.REMOVED)
        current_grant = next(
            item
            for item in self.service.grants(installation.id, self.actor)
            if item.id == grants[0].id
        )
        self.assertFalse(current_grant.active)

        with self.assertRaises(ExtensionConflictError):
            self.service.remove(
                installation.id,
                ExtensionRemoveRequest(
                    reason="hard delete",
                    preserve_tombstone=False,
                ),
                actor=self.actor,
            )

    def test_cross_tenant_installations_are_not_visible(self) -> None:
        installation = self._install()
        foreign = self.actor.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )

        self.assertEqual(self.service.list(foreign), [])
        with self.assertRaises(Exception):
            self.service.get(installation.id, foreign)

    def test_compatibility_grammar_fails_closed(self) -> None:
        self.assertTrue(
            extension_version_satisfies(
                "3.0.0",
                ">=3.0.0 <4.0.0",
            )
        )
        self.assertFalse(
            extension_version_satisfies(
                "3.0.0",
                ">=4.0.0",
            )
        )
        with self.assertRaises(Exception):
            extension_version_satisfies(
                "3.0.0",
                "^3.0.0",
            )

    def test_audit_events_record_install_grant_enable_without_secret_material(self) -> None:
        installation = self._install()
        self._grant_and_enable(installation, "extension.read")

        events = self.service.events(self.actor)
        event_types = {item.event_type for item in events}
        self.assertIn("extension_installed", event_types)
        self.assertIn("extension_capability_granted", event_types)
        self.assertIn("extension_enabled", event_types)
        serialized = "".join(item.model_dump_json() for item in events)
        self.assertNotIn("lease_token", serialized)


if __name__ == "__main__":
    unittest.main()
