from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_web.extension_packages import LocalExtensionPackageCatalog
from codex_web.extensions import (
    ExtensionDeploymentMode,
    ExtensionLifecycleState,
    ExtensionSignatureStatus,
)
from codex_web.services.extensions import ExtensionIntegrityError, ExtensionService
from codex_web.services.identity import IdentityService
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _manifest_payload(
    payload: bytes,
    *,
    extension_id: str = "com.example.package-test",
    version: str = "1.0.0",
    declared_digest: str | None = None,
) -> dict[str, object]:
    digest = declared_digest or "sha256:" + hashlib.sha256(payload).hexdigest()
    return {
        "schema_version": "codex-web.extension/v1",
        "id": extension_id,
        "version": version,
        "publisher": {
            "id": "com.example",
            "name": "Example",
        },
        "provenance": {
            "source": f"local-package:{extension_id}:{version}",
            "digest": digest,
        },
        "compatibility": {
            "codex_web": ">=3.0.0 <4.0.0",
        },
        "types": ["task_source"],
        "capabilities": {
            "requested": ["task.read"],
            "mandatory": ["task.read"],
        },
        "entrypoints": {
            "task_source": "extension:task_source",
        },
    }


def _write_package(
    root: Path,
    name: str,
    payload: bytes,
    *,
    manifest: dict[str, object] | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "payload.cwext").write_bytes(payload)
    (directory / "manifest.json").write_text(
        json.dumps(manifest or _manifest_payload(payload)),
        encoding="utf-8",
    )
    return directory


class ExtensionPackageCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "packages"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_discovery_computes_payload_digest_server_side(self) -> None:
        payload = b"opaque extension package bytes"
        _write_package(self.root, "example", payload)

        discovery = LocalExtensionPackageCatalog(self.root).discover()

        self.assertEqual(len(discovery.candidates), 1)
        self.assertEqual(discovery.errors, ())
        candidate = discovery.candidates[0]
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        self.assertEqual(candidate.verification.observed_digest, expected)
        self.assertTrue(candidate.verification.digest_verified)
        self.assertEqual(
            candidate.verification.signature_status,
            ExtensionSignatureStatus.UNSIGNED,
        )
        self.assertEqual(candidate.payload_size_bytes, len(payload))
        self.assertNotIn("payload", candidate.metadata())

    def test_digest_mismatch_is_visible_and_cannot_install(self) -> None:
        payload = b"actual bytes"
        declared = "sha256:" + hashlib.sha256(b"different bytes").hexdigest()
        _write_package(
            self.root,
            "mismatch",
            payload,
            manifest=_manifest_payload(
                payload,
                declared_digest=declared,
            ),
        )
        catalog = LocalExtensionPackageCatalog(self.root)
        candidate = catalog.discover().candidates[0]
        self.assertFalse(candidate.verification.digest_verified)

        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        actor = identity.local_trusted_actor()
        service = ExtensionService(ExtensionStateStore(sqlite))

        with self.assertRaises(ExtensionIntegrityError):
            service.install_verified_package(
                manifest=candidate.manifest,
                verification=candidate.verification,
                deployment_mode=ExtensionDeploymentMode.SELF_HOSTED,
                actor=actor,
                package_ref=candidate.package_ref,
            )
        self.assertEqual(service.list(actor), [])

    def test_server_verified_candidate_installs_through_canonical_lifecycle(self) -> None:
        payload = b"verified package"
        _write_package(self.root, "verified", payload)
        candidate = LocalExtensionPackageCatalog(self.root).discover().candidates[0]

        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        actor = identity.local_trusted_actor()
        service = ExtensionService(ExtensionStateStore(sqlite))

        installed = service.install_verified_package(
            manifest=candidate.manifest,
            verification=candidate.verification,
            deployment_mode=ExtensionDeploymentMode.SELF_HOSTED,
            actor=actor,
            package_ref=candidate.package_ref,
        )

        self.assertEqual(installed.lifecycle, ExtensionLifecycleState.INSTALLED)
        self.assertEqual(
            installed.package_verification.verifier,
            "local-package-catalog-v1",
        )
        events = service.events(actor)
        event = next(
            item
            for item in events
            if item.event_type == "extension_installed"
        )
        self.assertEqual(event.details["package_ref"], candidate.package_ref)

    def test_payload_symlink_is_rejected_without_following_target(self) -> None:
        self.root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"outside-secret-payload")
        directory = self.root / "symlinked"
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps(_manifest_payload(b"outside-secret-payload")),
            encoding="utf-8",
        )
        try:
            os.symlink(outside, directory / "payload.cwext")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this platform")

        discovery = LocalExtensionPackageCatalog(self.root).discover()

        self.assertEqual(discovery.candidates, ())
        self.assertEqual(len(discovery.errors), 1)
        self.assertIn("symbolic links", discovery.errors[0].error)

    def test_oversized_manifest_and_payload_are_discovery_errors(self) -> None:
        payload = b"x" * 32
        _write_package(self.root, "too-large-payload", payload)
        catalog = LocalExtensionPackageCatalog(
            self.root,
            max_manifest_bytes=4096,
            max_payload_bytes=16,
        )

        discovery = catalog.discover()

        self.assertEqual(discovery.candidates, ())
        self.assertEqual(len(discovery.errors), 1)
        self.assertIn("payload exceeds", discovery.errors[0].error)

        other_root = Path(self.temp.name) / "manifests"
        directory = _write_package(other_root, "too-large-manifest", b"x")
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(" " * 5000, encoding="utf-8")
        other_catalog = LocalExtensionPackageCatalog(
            other_root,
            max_manifest_bytes=4096,
        )

        other = other_catalog.discover()

        self.assertEqual(other.candidates, ())
        self.assertEqual(len(other.errors), 1)
        self.assertIn("manifest exceeds", other.errors[0].error)

    def test_invalid_manifest_is_reported_without_loading_payload_code(self) -> None:
        directory = self.root / "invalid"
        directory.mkdir(parents=True)
        (directory / "payload.cwext").write_bytes(
            b"this is never imported or executed"
        )
        (directory / "manifest.json").write_text(
            json.dumps({"id": "not-valid"}),
            encoding="utf-8",
        )

        discovery = LocalExtensionPackageCatalog(self.root).discover()

        self.assertEqual(discovery.candidates, ())
        self.assertEqual(len(discovery.errors), 1)
        self.assertIn("manifest is invalid", discovery.errors[0].error)

    def test_package_count_is_bounded_deterministically(self) -> None:
        self.root.mkdir(parents=True)
        for name in ("a", "b", "c"):
            _write_package(
                self.root,
                name,
                name.encode("utf-8"),
                manifest=_manifest_payload(
                    name.encode("utf-8"),
                    extension_id=f"com.example.package-{name}",
                ),
            )
        discovery = LocalExtensionPackageCatalog(
            self.root,
            max_packages=2,
        ).discover()

        self.assertEqual(
            tuple(item.relative_directory for item in discovery.candidates),
            ("a", "b"),
        )
        self.assertTrue(
            any("package count 3 exceeds" in item.error for item in discovery.errors)
        )


if __name__ == "__main__":
    unittest.main()
