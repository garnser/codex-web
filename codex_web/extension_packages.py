from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from codex_web.extensions import (
    ExtensionManifest,
    ExtensionPackageVerification,
    ExtensionSignatureStatus,
)


class ExtensionPackageCatalogError(RuntimeError):
    pass


class ExtensionPackageNotFoundError(ExtensionPackageCatalogError):
    pass


@dataclass(frozen=True, slots=True)
class ExtensionPackageCandidate:
    package_ref: str
    relative_directory: str
    manifest: ExtensionManifest
    payload_size_bytes: int
    verification: ExtensionPackageVerification

    def metadata(self) -> dict[str, object]:
        return {
            "package_ref": self.package_ref,
            "relative_directory": self.relative_directory,
            "manifest": self.manifest.model_dump(mode="json", by_alias=True),
            "payload_size_bytes": self.payload_size_bytes,
            "verification": self.verification.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class ExtensionPackageDiscoveryError:
    relative_directory: str
    error: str

    def metadata(self) -> dict[str, str]:
        return {
            "relative_directory": self.relative_directory,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ExtensionPackageDiscovery:
    candidates: tuple[ExtensionPackageCandidate, ...]
    errors: tuple[ExtensionPackageDiscoveryError, ...]


class LocalExtensionPackageCatalog:
    """Bounded metadata-only package discovery for self-hosted deployments.

    Package directories contain:
      - manifest.json: immutable ExtensionManifest metadata
      - payload.cwext: opaque package bytes whose SHA-256 is declared by manifest

    Discovery does not extract or import payload content.
    """

    manifest_filename = "manifest.json"
    payload_filename = "payload.cwext"

    def __init__(
        self,
        root: Path,
        *,
        max_packages: int = 500,
        max_manifest_bytes: int = 1024 * 1024,
        max_payload_bytes: int = 256 * 1024 * 1024,
        hash_chunk_bytes: int = 1024 * 1024,
    ) -> None:
        self.root = root
        self.max_packages = max(1, max_packages)
        self.max_manifest_bytes = max(1024, max_manifest_bytes)
        self.max_payload_bytes = max(1024, max_payload_bytes)
        self.hash_chunk_bytes = max(4096, hash_chunk_bytes)

    def _root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root.resolve()

    @staticmethod
    def _package_ref(relative_directory: str) -> str:
        digest = hashlib.sha256(relative_directory.encode("utf-8")).hexdigest()
        return f"pkg-{digest[:24]}"

    @staticmethod
    def _safe_regular_file(
        root: Path,
        package_dir: Path,
        path: Path,
    ) -> Path:
        if package_dir.is_symlink() or path.is_symlink():
            raise ExtensionPackageCatalogError(
                "extension package paths may not be symbolic links"
            )
        resolved_dir = package_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if not resolved_dir.is_relative_to(root) or not resolved.is_relative_to(resolved_dir):
            raise ExtensionPackageCatalogError(
                "extension package path escapes configured package root"
            )
        if not resolved.is_file():
            raise ExtensionPackageCatalogError(
                f"required package file is not a regular file: {path.name}"
            )
        return resolved

    def _manifest(
        self,
        root: Path,
        package_dir: Path,
    ) -> ExtensionManifest:
        path = self._safe_regular_file(
            root,
            package_dir,
            package_dir / self.manifest_filename,
        )
        size = path.stat().st_size
        if size > self.max_manifest_bytes:
            raise ExtensionPackageCatalogError(
                f"extension manifest exceeds {self.max_manifest_bytes} bytes"
            )
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExtensionPackageCatalogError(
                f"extension manifest is unreadable: {type(exc).__name__}"
            ) from exc
        try:
            return ExtensionManifest.model_validate(payload)
        except ValidationError as exc:
            raise ExtensionPackageCatalogError(
                f"extension manifest is invalid: {exc.errors()[0]['msg']}"
            ) from exc

    def _payload_digest(
        self,
        root: Path,
        package_dir: Path,
    ) -> tuple[Path, int, str]:
        path = self._safe_regular_file(
            root,
            package_dir,
            package_dir / self.payload_filename,
        )
        size = path.stat().st_size
        if size > self.max_payload_bytes:
            raise ExtensionPackageCatalogError(
                f"extension payload exceeds {self.max_payload_bytes} bytes"
            )
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(self.hash_chunk_bytes)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_payload_bytes:
                    raise ExtensionPackageCatalogError(
                        f"extension payload exceeds {self.max_payload_bytes} bytes"
                    )
                digest.update(chunk)
        return path, total, "sha256:" + digest.hexdigest()

    def _candidate(
        self,
        root: Path,
        package_dir: Path,
    ) -> ExtensionPackageCandidate:
        manifest = self._manifest(root, package_dir)
        _, size, observed_digest = self._payload_digest(root, package_dir)
        digest_verified = observed_digest == manifest.provenance.digest
        signature_status = (
            ExtensionSignatureStatus.UNVERIFIED
            if manifest.provenance.signature
            else ExtensionSignatureStatus.UNSIGNED
        )
        relative_directory = package_dir.resolve(strict=True).relative_to(root).as_posix()
        return ExtensionPackageCandidate(
            package_ref=self._package_ref(relative_directory),
            relative_directory=relative_directory,
            manifest=manifest,
            payload_size_bytes=size,
            verification=ExtensionPackageVerification(
                digest_verified=digest_verified,
                signature_status=signature_status,
                verifier="local-package-catalog-v1",
                observed_digest=observed_digest,
                detail=(
                    None
                    if digest_verified
                    else "server-computed payload digest does not match manifest"
                ),
            ),
        )

    def discover(self) -> ExtensionPackageDiscovery:
        root = self._root()
        candidates: list[ExtensionPackageCandidate] = []
        errors: list[ExtensionPackageDiscoveryError] = []
        directories = sorted(
            (
                path
                for path in root.iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.name,
        )
        if len(directories) > self.max_packages:
            errors.append(
                ExtensionPackageDiscoveryError(
                    relative_directory=".",
                    error=(
                        f"package count {len(directories)} exceeds "
                        f"configured maximum {self.max_packages}"
                    ),
                )
            )
            directories = directories[: self.max_packages]
        for package_dir in directories:
            try:
                candidates.append(self._candidate(root, package_dir))
            except (ExtensionPackageCatalogError, OSError) as exc:
                errors.append(
                    ExtensionPackageDiscoveryError(
                        relative_directory=package_dir.name,
                        error=str(exc),
                    )
                )
        return ExtensionPackageDiscovery(
            candidates=tuple(candidates),
            errors=tuple(errors),
        )

    def get(self, package_ref: str) -> ExtensionPackageCandidate:
        discovery = self.discover()
        item = next(
            (
                candidate
                for candidate in discovery.candidates
                if candidate.package_ref == package_ref
            ),
            None,
        )
        if item is None:
            raise ExtensionPackageNotFoundError("extension package not found")
        return item
