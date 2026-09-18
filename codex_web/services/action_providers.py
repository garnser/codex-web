from __future__ import annotations

import time
from typing import Any, Mapping

from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT,
    ActionDefinition,
    ActionPreparation,
    ActionProvider,
    ActionProviderBinding,
    ActionProviderBindingCreate,
    ActionRequest,
    ActionResult,
    ActionVerification,
    UnsupportedActionCapabilityError,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.resources import ResourceType
from codex_web.services.action_provider_conformance import ActionProviderConformanceSuite
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.services.resources import ResourceCatalogService, ResourceNotFoundError
from codex_web.services.secrets import SecretBroker
from codex_web.storage.action_providers import ActionProviderStateStore


class ActionProviderError(RuntimeError):
    pass


class ActionProviderNotFoundError(ActionProviderError):
    pass


class ActionBindingNotFoundError(ActionProviderError):
    pass


class ActionResolutionError(ActionProviderError):
    pass


class ActionRequirementError(ActionProviderError):
    pass


class ActionProviderRegistry:
    def __init__(self, store: ActionProviderStateStore) -> None:
        self.store = store
        self.providers: dict[tuple[str, str], ActionProvider] = {}
        self.conformance = ActionProviderConformanceSuite()

    def register(self, provider: ActionProvider) -> None:
        self.conformance.validate_contract(provider)
        key = (provider.provider_type, provider.provider_instance)
        self.providers[key] = provider

    def provider(self, provider_type: str, provider_instance: str) -> ActionProvider:
        provider = self.providers.get((provider_type, provider_instance))
        if provider is None:
            raise ActionProviderNotFoundError(
                f"action provider {provider_type}/{provider_instance} is not registered"
            )
        ACTION_PROVIDER_CONTRACT.require(provider.contract_version)
        return provider

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "action-provider:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    def bind(
        self,
        payload: ActionProviderBindingCreate,
        *,
        actor: AuthenticationActor,
        resources: ResourceCatalogService,
    ) -> ActionProviderBinding:
        if not self._admin(actor):
            raise AuthorizationError("action provider administration authority required")
        self.provider(payload.provider_type, payload.provider_instance)
        for resource_id in payload.resource_ids:
            resources.get(resource_id, actor)
        binding = ActionProviderBinding(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            **payload.model_dump(),
        )

        def apply(state):
            state.bindings.append(binding)
            return state

        self.store.update(apply)
        return binding

    def list_bindings(self, actor: AuthenticationActor) -> list[ActionProviderBinding]:
        return [
            item
            for item in self.store.load().bindings
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]

    def binding(self, binding_id: str, actor: AuthenticationActor) -> ActionProviderBinding:
        binding = next(
            (
                item
                for item in self.store.load().bindings
                if item.id == binding_id
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ),
            None,
        )
        if binding is None:
            raise ActionBindingNotFoundError("action provider binding not found")
        if not binding.enabled:
            raise ActionResolutionError("action provider binding is disabled")
        return binding

    def catalog(self, actor: AuthenticationActor) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for binding in self.list_bindings(actor):
            try:
                provider = self.provider(binding.provider_type, binding.provider_instance)
                actions = [item.model_dump(mode="json") for item in provider.actions()]
                status = "available" if binding.enabled else "disabled"
            except ActionProviderError:
                actions = []
                status = "unavailable"
            items.append(
                {
                    "binding": binding.model_dump(mode="json"),
                    "status": status,
                    "actions": actions,
                }
            )
        return items


