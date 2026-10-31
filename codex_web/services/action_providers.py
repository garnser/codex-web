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
from codex_web.identity import (
    AuthenticationActor,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.resources import ResourceType
from codex_web.services.action_provider_conformance import ActionProviderConformanceSuite
from codex_web.services.code_host_action_contract import (
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID,
    READINESS_OBSERVATION_ACTION_IDS,
    CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID, CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_ACTION_ID,
)
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


class SensitiveActionOutputError(RuntimeError):
    """A possible effect occurred, but its response cannot cross the credential boundary."""


def _credential_safe_output(result, credential):
    if credential:
        def contains(value):
            if isinstance(value, str):
                return credential in value
            if isinstance(value, Mapping):
                return any(contains(key) or contains(item) for key, item in value.items())
            if isinstance(value, (tuple, list)):
                return any(contains(item) for item in value)
            return False
        if contains(result.model_dump(mode="json")):
            # Never put the value or rejected output in exceptions/receipts.
            raise SensitiveActionOutputError("provider returned protected credential material; reconcile the effect")
    return result


class ActionProviderRegistry:
    def __init__(self, store: ActionProviderStateStore) -> None:
        self.store = store
        self.providers: dict[tuple[str, str], ActionProvider] = {}
        self.tenant_providers: dict[
            tuple[str, str, str, str],
            ActionProvider,
        ] = {}
        self.conformance = ActionProviderConformanceSuite()
        self._procedure_result_adapters: dict[int, dict[str, Any]] = {}

    @staticmethod
    def _provider_key(
        provider_type: str,
        provider_instance: str,
    ) -> tuple[str, str]:
        return (
            str(provider_type or "").strip(),
            str(provider_instance or "").strip(),
        )

    def register(
        self,
        provider: ActionProvider,
        *,
        tenant_scope: TenantScope | None = None,
    ) -> None:
        self.conformance.validate_contract(provider)
        provider_type, provider_instance = self._provider_key(
            provider.provider_type,
            provider.provider_instance,
        )
        if not provider_type or not provider_instance:
            raise ValueError("action provider type and instance are required")
        adapters = {}
        actions = {action.action_id for action in provider.actions()}
        for adapter in getattr(provider, "procedure_result_adapters", ()):
            if (adapter.provider_type != provider_type
                or adapter.contract_version != provider.contract_version
                or not callable(adapter.project)):
                raise ValueError("procedure result adapter provider contract mismatch")
            for action_id in adapter.action_ids:
                if action_id not in actions or action_id in adapters:
                    raise ValueError("procedure result adapter action mismatch")
                adapters[action_id] = adapter
        self._procedure_result_adapters[id(provider)] = adapters
        if tenant_scope is None:
            self.providers[(provider_type, provider_instance)] = provider
            return
        self.tenant_providers[
            (
                tenant_scope.organization_id,
                tenant_scope.workspace_id,
                provider_type,
                provider_instance,
            )
        ] = provider

    def unregister_tenant(
        self,
        provider_type: str,
        provider_instance: str,
        *,
        tenant_scope: TenantScope,
    ) -> None:
        provider_type, provider_instance = self._provider_key(
            provider_type,
            provider_instance,
        )
        self.tenant_providers.pop(
            (
                tenant_scope.organization_id,
                tenant_scope.workspace_id,
                provider_type,
                provider_instance,
            ),
            None,
        )

    def provider(
        self,
        provider_type: str,
        provider_instance: str,
        *,
        actor: AuthenticationActor | None = None,
    ) -> ActionProvider:
        return self.provider_configuration(provider_type, provider_instance,
            scope=actor.tenant if actor is not None else None)

    def provider_configuration(self, provider_type: str, provider_instance: str, *,
                               scope: TenantScope | None = None) -> ActionProvider:
        """Read a registered implementation for canonical routing configuration.

        This lookup grants no action authority and resolves no credential.
        Invocation still uses the existing actor/authority/provider boundary.
        """
        provider_type, provider_instance = self._provider_key(
            provider_type,
            provider_instance,
        )
        provider = None
        if scope is not None:
            provider = self.tenant_providers.get(
                (
                    scope.organization_id,
                    scope.workspace_id,
                    provider_type,
                    provider_instance,
                )
            )
        if provider is None:
            provider = self.providers.get((provider_type, provider_instance))
        if provider is None:
            raise ActionProviderNotFoundError(
                f"action provider {provider_type}/{provider_instance} is not registered"
            )
        ACTION_PROVIDER_CONTRACT.require(provider.contract_version)
        return provider

    def procedure_result_adapter(self, provider_type, provider_instance, action_id,
                                 contract_version, *, actor):
        # Resolve the current tenant registration, never another tenant's or a
        # superseded implementation's adapter. This is not execution authority.
        provider = self.provider(provider_type, provider_instance, actor=actor)
        if provider.contract_version != contract_version:
            raise ValueError("procedure result adapter contract changed")
        return self._procedure_result_adapters.get(id(provider), {}).get(action_id)

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
        self.provider(
            payload.provider_type,
            payload.provider_instance,
            actor=actor,
        )
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

    def set_enabled(
        self,
        binding_id: str,
        enabled: bool,
        *,
        actor: AuthenticationActor,
    ) -> ActionProviderBinding:
        if not self._admin(actor):
            raise AuthorizationError("action provider administration authority required")
        updated: list[ActionProviderBinding] = []

        def apply(state):
            for index, item in enumerate(state.bindings):
                if (
                    item.id == binding_id
                    and item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ):
                    replacement = item.model_copy(
                        update={"enabled": enabled, "updated_at": time.time()}
                    )
                    state.bindings[index] = replacement
                    updated.append(replacement)
                    return state
            raise ActionBindingNotFoundError("action provider binding not found")

        self.store.update(apply)
        return updated[0]

    def catalog(self, actor: AuthenticationActor) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for binding in self.list_bindings(actor):
            try:
                provider = self.provider(
                    binding.provider_type,
                    binding.provider_instance,
                    actor=actor,
                )
                actions = [item.model_dump(mode="json") for item in provider.actions()]
                status = (
                    "available"
                    if binding.enabled and actions
                    else ("disabled" if not binding.enabled else "unavailable")
                )
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
        self.procedure_admission = None
        self.observation_policy_resolver = None
        self.observation_deadline_resolver = None

    def can_observe_contract_reads(self, *, resources, provider_references=()) -> bool:
        """Classify stock metadata callbacks without invoking provider actions.

        This grants no authority and caches no binding or credential. When no
        actor is supplied, every tenant registration for each requested provider
        must be stock; unsupported registrations conservatively use legacy reads.
        """
        from codex_web.services.resources import _CANONICAL_RESOURCE_SERVICE_CHECK
        if (type(self) is not ActionExecutionService or self.resources is not resources
                or type(resources) is not ResourceCatalogService
                or not _CANONICAL_RESOURCE_SERVICE_CHECK(resources)
                or not _stock_bound(resources, 'require_read_observation', _CANONICAL_RESOURCE_REQUIRE)
                or not _stock_bound(resources, 'observe_reads', _CANONICAL_RESOURCE_OBSERVE)
                or any(not _stock_bound(self, name, method)
                       for name, method in _CANONICAL_EXECUTION_METADATA_READS.items())
                or self._definition is not _CANONICAL_ACTION_DEFINITION
                or not _stock_registry_metadata(self.registry)
                or not _stock_secret_metadata(self.secret_broker)):
            return False
        if type(provider_references) not in (tuple, list):
            return False
        for reference in provider_references:
            if (type(reference) not in (tuple, list) or len(reference) != 2
                    or any(type(part) is not str for part in reference)):
                return False
            key = _CANONICAL_PROVIDER_KEY(*reference)
            providers = []
            if key in self.registry.providers:
                providers.append(self.registry.providers[key])
            providers.extend(provider for tenant_key, provider in self.registry.tenant_providers.items()
                             if type(tenant_key) is tuple and len(tenant_key) == 4
                             and tenant_key[2:] == key)
            if not providers or any(not _stock_provider_metadata(provider, resources) for provider in providers):
                return False
        return True

    def _require_contract_read_observation(self, observation):
        if not _CANONICAL_CONTRACT_READ_CHECK(self, resources=self.resources):
            raise ActionRequirementError("canonical action metadata reader is unavailable")
        _CANONICAL_RESOURCE_REQUIRE(self.resources, observation)

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
        *,
        _resources=None,
    ) -> tuple[ActionProviderBinding, ActionProvider, ActionDefinition, ActionRequest]:
        if (
            request.organization_id != actor.organization_id
            or request.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant action request denied")
        if _resources is not None:
            _CANONICAL_REQUIRE_CONTRACT_READ(self, _resources)
        binding = self.registry.binding(binding_id, actor)
        provider = self.registry.provider(
            binding.provider_type,
            binding.provider_instance,
            actor=actor,
        )
        if _resources is not None and not _stock_provider_metadata(provider, self.resources):
            raise ActionRequirementError("canonical provider action metadata is unavailable")
        definition = self._definition(provider, request.action_id)

        if binding.project_id is not None and request.project_id != binding.project_id:
            raise ActionResolutionError("action request project does not match provider binding")
        if binding.resource_ids and not request.resource_ids:
            raise ActionResolutionError("resource-scoped provider binding requires explicit action targets")
        if binding.resource_ids and not set(request.resource_ids).issubset(set(binding.resource_ids)):
            raise ActionResolutionError("action request targets resources outside provider binding")

        if _resources is not None:
            self.resources.require_read_observation(_resources)
        resource_kwargs = ({'_read_observation': _resources} if _resources is not None else {})
        resolved_resources = [self.resources.get(resource_id, actor, **resource_kwargs)
                              for resource_id in request.resource_ids]
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
        _resources=None,
    ) -> tuple[ActionProviderBinding, ActionProvider, ActionDefinition, ActionRequest]:
        """Validate and normalize an action without performing a side effect."""

        if _resources is not None:
            _CANONICAL_REQUIRE_CONTRACT_READ(self, _resources)
        return self._resolve(binding_id, request, actor,
                             **({'_resources': _resources} if _resources is not None else {}))

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

    def _observation_context(self, binding, provider, definition, value, actor, phase):
        from codex_web.services.code_host_readiness import ReadyExecution
        from codex_web.services.code_host_continuation import request_identity
        if self.procedure_admission is None:
            raise ActionRequirementError('Readiness observation requires canonical ActionIntent admission')
        resolver = self.observation_policy_resolver
        policy = resolver(binding, provider, definition, value, actor, phase) if resolver else None
        deadline = None
        deadline_clock = None
        if value.action_id == CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID:
            if self.observation_deadline_resolver is None or policy is None:
                raise ActionRequirementError('Guarded observation requires canonical v2 deadline admission')
            deadline, deadline_clock = self.observation_deadline_resolver(binding, provider, definition, value, actor, phase)
        from codex_web.services.secrets import current_async_use_reference
        used_reference = current_async_use_reference()
        reference_id = value.credential_ref if isinstance(value, ActionRequest) else binding.credential_ref
        if (used_reference is None or used_reference.id != reference_id
                or used_reference.status().value != 'active'):
            raise ActionRequirementError('Readiness observation requires the active canonical credential borrow')
        ids = value.resource_ids if isinstance(value, ActionRequest) else (value.output.get('resource_id'),)
        resources = tuple(self.resources.get(identity, actor) for identity in ids)
        provider_pin = tuple(getattr(provider, key, None) for key in
            ('provider_type','provider_instance','contract_version','api_base','web_base'))
        def current():
            if deadline is not None and deadline_clock() >= deadline:
                raise ActionRequirementError('Guarded observation deadline changed or expired')
            if policy is not None and resolver(binding, provider, definition, value, actor, phase,
                    pinned=policy) != policy:
                raise ActionRequirementError('Readiness observation policy changed')
            observed = self.registry.binding(binding.id, actor)
            active = self.registry.provider(observed.provider_type, observed.provider_instance, actor=actor)
            if (observed != binding or active is not provider
                or self._definition(active, definition.action_id) != definition
                or tuple(getattr(active,key,None) for key in
                    ('provider_type','provider_instance','contract_version','api_base','web_base')) != provider_pin
                or tuple(self.resources.get(identity, actor) for identity in ids) != resources):
                raise ActionRequirementError('Readiness observation current provider or repository changed')
            reference = value.credential_ref if isinstance(value, ActionRequest) else observed.credential_ref
            if reference:
                current_reference = self.secret_broker.metadata(reference, actor=actor, require_use=True)
                if current_reference.status().value != 'active' or current_reference != used_reference:
                    raise ActionRequirementError('Readiness observation credential lifecycle or version changed')
        async def checkpoint():
            try:
                current()
                admitted = await self.procedure_admission(binding, provider, definition, value, actor, phase)
                if deadline is not None and admitted != deadline:
                    raise ActionRequirementError('Guarded observation deadline changed')
                current()
            except ActionRequirementError:
                raise
            except Exception as error:
                raise ActionRequirementError('Readiness observation current canonical admission unavailable') from error
        return ReadyExecution(binding.id, request_identity(value) if isinstance(value, ActionRequest) else '', checkpoint,
            observation_policy=policy, guarded_deadline_at=deadline,
            deadline_clock=deadline_clock)

    async def execute(
        self,
        binding_id: str,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionResult:
        binding, provider, definition, request = self._resolve(binding_id, request, actor)
        definition.capabilities.require("execute")
        if self.procedure_admission is not None:
            await self.procedure_admission(binding, provider, definition, request, actor, "execute")

        async def invoke(credential: str | None) -> ActionResult:
            if request.action_id in READINESS_OBSERVATION_ACTION_IDS:
                from codex_web.services.code_host_readiness import readiness_execution
                with readiness_execution(self._observation_context(binding, provider, definition, request, actor, 'execute')):
                    result = await provider.execute(request, binding=binding, credential=credential)
            else:
                result = await provider.execute(request, binding=binding, credential=credential)
            if request.action_id in {CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID, *READINESS_OBSERVATION_ACTION_IDS} and self.procedure_admission is not None:
                await self.procedure_admission(binding,provider,definition,result,actor,"verify")
            return _credential_safe_output(result, credential)

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
        provider = self.registry.provider(
            binding.provider_type,
            binding.provider_instance,
            actor=actor,
        )
        definition = self._definition(provider, result.action_id)
        definition.capabilities.require("verification")
        if result.action_id in {CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID, *READINESS_OBSERVATION_ACTION_IDS} and self.procedure_admission is not None:
            await self.procedure_admission(binding,provider,definition,result,actor,"verify")

        async def invoke(credential: str | None) -> ActionVerification:
            if result.action_id in READINESS_OBSERVATION_ACTION_IDS:
                from codex_web.services.code_host_readiness import readiness_execution
                with readiness_execution(self._observation_context(binding, provider, definition, result, actor, 'verify')):
                    verification = await provider.verify(result, binding=binding, credential=credential)
            else:
                verification = await provider.verify(result, binding=binding, credential=credential)
            if result.action_id in {CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID, *READINESS_OBSERVATION_ACTION_IDS} and self.procedure_admission is not None:
                await self.procedure_admission(binding,provider,definition,result,actor,"verify")
            return _credential_safe_output(verification, credential)

        credential_ref = binding.credential_ref
        if credential_ref:
            if self.secret_broker is None:
                raise ActionRequirementError("credential broker is unavailable")
            return await self.secret_broker.use_async(
                credential_ref,
                actor=actor,
                operation=(
                    f"action-provider:{binding.provider_type}:"
                    f"{result.action_id}:verify"
                ),
                consumer=invoke,
                context={
                    "binding_id": binding.id,
                    "action_id": result.action_id,
                },
            )
        # Preserve compatibility for non-credential providers that implemented
        # the 1.1 callback before verification accepted a brokered credential.
        return await provider.verify(result, binding=binding)

    async def rollback(
        self,
        binding_id: str,
        result: ActionResult,
        *,
        actor: AuthenticationActor,
        credential_ref: str | None = None,
    ) -> ActionResult:
        binding = self.registry.binding(binding_id, actor)
        provider = self.registry.provider(
            binding.provider_type,
            binding.provider_instance,
            actor=actor,
        )
        definition = self._definition(provider, result.action_id)
        definition.capabilities.require("rollback")
        if self.procedure_admission is not None:
            await self.procedure_admission(binding, provider, definition, result, actor, "rollback")
        if not definition.reversible:
            raise ActionRequirementError("action is not declared reversible")

        async def invoke(credential: str | None) -> ActionResult:
            rolled_back = await provider.rollback(result, binding=binding, credential=credential)
            return _credential_safe_output(rolled_back, credential)

        if binding.credential_ref and credential_ref and binding.credential_ref != credential_ref:
            raise ActionRequirementError("rollback cannot override its original provider binding credential")
        credential_ref = binding.credential_ref or credential_ref
        if definition.credential_required and credential_ref is None:
            raise ActionRequirementError("rollback requires the originating credential reference")
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


def _stock_bound(owner, name, method):
    observed = getattr(owner, name, None)
    return (getattr(observed, '__self__', None) is owner
            and getattr(observed, '__func__', None) is method)


def _stock_registry_metadata(registry):
    return (type(registry) is ActionProviderRegistry
            and all(_stock_bound(registry, name, method)
                    for name, method in _CANONICAL_REGISTRY_METADATA_READS.items())
            and registry._provider_key is _CANONICAL_PROVIDER_KEY
            and type(registry.providers) is dict and type(registry.tenant_providers) is dict
            and type(registry.store) is ActionProviderStateStore
            and all(_stock_bound(registry.store, name, method)
                    for name, method in _CANONICAL_BINDING_STORE_READS.items()))


def _stock_secret_metadata(broker):
    if broker is None:
        return True
    from codex_web.storage.secret_state import SecretStateStore
    return (type(broker) is SecretBroker
            and all(_stock_bound(broker, name, method)
                    for name, method in _CANONICAL_SECRET_METADATA_READS.items())
            and broker._scope is _CANONICAL_SECRET_SCOPE
            and broker._admin is _CANONICAL_SECRET_ADMIN
            and getattr(broker._can_use, '__self__', None) is SecretBroker
            and getattr(broker._can_use, '__func__', None) is _CANONICAL_SECRET_USE
            and type(broker.store) is SecretStateStore
            and all(_stock_bound(broker.store, name, method)
                    for name, method in _CANONICAL_SECRET_STORE_READS.items()))


def _stock_provider_metadata(provider, resources):
    # Lazy imports avoid the provider -> ActionRequirementError import cycle.
    # The originals themselves are captured in each provider module at import,
    # never inferred from the current (possibly class-overridden) method.
    from codex_web.services.github_action_provider import GitHubActionProvider, _CANONICAL_GITHUB_ACTIONS
    from codex_web.services.gitlab_action_provider import GitLabActionProvider, _CANONICAL_GITLAB_ACTIONS
    from codex_web.services.code_host_action_contract import CodeHostActionContract
    methods = {GitHubActionProvider: _CANONICAL_GITHUB_ACTIONS,
               GitLabActionProvider: _CANONICAL_GITLAB_ACTIONS}
    method = methods.get(type(provider))
    if method is None:
        return False
    contract = getattr(provider, 'contract', None)
    return (_stock_bound(provider, 'actions', method)
            and type(contract) is CodeHostActionContract
            and _stock_bound(contract, 'actions', _CANONICAL_CODE_HOST_ACTIONS)
            and contract.resources is resources
            and type(contract.provider_type) is str
            and type(contract.credential_purpose) is str
            and contract.provider_type == provider.provider_type)


# Exact stock callbacks are captured once. No backend getter is replaced: fresh
# bindings, Secret ACL/version metadata and all contract judgments still run.
from codex_web.services.code_host_action_contract import CodeHostActionContract as _CodeHostActionContract
from codex_web.storage.secret_state import SecretStateStore as _SecretStateStore

_CANONICAL_EXECUTION_METADATA_READS = {name: getattr(ActionExecutionService, name) for name in (
    'resolve_contract', '_resolve', 'can_observe_contract_reads', '_require_contract_read_observation')}
_CANONICAL_CONTRACT_READ_CHECK = ActionExecutionService.can_observe_contract_reads
_CANONICAL_REQUIRE_CONTRACT_READ = ActionExecutionService._require_contract_read_observation
_CANONICAL_RESOURCE_REQUIRE = ResourceCatalogService.require_read_observation
_CANONICAL_RESOURCE_OBSERVE = ResourceCatalogService.observe_reads
_CANONICAL_ACTION_DEFINITION = ActionExecutionService._definition
_CANONICAL_REGISTRY_METADATA_READS = {name: getattr(ActionProviderRegistry, name) for name in (
    'binding', 'list_bindings', 'provider', 'provider_configuration')}
_CANONICAL_PROVIDER_KEY = ActionProviderRegistry._provider_key
_CANONICAL_BINDING_STORE_READS = {name: getattr(ActionProviderStateStore, name) for name in ('load', '_decode')}
_CANONICAL_CODE_HOST_ACTIONS = _CodeHostActionContract.actions
_CANONICAL_SECRET_METADATA_READS = {name: getattr(SecretBroker, name) for name in ('metadata', '_reference')}
_CANONICAL_SECRET_SCOPE = SecretBroker._scope
_CANONICAL_SECRET_ADMIN = SecretBroker._admin
_CANONICAL_SECRET_USE = SecretBroker._can_use.__func__
_CANONICAL_SECRET_STORE_READS = {name: getattr(_SecretStateStore, name) for name in (
    'reference_state', 'load', '_decode')}
