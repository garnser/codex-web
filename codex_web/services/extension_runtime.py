from __future__ import annotations

from codex_web.action_providers import (
    ActionProvider,
    ActionProviderBinding,
    ActionRequest,
    ActionResult,
    ActionVerification,
)
from codex_web.extensions import ExtensionLifecycleState, ExtensionType
from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.models import WorkItemState
from codex_web.services.action_provider_conformance import (
    ActionProviderConformanceSuite,
)
from codex_web.services.action_providers import (
    ActionProviderRegistry,
    ActionResolutionError,
)
from codex_web.services.extension_conformance import (
    ExtensionConformanceSuite,
    ExtensionRuntimeDescriptor,
)
from codex_web.services.extensions import (
    ExtensionAuthorizationError,
    ExtensionConflictError,
    ExtensionService,
)
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_source_runtime import (
    TaskSourceFactory,
    TaskSourceRegistry,
    TaskSourceResolutionError,
)
from codex_web.services.task_sources import TaskSource


class ExtensionRuntimeRegistrationError(ExtensionConflictError):
    pass


class AuthorizedExtensionActionProvider:
    """ActionProvider wrapper that re-checks canonical extension authority.

    The wrapped provider is already loaded by a trusted runtime boundary. This
    wrapper does not import extension code or resolve secret values.
    """

    def __init__(
        self,
        provider: ActionProvider,
        *,
        extensions: ExtensionService,
        installation_id: str,
        descriptor: ExtensionRuntimeDescriptor,
        actor: AuthenticationActor,
    ) -> None:
        self._provider = provider
        self._extensions = extensions
        self._installation_id = installation_id
        self._descriptor = descriptor
        self._actor = actor
        self.contract_version = provider.contract_version
        self.provider_type = provider.provider_type
        self.provider_instance = provider.provider_instance

    def _tenant_matches(
        self,
        organization_id: str,
        workspace_id: str,
    ) -> None:
        if (
            organization_id != self._actor.organization_id
            or workspace_id != self._actor.workspace_id
        ):
            raise ActionResolutionError(
                "extension action provider cannot cross tenant boundary"
            )

    def _registration_available(self) -> bool:
        installation = self._extensions.get(
            self._installation_id,
            self._actor,
        )
        if installation.lifecycle != ExtensionLifecycleState.ENABLED:
            return False
        try:
            ExtensionConformanceSuite.validate_descriptor(
                installation.manifest,
                self._descriptor,
            )
        except ExtensionAuthorizationError:
            return False
        active = {
            item.capability
            for item in self._extensions.grants(
                self._installation_id,
                self._actor,
            )
            if item.active
        }
        return set(self._descriptor.capabilities).issubset(active)

    def _authorize(self, resource_ids: tuple[str, ...]) -> None:
        try:
            ExtensionConformanceSuite().authorize_runtime(
                self._extensions,
                self._installation_id,
                self._descriptor,
                actor=self._actor,
                resource_ids=resource_ids,
            )
        except ExtensionAuthorizationError as exc:
            raise ActionResolutionError(
                f"extension action provider authority unavailable: {exc}"
            ) from exc

    def actions(self):
        if not self._registration_available():
            return ()
        return self._provider.actions()

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ):
        self._tenant_matches(
            request.organization_id,
            request.workspace_id,
        )
        self._authorize(tuple(request.resource_ids))
        return await self._provider.prepare(request, binding=binding)

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        self._tenant_matches(
            request.organization_id,
            request.workspace_id,
        )
        self._authorize(tuple(request.resource_ids))
        return await self._provider.execute(
            request,
            binding=binding,
            credential=credential,
        )

    async def verify(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
    ) -> ActionVerification:
        self._tenant_matches(
            binding.organization_id,
            binding.workspace_id,
        )
        self._authorize(tuple(binding.resource_ids))
        return await self._provider.verify(result, binding=binding)

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        self._tenant_matches(
            binding.organization_id,
            binding.workspace_id,
        )
        self._authorize(tuple(binding.resource_ids))
        return await self._provider.rollback(
            result,
            binding=binding,
            credential=credential,
        )