class ActionExecutionService:
    """Single provider-neutral boundary for external operational actions."""

    def __init__(
        self,
        registry: ActionProviderRegistry,
        resources: ResourceCatalogService,
        *,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.registry = registry
        self.resources = resources
        self.secret_broker = secret_broker

    @staticmethod
    def _definition(provider: ActionProvider, action_id: str) -> ActionDefinition:
        definition = next((item for item in provider.actions() if item.action_id == action_id), None)
        if definition is None:
            raise ActionResolutionError(f"action {action_id!r} is not supported by provider")
        return definition

    def _resolve(
        self,
        binding_id: str,
        request: ActionRequest,
        actor: AuthenticationActor,
    ) -> tuple[ActionProviderBinding, ActionProvider, ActionDefinition, ActionRequest]:
        if (
            request.organization_id != actor.organization_id
            or request.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant action request denied")
        binding = self.registry.binding(binding_id, actor)
        provider = self.registry.provider(binding.provider_type, binding.provider_instance)
        definition = self._definition(provider, request.action_id)

        if binding.project_id is not None and request.project_id != binding.project_id:
            raise ActionResolutionError("action request project does not match provider binding")
        if binding.resource_ids and not request.resource_ids:
            raise ActionResolutionError("resource-scoped provider binding requires explicit action targets")
        if binding.resource_ids and not set(request.resource_ids).issubset(set(binding.resource_ids)):
            raise ActionResolutionError("action request targets resources outside provider binding")

        resolved_resources = [self.resources.get(resource_id, actor) for resource_id in request.resource_ids]
        if definition.required_resource_types:
            observed = {item.resource_type for item in resolved_resources}
            missing = set(definition.required_resource_types) - observed
            if missing:
                values = ", ".join(sorted(item.value for item in missing))
                raise ActionRequirementError(f"action requires resource types: {values}")

        if (
            binding.credential_ref
            and request.credential_ref
            and request.credential_ref != binding.credential_ref
        ):
            raise ActionResolutionError("action request cannot override provider binding credential")
        credential_ref = binding.credential_ref or request.credential_ref
        if definition.credential_required and not credential_ref:
            raise ActionRequirementError("action requires credential reference")
        if request.dry_run:
            definition.capabilities.require("dry_run")
        if request.idempotency_key:
            definition.capabilities.require("idempotency")

        if credential_ref and self.secret_broker is not None:
            self.secret_broker.metadata(
                credential_ref,
                actor=actor,
                require_use=True,
            )
        elif credential_ref and self.secret_broker is None:
            raise ActionRequirementError("credential broker is unavailable")

        if credential_ref != request.credential_ref:
            request = request.model_copy(update={"credential_ref": credential_ref})
        return binding, provider, definition, request

    def resolve_contract(
        self,
        binding_id: str,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> tuple[ActionProviderBinding, ActionProvider, ActionDefinition, ActionRequest]:
        """Validate and normalize an action without performing a side effect."""

        return self._resolve(binding_id, request, actor)

    async def prepare(
        self,
        binding_id: str,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionPreparation:
        binding, provider, definition, request = self._resolve(binding_id, request, actor)
        definition.capabilities.require("prepare")
        plan = await provider.prepare(request, binding=binding)
        return ActionPreparation(
            provider_binding_id=binding.id,
            action=definition,
            request=request,
            provider_plan=plan,
        )

    async def execute(
        self,
        binding_id: str,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionResult:
        binding, provider, definition, request = self._resolve(binding_id, request, actor)
        definition.capabilities.require("execute")

        async def invoke(credential: str | None) -> ActionResult:
            return await provider.execute(
                request,
                binding=binding,
                credential=credential,
            )

        if request.credential_ref:
            assert self.secret_broker is not None
            return await self.secret_broker.use_async(
                request.credential_ref,
                actor=actor,
                operation=f"action-provider:{binding.provider_type}:{request.action_id}",
                consumer=invoke,
                context={
                    "binding_id": binding.id,
                    "project_id": request.project_id,
                    "action_id": request.action_id,
                },
            )
        return await invoke(None)

    async def verify(
        self,
        binding_id: str,
        result: ActionResult,
        *,
        actor: AuthenticationActor,
    ) -> ActionVerification:
        binding = self.registry.binding(binding_id, actor)
        provider = self.registry.provider(binding.provider_type, binding.provider_instance)
        definition = self._definition(provider, result.action_id)
        definition.capabilities.require("verification")
        return await provider.verify(result, binding=binding)

    async def rollback(
        self,
        binding_id: str,
        result: ActionResult,
        *,
        actor: AuthenticationActor,
    ) -> ActionResult:
        binding = self.registry.binding(binding_id, actor)
        provider = self.registry.provider(binding.provider_type, binding.provider_instance)
        definition = self._definition(provider, result.action_id)
        definition.capabilities.require("rollback")
        if not definition.reversible:
            raise ActionRequirementError("action is not declared reversible")

        async def invoke(credential: str | None) -> ActionResult:
            return await provider.rollback(
                result,
                binding=binding,
                credential=credential,
            )

        credential_ref = binding.credential_ref
        if credential_ref:
            if self.secret_broker is None:
                raise ActionRequirementError("credential broker is unavailable")
            return await self.secret_broker.use_async(
                credential_ref,
                actor=actor,
                operation=f"action-provider:{binding.provider_type}:{result.action_id}:rollback",
                consumer=invoke,
                context={"binding_id": binding.id, "action_id": result.action_id},
            )
        return await invoke(None)
