from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.extension_builder import ExtensionPackageBuildError, build_extension_package
from codex_web.extension_packages import LocalExtensionPackageCatalog


def manifest_template() -> dict[str, object]:
    return {
        "id": "com.codex-web.reference-task-source",
        "version": "1.0.0",
        "publisher": {"id": "com.codex-web", "name": "codex-web"},
        "provenance": {
            "source": "reference-extension",
            "digest": "sha256:" + "0" * 64,
        },
        "compatibility": {"codex_web": ">=3.0.0 <4.0.0"},
        "types": ["task_source"],
        "entrypoints": {"task_source": "reference_task_source:create"},
    }


class ExtensionBuilderTests(unittest.TestCase):
    def test_builder_round_trips_through_package_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "reference.py"
            payload.write_bytes(b"reference-extension\n")
            package_dir = root / "packages" / "reference"

            built = build_extension_package(manifest_template(), payload, package_dir)
            discovery = LocalExtensionPackageCatalog(root / "packages").discover()

            self.assertEqual(discovery.errors, ())
            self.assertEqual(len(discovery.candidates), 1)
            candidate = discovery.candidates[0]
            self.assertEqual(candidate.manifest, built.manifest)
            self.assertTrue(candidate.verification.digest_verified)
            self.assertEqual(candidate.payload_size_bytes, len(b"reference-extension\n"))

    def test_manifest_output_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "payload"
            payload.write_bytes(b"same bytes")
            first = root / "first"
            second = root / "second"

            build_extension_package(manifest_template(), payload, first)
            build_extension_package(manifest_template(), payload, second)

            self.assertEqual(
                (first / "manifest.json").read_bytes(),
                (second / "manifest.json").read_bytes(),
            )
            parsed = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(parsed["provenance"]["digest"], "sha256:" + "0" * 64)

    def test_symlink_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "payload"
            target.write_bytes(b"payload")
            link = root / "payload-link"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")

            with self.assertRaisesRegex(ExtensionPackageBuildError, "symbolic link"):
                build_extension_package(manifest_template(), link, root / "package")


if __name__ == "__main__":
    unittest.main()
