from __future__ import annotations

from collections.abc import Iterable, Iterator

from codex_web.artifact_content import (
    ArtifactContentError,
    ArtifactContentHealth,
    ArtifactContentRange,
    ArtifactContentScope,
    ArtifactContentStore,
    ArtifactContentWriteResult,
)


class ArtifactContentRegistry:
    def __init__(self) -> None:
        self._stores: dict[str, ArtifactContentStore] = {}

    def register(self, store: ArtifactContentStore) -> None:
        backend_id = str(store.backend_id).strip()
        if not backend_id:
            raise ValueError("artifact content backend id is required")
        if backend_id in self._stores and self._stores[backend_id] is not store:
            raise ValueError(f"artifact content backend already registered: {backend_id}")
        self._stores[backend_id] = store

    def get(self, backend_id: str) -> ArtifactContentStore:
        try:
            return self._stores[backend_id]
        except KeyError as exc:
            raise ArtifactContentError(
                f"artifact content backend is not registered: {backend_id}"
            ) from exc

    def list_health(self) -> tuple[ArtifactContentHealth, ...]:
        return tuple(
            self._stores[key].health()
            for key in sorted(self._stores)
        )


class ArtifactContentService:
    """Backend selection/copy boundary; canonical Artifact metadata stays elsewhere."""

    def __init__(
        self,
        registry: ArtifactContentRegistry,
        *,
        default_backend_id: str = "local",
    ) -> None:
        self.registry = registry
        self.default_backend_id = default_backend_id

    def backend(self, backend_id: str | None = None) -> ArtifactContentStore:
        return self.registry.get(backend_id or self.default_backend_id)

    def put(
        self,
        scope: ArtifactContentScope,
        chunks: Iterable[bytes],
        *,
        backend_id: str | None = None,
        media_type: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArtifactContentWriteResult:
        return self.backend(backend_id).put(
            scope,
            chunks,
            media_type=media_type,
            expected_sha256=expected_sha256,
        )

    def open(
        self,
        scope: ArtifactContentScope,
        *,
        backend_id: str,
        locator: str,
        byte_range: ArtifactContentRange | None = None,
        chunk_size: int = 65536,
    ) -> Iterator[bytes]:
        return self.backend(backend_id).open(
            scope,
            locator,
            byte_range=byte_range,
            chunk_size=chunk_size,
        )

    def copy(
        self,
        scope: ArtifactContentScope,
        *,
        source_backend_id: str,
        source_locator: str,
        target_backend_id: str,
        expected_sha256: str,
        media_type: str | None = None,
    ) -> ArtifactContentWriteResult:
        source = self.backend(source_backend_id)
        source.verify(scope, source_locator, expected_sha256)
        return self.backend(target_backend_id).put(
            scope,
            source.open(scope, source_locator),
            media_type=media_type,
            expected_sha256=expected_sha256,
        )

    def health(self) -> tuple[ArtifactContentHealth, ...]:
        return self.registry.list_health()
