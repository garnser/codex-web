from __future__ import annotations

import types
import unittest

from codex_web.model_gateway import ModelLifecycle, ModelProviderStatus
from codex_web.retrieval import EmbeddingModelIdentity
from codex_web.services.retrieval_embedding import (
    EmbeddingIdentityValidationError,
    ModelGatewayEmbeddingIdentityValidator,
)


class _Gateway:
    def __init__(self, providers, models):
        self.providers = providers
        self.models = models

    def list_providers(self, actor):
        del actor
        return list(self.providers)

    def list_models(self, actor):
        del actor
        return list(self.models)


class EmbeddingIdentityValidatorTests(unittest.TestCase):
    def setUp(self):
        self.actor = types.SimpleNamespace(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.provider = types.SimpleNamespace(
            id="embedding-provider",
            status=ModelProviderStatus.ACTIVE,
            residency_tags=("eu",),
        )
        self.model = types.SimpleNamespace(
            id="embedding-model",
            provider_id="embedding-provider",
            lifecycle=ModelLifecycle.ACTIVE,
            capabilities=("embedding",),
            embedding_dimensions=384,
            model_version="2026-09",
            updated_at=1.0,
            residency_tags=("eu",),
        )
        self.validator = ModelGatewayEmbeddingIdentityValidator(
            _Gateway([self.provider], [self.model])
        )

    def identity(self, **updates):
        values = {
            "provider_id": "embedding-provider",
            "model_id": "embedding-model",
            "model_revision": "2026-09",
            "dimensions": 384,
            "capability_revision": 1,
            "local": False,
            "residency_tags": ("eu",),
        }
        values.update(updates)
        return EmbeddingModelIdentity(**values)

    def test_external_embedding_identity_must_match_canonical_model_registry(self):
        self.validator(self.identity(), self.actor)

        with self.assertRaisesRegex(
            EmbeddingIdentityValidationError,
            "dimensions",
        ):
            self.validator(self.identity(dimensions=768), self.actor)

        with self.assertRaisesRegex(
            EmbeddingIdentityValidationError,
            "revision",
        ):
            self.validator(
                self.identity(model_revision="old"),
                self.actor,
            )

    def test_embedding_capability_and_residency_are_fail_closed(self):
        model = types.SimpleNamespace(**self.model.__dict__)
        model.capabilities = ("text",)
        validator = ModelGatewayEmbeddingIdentityValidator(
            _Gateway([self.provider], [model])
        )
        with self.assertRaisesRegex(
            EmbeddingIdentityValidationError,
            "embedding capability",
        ):
            validator(self.identity(), self.actor)

        with self.assertRaisesRegex(
            EmbeddingIdentityValidationError,
            "residency",
        ):
            self.validator(
                self.identity(residency_tags=("us",)),
                self.actor,
            )

    def test_local_embedding_identity_does_not_require_registry_entry(self):
        empty = ModelGatewayEmbeddingIdentityValidator(
            _Gateway([], [])
        )
        empty(
            EmbeddingModelIdentity(
                provider_id="local",
                model_id="concept-hash",
                model_revision="1",
                dimensions=128,
                local=True,
            ),
            self.actor,
        )


if __name__ == "__main__":
    unittest.main()
