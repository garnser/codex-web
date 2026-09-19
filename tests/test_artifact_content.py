from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from codex_web.artifact_content import (
    ArtifactContentAccessError,
    ArtifactContentIntegrityError,
    ArtifactContentRange,
    ArtifactContentScope,
)
from codex_web.services.artifact_content import (
    ArtifactContentRegistry,
    ArtifactContentService,
)
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.s3_artifact_content import S3CompatibleArtifactContentStore


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _body(value):
        if hasattr(value, "read"):
            return value.read()
        return bytes(value)

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": self._body(kwargs["Body"]),
            "Metadata": dict(kwargs.get("Metadata") or {}),
            "ContentType": kwargs.get("ContentType"),
        }
        return {}

    def head_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {
            "Metadata": dict(item["Metadata"]),
            "ContentLength": len(item["Body"]),
            "ContentType": item["ContentType"],
        }

    def get_object(self, *, Bucket, Key, Range=None):
        item = self.objects[(Bucket, Key)]
        data = item["Body"]
        if Range:
            raw = Range.removeprefix("bytes=")
            start_text, end_text = raw.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            data = data[start : end + 1]
        return {"Body": io.BytesIO(data)}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}

    def head_bucket(self, *, Bucket):
        return {"Bucket": Bucket}


class ArtifactContentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = ArtifactContentScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.other_scope = ArtifactContentScope(
            organization_id="org-b",
            workspace_id="ws-b",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_local_store_streams_ranges_and_verifies_digest(self) -> None:
        store = LocalArtifactContentStore(self.root / "content")
        payload = b"abcdefghijklmnopqrstuvwxyz"
        result = store.put(
            self.scope,
            (payload[:7], payload[7:19], payload[19:]),
            media_type="text/plain",
        )

        self.assertEqual(result.size_bytes, len(payload))
        self.assertEqual(
            b"".join(store.open(self.scope, result.locator)),
            payload,
        )
        self.assertEqual(
            b"".join(
                store.open(
                    self.scope,
                    result.locator,
                    byte_range=ArtifactContentRange(
                        start=5,
                        end_exclusive=12,
                    ),
                )
            ),
            payload[5:12],
        )
        head = store.verify(self.scope, result.locator, result.sha256)
        self.assertEqual(head.sha256, result.sha256)
        self.assertEqual(head.media_type, "text/plain")
        self.assertFalse(head.tombstoned)

    def test_locator_alone_cannot_cross_tenant_boundary(self) -> None:
        store = LocalArtifactContentStore(self.root / "content")
        result = store.put(self.scope, (b"tenant-a",))

        with self.assertRaises(ArtifactContentAccessError):
            store.head(self.other_scope, result.locator)
        with self.assertRaises(ArtifactContentAccessError):
            list(store.open(self.other_scope, result.locator))

    def test_local_corruption_and_expected_digest_mismatch_fail_closed(self) -> None:
        store = LocalArtifactContentStore(self.root / "content")
        result = store.put(self.scope, (b"original",))

        with self.assertRaises(ArtifactContentIntegrityError):
            store.put(
                self.scope,
                (b"different",),
                expected_sha256=result.sha256,
            )

        path = store._resolve(self.scope, result.locator)
        path.write_bytes(b"corrupt")
        with self.assertRaises(ArtifactContentIntegrityError):
            store.verify(self.scope, result.locator, result.sha256)

    def test_delete_leaves_tombstone_but_no_readable_bytes(self) -> None:
        store = LocalArtifactContentStore(self.root / "content")
        result = store.put(self.scope, (b"delete-me",))

        tombstone = store.delete(self.scope, result.locator)

        self.assertTrue(tombstone.tombstoned)
        with self.assertRaises(Exception):
            list(store.open(self.scope, result.locator))

    def test_s3_compatible_adapter_uses_same_contract(self) -> None:
        client = _FakeS3()
        store = S3CompatibleArtifactContentStore(
            client,
            bucket="artifact-bucket",
        )
        payload = b"object-store-payload"
        result = store.put(
            self.scope,
            (payload,),
            media_type="application/octet-stream",
        )

        self.assertEqual(
            b"".join(store.open(self.scope, result.locator)),
            payload,
        )
        self.assertEqual(
            b"".join(
                store.open(
                    self.scope,
                    result.locator,
                    byte_range=ArtifactContentRange(start=2, end_exclusive=8),
                )
            ),
            payload[2:8],
        )
        self.assertEqual(
            store.verify(self.scope, result.locator, result.sha256).sha256,
            result.sha256,
        )
        self.assertTrue(store.health().healthy)

    def test_copy_preserves_digest_across_backends(self) -> None:
        local = LocalArtifactContentStore(
            self.root / "local",
            backend_id="local",
        )
        s3 = S3CompatibleArtifactContentStore(
            _FakeS3(),
            bucket="artifact-bucket",
            backend_id="s3",
        )
        registry = ArtifactContentRegistry()
        registry.register(local)
        registry.register(s3)
        service = ArtifactContentService(registry)
        payload = b"portable-content"
        source = service.put(self.scope, (payload,), backend_id="local")

        copied = service.copy(
            self.scope,
            source_backend_id="local",
            source_locator=source.locator,
            target_backend_id="s3",
            expected_sha256=source.sha256,
            media_type="application/octet-stream",
        )

        self.assertEqual(copied.sha256, source.sha256)
        self.assertEqual(
            b"".join(
                service.open(
                    self.scope,
                    backend_id="s3",
                    locator=copied.locator,
                )
            ),
            payload,
        )


if __name__ == "__main__":
    unittest.main()
