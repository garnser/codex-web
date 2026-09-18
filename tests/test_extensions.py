from __future__ import annotations

import unittest

from pydantic import ValidationError

from codex_web.extensions import ExtensionLifecycleState, ExtensionManifest


def manifest_data() -> dict:
    return {
        "schema_version": "codex-web.extension/v1",
        "id": "com.example.github-task-source",
        "version": "1.2.3",
        "publisher": {"id": "com.example", "name": "Example"},
        "provenance": {
            "source": "oci://registry.example/extension@sha256:abc",
            "digest": "sha256:" + "a" * 64,
            "signature": "sigstore:example",
        },
        "compatibility": {"codex_web": ">=3.0.0 <4.0.0"},
        "types": ["task_source"],
        "capabilities": {
            "requested": ["network.github.read", "task.import"],
            "mandatory": ["task.import"],
        },
        "configuration": {
            "schema": "schemas/config.json",
            "secret_refs": ["github_token"],
        },
        "entrypoints": {"task_source": "extension.task_source:GitHubTaskSource"},
    }


class ExtensionManifestTests(unittest.TestCase):
    def test_manifest_accepts_canonical_contract_without_secret_values(self) -> None:
        manifest = ExtensionManifest.model_validate(manifest_data())

        self.assertEqual(
            manifest.identity,
            (
                "com.example.github-task-source",
                "1.2.3",
                "sha256:" + "a" * 64,
            ),
        )
        self.assertEqual(manifest.configuration.schema_path, "schemas/config.json")
        self.assertEqual(
            manifest.configuration.secret_refs,
            ("github_token",),
        )
        self.assertEqual(manifest.capabilities.mandatory, ("task.import",))

    def test_manifest_rejects_unknown_fields_before_extension_execution(self) -> None:
        data = manifest_data()
        data["authorization"] = {"granted": ["network.github.read"]}

        with self.assertRaisesRegex(
            ValidationError,
            "Extra inputs are not permitted",
        ):
            ExtensionManifest.model_validate(data)

    def test_manifest_rejects_invalid_identity_version_and_digest(self) -> None:
        for field, value in (
            ("id", "GitHubPlugin"),
            ("version", "latest"),
        ):
            with self.subTest(field=field):
                data = manifest_data()
                data[field] = value
                with self.assertRaises(ValidationError):
                    ExtensionManifest.model_validate(data)

        data = manifest_data()
        data["provenance"]["digest"] = "sha256:not-a-digest"
        with self.assertRaises(ValidationError):
            ExtensionManifest.model_validate(data)

    def test_manifest_requires_entrypoint_for_every_declared_type(self) -> None:
        data = manifest_data()
        data["types"] = ["task_source", "action_provider"]

        with self.assertRaisesRegex(
            ValidationError,
            "requires a matching entrypoint",
        ):
            ExtensionManifest.model_validate(data)

    def test_mandatory_capability_must_be_requested(self) -> None:
        data = manifest_data()
        data["capabilities"]["mandatory"] = ["repository.write"]

        with self.assertRaisesRegex(
            ValidationError,
            "mandatory capabilities must also be requested",
        ):
            ExtensionManifest.model_validate(data)

    def test_manifest_deduplicates_capabilities_and_types_deterministically(self) -> None:
        data = manifest_data()
        data["types"] = ["task_source", "task_source"]
        data["capabilities"]["requested"] = ["task.import", "task.import"]

        manifest = ExtensionManifest.model_validate(data)

        self.assertEqual(
            tuple(item.value for item in manifest.types),
            ("task_source",),
        )
        self.assertEqual(manifest.capabilities.requested, ("task.import",))

    def test_lifecycle_enum_matches_normative_contract(self) -> None:
        self.assertEqual(
            {state.value for state in ExtensionLifecycleState},
            {
                "discovered",
                "installed",
                "configured",
                "enabled",
                "disabled",
                "quarantined",
                "upgrading",
                "incompatible",
                "deprecated",
                "removed",
            },
        )


if __name__ == "__main__":
    unittest.main()
