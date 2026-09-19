from __future__ import annotations

import hashlib
import unittest

from codex_web.retrieval import (
    DeterministicLocalEmbeddingProvider,
    LocalLexicalRetrievalBackend,
    LocalVectorRetrievalBackend,
    RetrievalIndexDocument,
    RetrievalSearchRequest,
)


def _doc(
    knowledge_id: str,
    *,
    text: str,
    version: int = 1,
    organization_id: str = "org-a",
    workspace_id: str = "ws-a",
    project_id: str | None = "project-a",
    object_type: str = "architecture_decision",
    lifecycle: str = "current",
) -> RetrievalIndexDocument:
    digest = hashlib.sha256(
        f"{knowledge_id}:{version}:{text}".encode("utf-8")
    ).hexdigest()
    return RetrievalIndexDocument(
        knowledge_id=knowledge_id,
        canonical_version=version,
        content_sha256=digest,
        organization_id=organization_id,
        workspace_id=workspace_id,
        project_id=project_id,
        object_type=object_type,
        lifecycle=lifecycle,
        title=text,
        summary=text,
        content=text,
        tags=("architecture",),
        indexed_text=text,
    )


class RetrievalBackendConformanceMixin:
    def backend(self):
        raise NotImplementedError

    def test_rebuild_search_scope_allowlist_and_delete(self):
        backend = self.backend()
        first = _doc(
            "knowledge-a",
            text="PostgreSQL database architecture decision",
        )
        second = _doc(
            "knowledge-b",
            text="Customer onboarding product workflow",
            object_type="product",
        )
        foreign = _doc(
            "knowledge-foreign",
            text="PostgreSQL database architecture decision",
            organization_id="org-other",
            workspace_id="ws-other",
        )
        backend.rebuild((first, second, foreign))

        status = backend.status()
        self.assertTrue(status.healthy)
        self.assertEqual(status.document_count, 3)
        first_revision = status.index_revision

        hits = backend.search(
            RetrievalSearchRequest(
                text="database architecture",
                organization_id="org-a",
                workspace_id="ws-a",
                allowed_knowledge_ids=("knowledge-a", "knowledge-b"),
                project_ids=("project-a",),
                object_types=("architecture_decision",),
                lifecycles=("current",),
                limit=10,
            )
        )
        self.assertEqual([item.knowledge_id for item in hits], ["knowledge-a"])
        self.assertEqual(hits[0].canonical_version, 1)
        self.assertEqual(hits[0].content_sha256, first.content_sha256)
        self.assertEqual(hits[0].index_revision, first_revision)

        denied_by_allowlist = backend.search(
            RetrievalSearchRequest(
                text="database architecture",
                organization_id="org-a",
                workspace_id="ws-a",
                allowed_knowledge_ids=("knowledge-b",),
                limit=10,
            )
        )
        self.assertNotIn(
            "knowledge-a",
            {item.knowledge_id for item in denied_by_allowlist},
        )
        self.assertNotIn(
            "knowledge-foreign",
            {item.knowledge_id for item in denied_by_allowlist},
        )

        updated = _doc(
            "knowledge-a",
            text="PostgreSQL database architecture decision revised",
            version=2,
        )
        backend.upsert(updated)
        refreshed = backend.search(
            RetrievalSearchRequest(
                text="database architecture revised",
                organization_id="org-a",
                workspace_id="ws-a",
                allowed_knowledge_ids=("knowledge-a",),
                limit=5,
            )
        )
        self.assertEqual(refreshed[0].canonical_version, 2)
        self.assertEqual(refreshed[0].content_sha256, updated.content_sha256)
        self.assertNotEqual(
            backend.status().index_revision,
            first_revision,
        )

        backend.delete("knowledge-a")
        after_delete = backend.search(
            RetrievalSearchRequest(
                text="database",
                organization_id="org-a",
                workspace_id="ws-a",
                allowed_knowledge_ids=("knowledge-a",),
                limit=5,
            )
        )
        self.assertEqual(after_delete, ())


class LocalLexicalRetrievalBackendTests(
    RetrievalBackendConformanceMixin,
    unittest.TestCase,
):
    def backend(self):
        return LocalLexicalRetrievalBackend()

    def test_reports_lexical_capability_without_embedding_identity(self):
        status = self.backend().status()
        self.assertIn("lexical", status.capabilities)
        self.assertNotIn("vector", status.capabilities)
        self.assertIsNone(status.embedding_identity)


class LocalVectorRetrievalBackendTests(
    RetrievalBackendConformanceMixin,
    unittest.TestCase,
):
    def backend(self):
        return LocalVectorRetrievalBackend(
            DeterministicLocalEmbeddingProvider(
                provider_id="local",
                model_id="concept-hash",
                model_revision="1",
            )
        )

    def test_embedding_revision_change_requires_rebuild_and_is_attributable(self):
        backend = self.backend()
        document = _doc(
            "knowledge-a",
            text="database architecture decision",
        )
        backend.rebuild((document,))
        initial = backend.status()
        self.assertTrue(initial.healthy)
        self.assertEqual(initial.embedding_identity.model_revision, "1")

        backend.embedding_provider = DeterministicLocalEmbeddingProvider(
            provider_id="local",
            model_id="concept-hash",
            model_revision="2",
        )
        stale = backend.status()
        self.assertFalse(stale.healthy)
        self.assertIn("rebuild required", stale.last_error)

        backend.rebuild((document,))
        rebuilt = backend.status()
        self.assertTrue(rebuilt.healthy)
        self.assertEqual(rebuilt.embedding_identity.model_revision, "2")
        hit = backend.search(
            RetrievalSearchRequest(
                text="database architecture",
                organization_id="org-a",
                workspace_id="ws-a",
                allowed_knowledge_ids=("knowledge-a",),
                limit=5,
            )
        )[0]
        self.assertEqual(hit.embedding_identity.model_revision, "2")
        self.assertIn("semantic:", " ".join(hit.reasons))


if __name__ == "__main__":
    unittest.main()
