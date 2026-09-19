from __future__ import annotations

import time

from codex_web.agent_providers import (
    AgentProviderCapability,
    AgentProviderCompatibility,
    AgentProviderDiscovery,
    AgentProviderHealth,
    AgentProviderLifecycle,
    AgentProviderRecord,
    AgentProviderUpsert,
)
from codex_web.extensions import (
    ExtensionHealthStatus,
    ExtensionLifecycleState,
    ExtensionType,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.model_gateway import ModelProviderStatus
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.model_gateway import ModelGatewayStore


class AgentProviderError(RuntimeError):
    pass


class AgentProviderConflictError(AgentProviderError):
    pass


class AgentProviderService:
    """Canonical identity and capability view above model and execution adapters.

    Capability declaration is descriptive. Only the intersection of declared
    and granted capabilities can become effective, and lifecycle/compatibility
    dependencies can reduce that set to empty. Authority/policy enforcement for
    actual actions remains in its owning canonical boundaries.
    """

    def __init__(
        self,
        store: AgentProviderStore,
        *,
        model_gateway: ModelGatewayStore | None = None,
        extensions: ExtensionStateStore | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.model_gateway = model_gateway
        self.extensions = extensions
        self.clock = clock

    @staticmethod
    def _same_scope(item, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "agent-providers:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "agent-providers:admin service scope required"
                )
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )

    def _model_providers(self, actor: AuthenticationActor):
        if self.model_gateway is None:
            return []
        return [
            item
            for item in self.model_gateway.load().providers
            if self._same_scope(item, actor)
        ]

    def _synthesized_model_records(
        self,
        actor: AuthenticationActor,
        persisted: list[AgentProviderRecord],
    ) -> list[AgentProviderRecord]:
        known_ids = {item.id for item in persisted}
        known_model_ids = {
            model_id
            for item in persisted
            for model_id in item.model_provider_ids
        }
        synthesized: list[AgentProviderRecord] = []
        now = float(self.clock())
        for provider in self._model_providers(actor):
            if provider.id in known_ids or provider.id in known_model_ids:
                continue
            lifecycle = (
                AgentProviderLifecycle.ACTIVE
                if provider.status != ModelProviderStatus.DISABLED
                else AgentProviderLifecycle.DISABLED
            )
            health = {
                ModelProviderStatus.ACTIVE: AgentProviderHealth.HEALTHY,
                ModelProviderStatus.DEGRADED: AgentProviderHealth.DEGRADED,
                ModelProviderStatus.DISABLED: AgentProviderHealth.UNAVAILABLE,
            }[provider.status]
            synthesized.append(
                AgentProviderRecord(
                    id=provider.id,
                    display_name=provider.display_name,
                    organization_id=provider.organization_id,
                    workspace_id=provider.workspace_id,
                    declared_capabilities=(
                        AgentProviderCapability.MODEL_INFERENCE,
                    ),
                    granted_capabilities=(
                        AgentProviderCapability.MODEL_INFERENCE,
                    ),
                    model_provider_ids=(provider.id,),
                    credential_refs=(
                        (provider.credential_ref,)
                        if provider.credential_ref
                        else ()
                    ),
                    residency_tags=provider.residency_tags,
                    compliance_tags=provider.compliance_tags,
                    lifecycle=lifecycle,
                    health=health,
                    compatibility=AgentProviderCompatibility.COMPATIBLE,
                    synthesized_from_model_gateway=True,
                    created_at=provider.created_at,
                    created_by=provider.updated_by,
                    updated_at=provider.updated_at,
                    updated_by=provider.updated_by,
                )
            )
        return synthesized

    def list(self, actor: AuthenticationActor) -> list[AgentProviderRecord]:
        persisted = [
            item for item in self.store.list() if self._same_scope(item, actor)
        ]
        return sorted(
            [
                *persisted,
                *self._synthesized_model_records(actor, persisted),
            ],
            key=lambda item: item.id,
        )

    def get(self, provider_id: str, actor: AuthenticationActor) -> AgentProviderRecord:
        item = next(
            (provider for provider in self.list(actor) if provider.id == provider_id),
            None,
        )
        if item is None:
            raise AgentProviderError("agent provider not found")
        return item

    def _extension_installation(
        self,
        installation_id: str,
        actor: AuthenticationActor,
    ):
        if self.extensions is None:
            raise AgentProviderConflictError(
                "extension registry is unavailable"
            )
        installation = next(
            (
                item
                for item in self.extensions.load().installations
                if item.id == installation_id and self._same_scope(item, actor)
            ),
            None,
        )
        if installation is None:
            raise AgentProviderConflictError(
                "extension installation not found in tenant scope"
            )
        return installation

    def _validate_model_links(
        self,
        model_provider_ids: tuple[str, ...],
        actor: AuthenticationActor,
    ) -> None:
        if not model_provider_ids:
            return
        available = {item.id for item in self._model_providers(actor)}
        missing = sorted(set(model_provider_ids) - available)
        if missing:
            raise AgentProviderConflictError(
                f"model provider is not available in tenant scope: {missing[0]}"
            )

    def upsert(
        self,
        payload: AgentProviderUpsert,
        *,
        actor: AuthenticationActor,
    ) -> AgentProviderRecord:
        self._require_admin(actor)
        self._validate_model_links(payload.model_provider_ids, actor)
        now = float(self.clock())
        extension_id = None
        extension_version = None
        extension_digest = None
        if payload.extension_installation_id:
            installation = self._extension_installation(
                payload.extension_installation_id,
                actor,
            )
            extension_id = installation.extension_id
            extension_version = installation.version
            extension_digest = installation.manifest.provenance.digest
            types = set(installation.manifest.types)
            if (
                AgentProviderCapability.MODEL_INFERENCE
                in payload.declared_capabilities
                and ExtensionType.MODEL_PROVIDER not in types
            ):
                raise AgentProviderConflictError(
                    "model-inference provider extension must declare model_provider type"
                )
            if (
                AgentProviderCapability.AGENT_EXECUTION
                in payload.declared_capabilities
                and ExtensionType.WORKER not in types
            ):
                raise AgentProviderConflictError(
                    "execution-agent provider extension must declare worker type"
                )

        existing = next(
            (
                item
                for item in self.store.list()
                if item.id == payload.id and self._same_scope(item, actor)
            ),
            None,
        )
        record = AgentProviderRecord(
            **payload.model_dump(mode="python"),
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            extension_id=extension_id,
            extension_version=extension_version,
            extension_digest=extension_digest,
            created_at=existing.created_at if existing else now,
            created_by=existing.created_by if existing else actor.identity_id,
            updated_at=now,
            updated_by=actor.identity_id,
            revision=existing.revision if existing else 1,
        )
        return self.store.upsert(record)

    def _extension_reasons(
        self,
        provider: AgentProviderRecord,
        actor: AuthenticationActor,
    ) -> list[str]:
        installation_id = provider.extension_installation_id
        if not installation_id:
            return []
        if self.extensions is None:
            return ["extension registry unavailable"]
        installation = next(
            (
                item
                for item in self.extensions.load().installations
                if item.id == installation_id and self._same_scope(item, actor)
            ),
            None,
        )
        if installation is None:
            return ["extension installation missing"]
        reasons: list[str] = []
        if installation.lifecycle != ExtensionLifecycleState.ENABLED:
            reasons.append(
                f"extension lifecycle is {installation.lifecycle.value}"
            )
        if installation.health_status in {
            ExtensionHealthStatus.UNHEALTHY,
        }:
            reasons.append(
                f"extension health is {installation.health_status.value}"
            )
        return reasons

    def _model_link_reasons(
        self,
        provider: AgentProviderRecord,
        actor: AuthenticationActor,
    ) -> list[str]:
        if not provider.model_provider_ids:
            return []
        models = {item.id: item for item in self._model_providers(actor)}
        linked = [
            models.get(item_id)
            for item_id in provider.model_provider_ids
        ]
        if any(item is None for item in linked):
            return ["linked model provider missing"]
        if all(
            item is not None and item.status == ModelProviderStatus.DISABLED
            for item in linked
        ):
            return ["all linked model providers are disabled"]
        return []

    def discover(
        self,
        actor: AuthenticationActor,
        *,
        required_capabilities: tuple[AgentProviderCapability, ...] = (),
    ) -> list[AgentProviderDiscovery]:
        required = set(required_capabilities)
        results: list[AgentProviderDiscovery] = []
        for provider in self.list(actor):
            reasons: list[str] = []
            if provider.lifecycle != AgentProviderLifecycle.ACTIVE:
                reasons.append(f"provider lifecycle is {provider.lifecycle.value}")
            if provider.compatibility != AgentProviderCompatibility.COMPATIBLE:
                reasons.append("provider is incompatible")
            if provider.health == AgentProviderHealth.UNAVAILABLE:
                reasons.append("provider is unavailable")
            reasons.extend(self._extension_reasons(provider, actor))
            reasons.extend(self._model_link_reasons(provider, actor))

            effective = tuple(
                capability
                for capability in provider.declared_capabilities
                if capability in set(provider.granted_capabilities)
            )
            availability_blocked = bool(reasons)
            if availability_blocked:
                effective = ()
            missing = required - set(effective)
            if missing:
                reasons.append(
                    "missing required capabilities: "
                    + ", ".join(sorted(item.value for item in missing))
                )
            results.append(
                AgentProviderDiscovery(
                    provider=provider,
                    effective_capabilities=effective,
                    eligible=not reasons,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )
        return results
