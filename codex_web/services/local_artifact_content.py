from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from codex_web.artifact_content import (
    ArtifactContentAccessError,
    ArtifactContentCapability,
    ArtifactContentError,
    ArtifactContentHealth,
    ArtifactContentHead,
    ArtifactContentIntegrityError,
    ArtifactContentNotFoundError,
    ArtifactContentRange,
    ArtifactContentScope,
    ArtifactContentWriteResult,
)


class LocalArtifactContentStore:
    """Tenant-scoped content-addressable filesystem backend."""

    backend_id = "local"

    def __init__(self, root: Path, *, backend_id: str = "local") -> None:
        self.root = Path(root)
        self.backend_id = backend_id

    def capabilities(self) -> frozenset[ArtifactContentCapability]:
        return frozenset(ArtifactContentCapability)

    @staticmethod
    def _scope_component(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _scope_prefix(self, scope: ArtifactContentScope) -> str:
        return (
            f"v1/{self._scope_component(scope.organization_id)}/"
            f"{self._scope_component(scope.workspace_id)}"
        )

    def _locator(self, scope: ArtifactContentScope, digest: str) -> str:
        return f"{self._scope_prefix(scope)}/{digest[:2]}/{digest}"

    def _resolve(self, scope: ArtifactContentScope, locator: str) -> Path:
        prefix = self._scope_prefix(scope) + "/"
        if not locator.startswith(prefix):
            raise ArtifactContentAccessError("artifact content locator is outside tenant scope")
        parts = locator.split("/")
        if len(parts) != 5 or parts[0] != "v1":
            raise ArtifactContentAccessError("invalid artifact content locator")
        digest = parts[-1]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ArtifactContentAccessError("invalid artifact content digest locator")
        candidate = self.root.joinpath(*parts)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactContentAccessError("invalid artifact content locator") from exc
        return candidate

    @staticmethod
    def _metadata_path(path: Path) -> Path:
        return path.with_name(path.name + ".meta.json")

    def _read_metadata(self, path: Path) -> dict:
        metadata_path = self._metadata_path(path)
        if not metadata_path.exists():
            if not path.exists():
                raise ArtifactContentNotFoundError("artifact content not found")
            return {
                "sha256": path.name,
                "size_bytes": path.stat().st_size,
                "media_type": None,
                "created_at": path.stat().st_mtime,
                "tombstoned": False,
            }
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactContentError("artifact content metadata is unreadable") from exc
        if not isinstance(payload, dict):
            raise ArtifactContentError("artifact content metadata is invalid")
        return payload

    def _write_metadata(self, path: Path, payload: dict) -> None:
        metadata_path = self._metadata_path(path)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=metadata_path.parent,
            prefix=".meta-",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, metadata_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def put(
        self,
        scope: ArtifactContentScope,
        chunks: Iterable[bytes],
        *,
        media_type: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArtifactContentWriteResult:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary_root = self.root / ".tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=temporary_root, prefix="artifact-")
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ArtifactContentError("artifact content chunks must be bytes")
                    data = bytes(chunk)
                    if not data:
                        continue
                    digest.update(data)
                    size += len(data)
                    handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            value = digest.hexdigest()
            if expected_sha256 is not None and value != expected_sha256:
                raise ArtifactContentIntegrityError(
                    f"artifact content digest mismatch: expected {expected_sha256}, got {value}"
                )
            locator = self._locator(scope, value)
            target = self._resolve(scope, locator)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing = self.verify(scope, locator, value)
                if existing.size_bytes != size:
                    raise ArtifactContentIntegrityError(
                        "existing artifact content size does not match uploaded content"
                    )
                os.unlink(temporary)
            else:
                os.replace(temporary, target)
            created_at = time.time()
            self._write_metadata(
                target,
                {
                    "sha256": value,
                    "size_bytes": size,
                    "media_type": media_type,
                    "created_at": created_at,
                    "tombstoned": False,
                },
            )
            return ArtifactContentWriteResult(
                backend_id=self.backend_id,
                locator=locator,
                size_bytes=size,
                sha256=value,
                media_type=media_type,
                created_at=created_at,
            )
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def head(
        self,
        scope: ArtifactContentScope,
        locator: str,
    ) -> ArtifactContentHead:
        path = self._resolve(scope, locator)
        metadata = self._read_metadata(path)
        tombstoned = bool(metadata.get("tombstoned"))
        size = int(metadata.get("size_bytes") or 0)
        if not tombstoned and not path.exists():
            raise ArtifactContentNotFoundError("artifact content payload is missing")
        return ArtifactContentHead(
            backend_id=self.backend_id,
            locator=locator,
            size_bytes=size,
            sha256=str(metadata.get("sha256") or path.name),
            media_type=metadata.get("media_type"),
            tombstoned=tombstoned,
            modified_at=(
                self._metadata_path(path).stat().st_mtime
                if self._metadata_path(path).exists()
                else path.stat().st_mtime
                if path.exists()
                else None
            ),
        )

    def open(
        self,
        scope: ArtifactContentScope,
        locator: str,
        *,
        byte_range: ArtifactContentRange | None = None,
        chunk_size: int = 65536,
    ) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        path = self._resolve(scope, locator)
        head = self.head(scope, locator)
        if head.tombstoned:
            raise ArtifactContentNotFoundError("artifact content is tombstoned")
        start = byte_range.start if byte_range is not None else 0
        end = (
            min(byte_range.end_exclusive, head.size_bytes)
            if byte_range is not None
            else head.size_bytes
        )
        if start >= head.size_bytes and head.size_bytes:
            return iter(())

        def generate() -> Iterator[bytes]:
            remaining = max(0, end - start)
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining:
                    chunk = handle.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return generate()

    def verify(
        self,
        scope: ArtifactContentScope,
        locator: str,
        expected_sha256: str,
    ) -> ArtifactContentHead:
        head = self.head(scope, locator)
        if head.tombstoned:
            raise ArtifactContentNotFoundError("artifact content is tombstoned")
        digest = hashlib.sha256()
        size = 0
        for chunk in self.open(scope, locator):
            digest.update(chunk)
            size += len(chunk)
        value = digest.hexdigest()
        if value != expected_sha256 or value != head.sha256 or size != head.size_bytes:
            raise ArtifactContentIntegrityError(
                f"artifact content verification failed for {locator}"
            )
        return head.model_copy(update={"modified_at": time.time()})

    def delete(
        self,
        scope: ArtifactContentScope,
        locator: str,
    ) -> ArtifactContentHead:
        path = self._resolve(scope, locator)
        try:
            metadata = self._read_metadata(path)
        except ArtifactContentNotFoundError:
            digest = path.name
            metadata = {
                "sha256": digest,
                "size_bytes": 0,
                "media_type": None,
                "created_at": time.time(),
            }
        if path.exists():
            path.unlink()
        metadata["tombstoned"] = True
        metadata["deleted_at"] = time.time()
        self._write_metadata(path, metadata)
        return self.head(scope, locator)

    def health(self) -> ArtifactContentHealth:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            healthy = self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)
            detail = None if healthy else "content directory is not readable/writable"
        except OSError as exc:
            healthy = False
            detail = str(exc)
        return ArtifactContentHealth(
            backend_id=self.backend_id,
            healthy=healthy,
            capabilities=tuple(sorted(self.capabilities(), key=lambda item: item.value)),
            detail=detail,
        )
