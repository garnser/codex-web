from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Iterable, Iterator
from typing import Any

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


class S3CompatibleArtifactContentStore:
    """S3/MinIO-compatible adapter with an injected credential-bound client."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        backend_id: str = "s3",
        prefix: str = "codex-web/artifacts",
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.backend_id = backend_id
        self.prefix = prefix.strip("/")

    def capabilities(self) -> frozenset[ArtifactContentCapability]:
        return frozenset(ArtifactContentCapability)

    @staticmethod
    def _scope_component(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _scope_prefix(self, scope: ArtifactContentScope) -> str:
        return (
            f"{self.prefix}/v1/{self._scope_component(scope.organization_id)}/"
            f"{self._scope_component(scope.workspace_id)}"
        )

    def _key(self, scope: ArtifactContentScope, digest: str) -> str:
        return f"{self._scope_prefix(scope)}/{digest[:2]}/{digest}"

    def _validate_locator(self, scope: ArtifactContentScope, locator: str) -> None:
        if not locator.startswith(self._scope_prefix(scope) + "/"):
            raise ArtifactContentAccessError("artifact content locator is outside tenant scope")

    @staticmethod
    def _error(exc: Exception) -> ArtifactContentError:
        response = getattr(exc, "response", None)
        code = None
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code")
        if str(code) in {"404", "NoSuchKey", "NotFound"}:
            return ArtifactContentNotFoundError("artifact content not found")
        return ArtifactContentError(str(exc))

    def put(
        self,
        scope: ArtifactContentScope,
        chunks: Iterable[bytes],
        *,
        media_type: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArtifactContentWriteResult:
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ArtifactContentError("artifact content chunks must be bytes")
                data = bytes(chunk)
                if not data:
                    continue
                digest.update(data)
                size += len(data)
                body.write(data)
            value = digest.hexdigest()
            if expected_sha256 is not None and value != expected_sha256:
                raise ArtifactContentIntegrityError(
                    f"artifact content digest mismatch: expected {expected_sha256}, got {value}"
                )
            body.seek(0)
            locator = self._key(scope, value)
            metadata = {
                "codex-sha256": value,
                "codex-size": str(size),
                "codex-tombstone": "0",
            }
            kwargs = {
                "Bucket": self.bucket,
                "Key": locator,
                "Body": body,
                "Metadata": metadata,
            }
            if media_type:
                kwargs["ContentType"] = media_type
            try:
                self.client.put_object(**kwargs)
            except Exception as exc:
                raise self._error(exc) from exc
        return ArtifactContentWriteResult(
            backend_id=self.backend_id,
            locator=locator,
            size_bytes=size,
            sha256=value,
            media_type=media_type,
            created_at=time.time(),
        )

    def head(
        self,
        scope: ArtifactContentScope,
        locator: str,
    ) -> ArtifactContentHead:
        self._validate_locator(scope, locator)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=locator)
        except Exception as exc:
            raise self._error(exc) from exc
        metadata = response.get("Metadata") or {}
        sha256 = str(metadata.get("codex-sha256") or locator.rsplit("/", 1)[-1])
        return ArtifactContentHead(
            backend_id=self.backend_id,
            locator=locator,
            size_bytes=int(metadata.get("codex-size") or response.get("ContentLength") or 0),
            sha256=sha256,
            media_type=response.get("ContentType"),
            tombstoned=str(metadata.get("codex-tombstone") or "0") == "1",
            modified_at=(
                response.get("LastModified").timestamp()
                if hasattr(response.get("LastModified"), "timestamp")
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
        head = self.head(scope, locator)
        if head.tombstoned:
            raise ArtifactContentNotFoundError("artifact content is tombstoned")
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": locator}
        if byte_range is not None:
            kwargs["Range"] = f"bytes={byte_range.start}-{byte_range.end_exclusive - 1}"
        try:
            response = self.client.get_object(**kwargs)
        except Exception as exc:
            raise self._error(exc) from exc
        body = response.get("Body")
        if body is None:
            raise ArtifactContentError("object store returned no response body")

        def generate() -> Iterator[bytes]:
            try:
                iterator = getattr(body, "iter_chunks", None)
                if callable(iterator):
                    for chunk in iterator(chunk_size=chunk_size):
                        if chunk:
                            yield bytes(chunk)
                    return
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    yield bytes(chunk)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

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
        self._validate_locator(scope, locator)
        try:
            existing = self.head(scope, locator)
        except ArtifactContentNotFoundError:
            existing = ArtifactContentHead(
                backend_id=self.backend_id,
                locator=locator,
                size_bytes=0,
                sha256=locator.rsplit("/", 1)[-1],
            )
        try:
            self.client.delete_object(Bucket=self.bucket, Key=locator)
            self.client.put_object(
                Bucket=self.bucket,
                Key=locator,
                Body=b"",
                Metadata={
                    "codex-sha256": existing.sha256,
                    "codex-size": str(existing.size_bytes),
                    "codex-tombstone": "1",
                },
                **({"ContentType": existing.media_type} if existing.media_type else {}),
            )
        except Exception as exc:
            raise self._error(exc) from exc
        return self.head(scope, locator)

    def health(self) -> ArtifactContentHealth:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            healthy = True
            detail = None
        except Exception as exc:
            healthy = False
            detail = str(exc)
        return ArtifactContentHealth(
            backend_id=self.backend_id,
            healthy=healthy,
            capabilities=tuple(sorted(self.capabilities(), key=lambda item: item.value)),
            detail=detail,
        )
