from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_web.extension_packages import LocalExtensionPackageCatalog
from codex_web.extensions import ExtensionManifest


class ExtensionPackageBuildError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BuiltExtensionPackage:
    directory: Path
    manifest: ExtensionManifest
    payload_size_bytes: int


def build_extension_package(
    manifest_template: dict[str, Any],
    payload: Path,
    output_directory: Path,
) -> BuiltExtensionPackage:
    """Build the local package format consumed by LocalExtensionPackageCatalog.

    The payload remains opaque. The builder computes its SHA-256 digest, injects
    that digest into provenance, validates the final canonical manifest, and
    writes deterministic manifest JSON plus payload.cwext.
    """
    if payload.is_symlink():
        raise ExtensionPackageBuildError("extension payload may not be a symbolic link")
    try:
        resolved_payload = payload.resolve(strict=True)
    except OSError as exc:
        raise ExtensionPackageBuildError("extension payload does not exist") from exc
    if not resolved_payload.is_file():
        raise ExtensionPackageBuildError("extension payload must be a regular file")

    digest = hashlib.sha256()
    size = 0
    with resolved_payload.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)

    manifest_data = json.loads(json.dumps(manifest_template))
    provenance = manifest_data.get("provenance")
    if not isinstance(provenance, dict):
        raise ExtensionPackageBuildError("manifest template requires provenance metadata")
    provenance["digest"] = "sha256:" + digest.hexdigest()
    manifest = ExtensionManifest.model_validate(manifest_data)

    output_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = output_directory / LocalExtensionPackageCatalog.manifest_filename
    payload_path = output_directory / LocalExtensionPackageCatalog.payload_filename
    manifest_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(resolved_payload, payload_path)
    return BuiltExtensionPackage(output_directory, manifest, size)