class ExtensionRuntimeRegistry:
    """Bridge canonical extension state into existing provider registries.

    Registration accepts already-loaded adapter objects/factories only. Package
    discovery remains metadata-only and never imports extension payloads.
    """

    def __init__(
        self,
        extensions: ExtensionService,
        task_sources: TaskSourceRegistry,
        action_providers: ActionProviderRegistry,
    ) -> None:
        self.extensions = extensions
        self.task_sources = task_sources
        self.action_providers = action_providers
        self.conformance = ExtensionConformanceSuite()
        self.task_source_conformance = TaskSourceConformanceSuite()
        self.action_provider_conformance = ActionProviderConformanceSuite()
        self._task_source_registrations: dict[
            tuple[str, str, str],
            str,
        ] = {}
        self._action_provider_registrations: dict[
            tuple[str, str, str, str],
            str,
        ] = {}
        self.extensions.add_lifecycle_listener(self._on_extension_lifecycle)

    def _on_extension_lifecycle(
        self,
        installation,
        event_type: str,
    ) -> None:
        if installation.lifecycle != ExtensionLifecycleState.ENABLED:
            self.unregister_installation(installation.id)

    def _assert_registration_ready(
        self,
        installation_id: str,
        descriptor: ExtensionRuntimeDescriptor,
        *,
        actor: AuthenticationActor,
    ) -> None:
        installation = self.extensions.get(installation_id, actor)
        if installation.lifecycle != ExtensionLifecycleState.ENABLED:
            raise ExtensionRuntimeRegistrationError(
                "extension must be enabled before runtime registration"
            )
        self.conformance.validate_descriptor(
            installation.manifest,
            descriptor,
        )
        active = {
            item.capability
            for item in self.extensions.grants(
                installation_id,
                actor,
            )
            if item.active
        }
        missing = set(descriptor.capabilities) - active
        if missing:
            raise ExtensionAuthorizationError(
                "extension runtime capabilities are not granted: "
                + ", ".join(sorted(missing))
            )

    def register_task_source(
        self,
        installation_id: str,
        *,
        source_type: str,
        factory: TaskSourceFactory,
        descriptor: ExtensionRuntimeDescriptor,
        actor: AuthenticationActor,
    ) -> None:
        if descriptor.extension_type != ExtensionType.TASK_SOURCE:
            raise ExtensionRuntimeRegistrationError(
                "task source registration requires task_source extension type"
            )
        self._assert_registration_ready(
            installation_id,
            descriptor,
            actor=actor,
        )
        key = (
            actor.organization_id,
            actor.workspace_id,
            str(source_type or "").strip().casefold(),
        )
        if not key[2]:
            raise ValueError("task-source type must not be empty")
        owner = self._task_source_registrations.get(key)
        if owner is not None and owner != installation_id:
            raise ExtensionRuntimeRegistrationError(
                "task-source type is already owned by another extension "
                "in this tenant"
            )

        def guarded_factory(state: WorkItemState) -> TaskSource | None:
            if (
                state.organization_id != actor.organization_id
                or state.workspace_id != actor.workspace_id
            ):
                return None
            try:
                self.conformance.authorize_runtime(
                    self.extensions,
                    installation_id,
                    descriptor,
                    actor=actor,
                    resource_ids=tuple(state.resource_ids),
                )
            except ExtensionAuthorizationError as exc:
                raise TaskSourceResolutionError(
                    f"extension task-source authority unavailable: {exc}"
                ) from exc
            source = factory(state)
            if source is None:
                return None
            self.task_source_conformance.validate_adapter(source)
            return source

        self.task_sources.register_tenant(
            actor.tenant,
            source_type,
            guarded_factory,
        )
        self._task_source_registrations[key] = installation_id

    def register_action_provider(
        self,
        installation_id: str,
        *,
        provider: ActionProvider,
        descriptor: ExtensionRuntimeDescriptor,
        actor: AuthenticationActor,
    ) -> ActionProvider:
        if descriptor.extension_type != ExtensionType.ACTION_PROVIDER:
            raise ExtensionRuntimeRegistrationError(
                "action provider registration requires action_provider extension type"
            )
        self._assert_registration_ready(
            installation_id,
            descriptor,
            actor=actor,
        )
        self.action_provider_conformance.validate_contract(provider)
        key = (
            actor.organization_id,
            actor.workspace_id,
            provider.provider_type,
            provider.provider_instance,
        )
        owner = self._action_provider_registrations.get(key)
        if owner is not None and owner != installation_id:
            raise ExtensionRuntimeRegistrationError(
                "action provider identity is already owned by another extension "
                "in this tenant"
            )
        wrapped = AuthorizedExtensionActionProvider(
            provider,
            extensions=self.extensions,
            installation_id=installation_id,
            descriptor=descriptor,
            actor=actor,
        )
        self.action_providers.register(
            wrapped,
            tenant_scope=actor.tenant,
        )
        self._action_provider_registrations[key] = installation_id
        return wrapped

    def unregister_installation(
        self,
        installation_id: str,
    ) -> None:
        for key, owner in list(self._task_source_registrations.items()):
            if owner == installation_id:
                scope = TenantScope(
                    organization_id=key[0],
                    workspace_id=key[1],
                )
                self.task_sources.unregister_tenant(
                    scope,
                    key[2],
                )
                self._task_source_registrations.pop(key, None)

        for key, owner in list(self._action_provider_registrations.items()):
            if owner == installation_id:
                scope = TenantScope(
                    organization_id=key[0],
                    workspace_id=key[1],
                )
                self.action_providers.unregister_tenant(
                    key[2],
                    key[3],
                    tenant_scope=scope,
                )
                self._action_provider_registrations.pop(key, None)
