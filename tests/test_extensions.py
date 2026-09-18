from __future__ import annotations

import pytest
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
        "configuration": {"schema": "schemas/config.json", "secret_refs": ["github_token"]},
        "entrypoints": {"task_source": "extension.task_source:GitHubTaskSource"},
    }


def test_manifest_accepts_canonical_contract_without_secret_values() -> None:
    manifest = ExtensionManifest.model_validate(manifest_data())

    assert manifest.identity == (
        "com.example.github-task-source",
        "1.2.3",
        "sha256:" + "a" * 64,
    )
    assert manifest.configuration.schema_path == "schemas/config.json"
    assert manifest.configuration.secret_refs == ("github_token",)
    assert manifest.capabilities.mandatory == ("task.import",)


def test_manifest_rejects_unknown_fields_before_extension_execution() -> None:
    data = manifest_data()
    data["authorization"] = {"granted": ["network.github.read"]}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExtensionManifest.model_validate(data)


def test_manifest_rejects_invalid_identity_version_and_digest() -> None:
    for field, value in (
        ("id", "GitHubPlugin"),
        ("version", "latest"),
    ):
        data = manifest_data()
        data[field] = value
        with pytest.raises(ValidationError):
            ExtensionManifest.model_validate(data)

    data = manifest_data()
    data["provenance"]["digest"] = "sha256:not-a-digest"
    with pytest.raises(ValidationError):
        ExtensionManifest.model_validate(data)


def test_manifest_requires_entrypoint_for_every_declared_type() -> None:
    data = manifest_data()
    data["types"] = ["task_source", "action_provider"]

    with pytest.raises(ValidationError, match="requires a matching entrypoint"):
        ExtensionManifest.model_validate(data)


def test_mandatory_capability_must_be_requested() -> None:
    data = manifest_data()
    data["capabilities"]["mandatory"] = ["repository.write"]

    with pytest.raises(ValidationError, match="mandatory capabilities must also be requested"):
        ExtensionManifest.model_validate(data)


def test_manifest_deduplicates_capabilities_and_types_deterministically() -> None:
    data = manifest_data()
    data["types"] = ["task_source", "task_source"]
    data["capabilities"]["requested"] = ["task.import", "task.import"]

    manifest = ExtensionManifest.model_validate(data)

    assert tuple(item.value for item in manifest.types) == ("task_source",)
    assert manifest.capabilities.requested == ("task.import",)


def test_lifecycle_enum_matches_normative_contract() -> None:
    assert {state.value for state in ExtensionLifecycleState} == {
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
    }
