from __future__ import annotations

from codex_web.identity import AuthenticationActor
from codex_web.model_gateway import ModelLifecycle, ModelProviderStatus
from codex_web.retrieval import EmbeddingModelIdentity
from codex_web.services.model_gateway import ModelGatewayService


class EmbeddingIdentityValidationError(RuntimeError):
    pass


class ModelGatewayEmbeddingIdentityValidator:
    """Bind non-local embedding identities to the canonical ModelGateway registry."""

    def __init__(self, gateway: ModelGatewayService) -> None:
        self.gateway = gateway

    def __call__(
        self,
        identity: EmbeddingModelIdentity,
        actor: AuthenticationActor,
    ) -> None:
        if identity.local:
            return

        provider = next(
            (
                item
                for item in self.gateway.list_providers(actor)
                if item.id == identity.provider_id
            ),
            None,
        )
        if provider is None:
            raise EmbeddingIdentityValidationError(
                f"embedding provider is not registered: {identity.provider_id}"
            )
        if provider.status != ModelProviderStatus.ACTIVE:
            raise EmbeddingIdentityValidationError(
                f"embedding provider is not active: {identity.provider_id}"
            )

        model = next(
            (
                item
                for item in self.gateway.list_models(actor)
                if item.id == identity.model_id
            ),
            None,
        )
        if model is None:
            raise EmbeddingIdentityValidationError(
                f"embedding model is not registered: {identity.model_id}"
            )
        if model.provider_id != provider.id:
            raise EmbeddingIdentityValidationError(
                "embedding model/provider identity mismatch"
            )
        if model.lifecycle != ModelLifecycle.ACTIVE:
            raise EmbeddingIdentityValidationError(
                f"embedding model is not active: {identity.model_id}"
            )
        if "embedding" not in model.capabilities:
            raise EmbeddingIdentityValidationError(
                f"model does not declare embedding capability: {identity.model_id}"
            )
        if model.embedding_dimensions is None:
            raise EmbeddingIdentityValidationError(
                f"embedding model dimensions are not declared: {identity.model_id}"
            )
        if model.embedding_dimensions != identity.dimensions:
            raise EmbeddingIdentityValidationError(
                "embedding dimensions do not match canonical model definition"
            )
        expected_revision = model.model_version or f"record:{model.updated_at}"
        if identity.model_revision != expected_revision:
            raise EmbeddingIdentityValidationError(
                "embedding model revision does not match canonical model definition"
            )

        configured_residency = set(provider.residency_tags) | set(model.residency_tags)
        if (
            identity.residency_tags
            and not set(identity.residency_tags).issubset(configured_residency)
        ):
            raise EmbeddingIdentityValidationError(
                "embedding provider residency is not declared by canonical model configuration"
            )
