from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from codex_web.code_hosts import (
    CodeHostCapability,
    CodeHostCheckFact,
    CodeHostCommitFact,
    CodeHostCompareFact,
    CodeHostError,
    CodeHostProvider,
    CodeHostProviderBinding,
    CodeHostPullRequestFact,
    CodeHostRefFact,
    CodeHostReleaseFact,
    CodeHostReviewFact,
    CodeHostRepositoryFact,
    CodeHostUnsupportedCapabilityError,
    CodeHostWebhookFact,
)
from codex_web.identity import AuthenticationActor
from codex_web.resources import Resource, ResourceType
from codex_web.services.canonical_events import (
    CanonicalEventDelivery,
    CanonicalEventIngestionService,
)
from codex_web.services.identity import TenantIsolationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker


T = TypeVar("T")


class CodeHostRegistry:
    """Runtime registry for provider adapters and tenant-scoped provider bindings."""

    def __init__(self) -> None:
        self._providers: dict[str, CodeHostProvider] = {}
        self._bindings: dict[str, CodeHostProviderBinding] = {}

    def register_provider(self, provider: CodeHostProvider) -> None:
        existing = self._providers.get(provider.provider_type)
        if existing is not None and existing is not provider:
            raise CodeHostError(
                f"code-host provider already registered: {provider.provider_type}"
            )
        self._providers[provider.provider_type] = provider

    def register_binding(self, binding: CodeHostProviderBinding) -> None:
        provider = self._providers.get(binding.provider_type)
        if provider is None:
            raise CodeHostError(
                f"code-host provider unavailable: {binding.provider_type}"
            )
        unsupported = set(binding.capabilities) - set(provider.capabilities())
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise CodeHostUnsupportedCapabilityError(
                f"binding declares unsupported capabilities: {names}"
            )
        self._bindings[binding.id] = binding

    def resolve(
        self,
        binding_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostProviderBinding, CodeHostProvider]:
        binding = self._bindings.get(binding_id)
        if binding is None:
            raise CodeHostError("code-host binding not found")
        if (
            binding.organization_id != actor.organization_id
            or binding.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant code-host binding access denied")
        provider = self._providers.get(binding.provider_type)
        if provider is None:
            raise CodeHostError(
                f"code-host provider unavailable: {binding.provider_type}"
            )
        return binding, provider


class CodeHostService:
    """Read-only SCM facade. External mutations intentionally live elsewhere."""

    def __init__(
        self,
        registry: CodeHostRegistry,
        resources: ResourceCatalogService,
        *,
        secrets: SecretBroker | None = None,
        canonical_events: CanonicalEventIngestionService | None = None,
    ) -> None:
        self.registry = registry
        self.resources = resources
        self.secrets = secrets
        self.canonical_events = canonical_events

    @staticmethod
    def _require_capability(
        binding: CodeHostProviderBinding,
        provider: CodeHostProvider,
        capability: CodeHostCapability,
    ) -> None:
        if capability not in binding.capabilities or capability not in provider.capabilities():
            raise CodeHostUnsupportedCapabilityError(
                f"code-host capability unavailable: {capability.value}"
            )

    def _resource(
        self,
        resource_id: str,
        *,
        actor: AuthenticationActor,
        binding: CodeHostProviderBinding,
    ) -> Resource:
        resource = self.resources.get(resource_id, actor)
        if resource.resource_type != ResourceType.REPOSITORY:
            raise CodeHostError("code-host operations require a repository Resource")
        provenance = resource.provenance
        if provenance and provenance.provider:
            if provenance.provider.casefold() != binding.provider_type.casefold():
                raise CodeHostError(
                    "repository Resource provider provenance does not match code-host binding"
                )
            if (
                provenance.provider_instance
                and provenance.provider_instance != binding.provider_instance
            ):
                raise CodeHostError(
                    "repository Resource provider instance does not match code-host binding"
                )
        return resource

    async def _with_credential(
        self,
        binding: CodeHostProviderBinding,
        *,
        actor: AuthenticationActor,
        resource: Resource,
        operation: str,
        callback: Callable[[str | None], Awaitable[T]],
    ) -> T:
        if not binding.credential_ref:
            return await callback(None)
        if self.secrets is None:
            raise CodeHostError("secret broker is required for code-host credentials")
        return await self.secrets.use_async(
            binding.credential_ref,
            actor=actor,
            operation=operation,
            consumer=lambda value: callback(value),
            context={
                "binding_id": binding.id,
                "provider_type": binding.provider_type,
                "provider_instance": binding.provider_instance,
                "resource_id": resource.id,
            },
        )

    async def repository(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostRepositoryFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.REPOSITORY_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.repository.read",
            callback=lambda credential: provider.repository(
                binding,
                resource,
                credential=credential,
            ),
        )

    async def refs(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostRefFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.REFS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.refs.read",
            callback=lambda credential: provider.refs(
                binding,
                resource,
                credential=credential,
            ),
        )

    async def commit(
        self,
        binding_id: str,
        resource_id: str,
        revision: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostCommitFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.COMMITS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.commit.read",
            callback=lambda credential: provider.commit(
                binding,
                resource,
                revision,
                credential=credential,
            ),
        )

    async def pull_request(
        self,
        binding_id: str,
        resource_id: str,
        external_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostPullRequestFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.PULL_REQUEST_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.pull-request.read",
            callback=lambda credential: provider.pull_request(
                binding,
                resource,
                external_id,
                credential=credential,
            ),
        )

    async def reviews(
        self,
        binding_id: str,
        resource_id: str,
        external_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostReviewFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.REVIEW_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.reviews.read",
            callback=lambda credential: provider.reviews(
                binding,
                resource,
                external_id,
                credential=credential,
            ),
        )

    async def checks(
        self,
        binding_id: str,
        resource_id: str,
        revision: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostCheckFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.CHECKS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.checks.read",
            callback=lambda credential: provider.checks(
                binding,
                resource,
                revision,
                credential=credential,
            ),
        )

    async def releases(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostReleaseFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.RELEASE_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.releases.read",
            callback=lambda credential: provider.releases(
                binding,
                resource,
                credential=credential,
            ),
        )

    async def compare(
        self,
        binding_id: str,
        resource_id: str,
        base_revision: str,
        head_revision: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostCompareFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.COMPARE_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.compare.read",
            callback=lambda credential: provider.compare(
                binding,
                resource,
                base_revision,
                head_revision,
                credential=credential,
            ),
        )

    async def ingest_webhook(
        self,
        binding_id: str,
        *,
        actor: AuthenticationActor,
        event_kind: str,
        event_id: str,
        payload: dict,
    ) -> tuple[CodeHostWebhookFact, CanonicalEventDelivery | None]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.WEBHOOK_NORMALIZE)
        fact = provider.normalize_webhook(
            binding,
            payload,
            event_kind=event_kind,
            event_id=event_id,
        )
        if self.canonical_events is None:
            return fact, None
        delivery = await self.canonical_events.ingest(
            event_type=fact.canonical_event_type,
            source=f"code-host:{binding.provider_type}:{binding.provider_instance}",
            idempotency_key=f"{binding.provider_instance}:{fact.event_id}",
            payload={
                "provider_type": fact.provider_type,
                "provider_instance": fact.provider_instance,
                "event_kind": fact.event_kind,
                "repository_external_id": fact.repository_external_id,
                "subject_external_id": fact.subject_external_id,
                "action": fact.action,
                "state": fact.state,
                "web_url": fact.web_url,
                "provider_payload": fact.provider_payload,
            },
            occurred_at=fact.occurred_at,
            tenant_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        return fact, delivery
