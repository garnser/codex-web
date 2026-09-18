from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.action_providers import (
    ActionProviderBindingCreate,
    ActionRequest,
)
from codex_web.extensions import (
    ExtensionGrantRequest,
    ExtensionInstallRequest,
    ExtensionManifest,
    ExtensionType,
)
from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderNotFoundError,
    ActionProviderRegistry,
    ActionResolutionError,
)
from codex_web.services.extension_conformance import ExtensionRuntimeDescriptor
from codex_web.services.extension_runtime import ExtensionRuntimeRegistry
from codex_web.services.extensions import (
    ExtensionAuthorizationError,
    ExtensionService,
)
from codex_web.services.identity import IdentityService
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceResolutionError,
)
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _manifest(
    extension_type: ExtensionType,
    *,
    extension_id: str,
) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "publisher": {
                "id": "com.example",
                "name": "Example",
            },
            "provenance": {
                "source": f"runtime-test:{extension_id}",
                "digest": "sha256:" + "a" * 64,
            },
            "compatibility": {
                "codex_web": ">=3.0.0 <4.0.0",
            },
            "types": [extension_type.value],
            "capabilities": {
                "requested": ["runtime.use"],
                "mandatory": ["runtime.use"],
            },
            "entrypoints": {
                extension_type.value: f"extension:{extension_type.value}",
            },
        }
    )


class ExtensionRuntimeRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.extensions = ExtensionService(ExtensionStateStore(self.sqlite))
        self.task_sources = TaskSourceRegistry()
        self.action_providers = ActionProviderRegistry(
            ActionProviderStateStore(self.sqlite)
        )
        self.runtime = ExtensionRuntimeRegistry(
            self.extensions,
            self.task_sources,
            self.action_providers,
        )
        self.action_execution = ActionExecutionService(
            self.action_providers,
            self.resources,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _install_enabled(
        self,
        extension_type: ExtensionType,
        *,
        extension_id: str,
    ):
        manifest = _manifest(
            extension_type,
            extension_id=extension_id,
        )
        installation = self.extensions.install(
            ExtensionInstallRequest(
                manifest=manifest,
                observed_digest=manifest.provenance.digest,
            ),
            actor=self.actor,
        )
        grants = self.extensions.grant(
            installation.id,
            ExtensionGrantRequest(capabilities=("runtime.use",)),
            actor=self.actor,
        )
        installation = self.extensions.enable(
            installation.id,
            actor=self.actor,
        )
        descriptor = ExtensionRuntimeDescriptor(
            extension_id=manifest.id,
            version=manifest.version,
            extension_type=extension_type,
            capabilities=("runtime.use",),
        )
        return installation, grants, descriptor

    async def test_task_source_dispatch_rechecks_grant_and_tenant(self) -> None:
        installation, grants, descriptor = self._install_enabled(
            ExtensionType.TASK_SOURCE,
            extension_id="com.example.runtime-task-source",
        )
        source = ReferenceTaskSource("extension-instance")
        self.runtime.register_task_source(
            installation.id,
            source_type="reference",
            factory=lambda state: source,
            descriptor=descriptor,
            actor=self.actor,
        )
        identity = TaskSourceIdentity(
            source_type="reference",
            source_instance="extension-instance",
            external_id="TASK-1",
        )
        state = WorkItemState(
            ref="task-source-runtime-1",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            source_identity=identity,
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )

        resolved = self.task_sources.resolve(state, required=True)
        self.assertIs(resolved, source)

        foreign = state.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        with self.assertRaises(TaskSourceResolutionError):
            self.task_sources.resolve(foreign, required=True)

        self.extensions.revoke_grant(
            installation.id,
            grants[0].id,
            actor=self.actor,
            reason="runtime authority revoked",
        )
        with self.assertRaises(ExtensionAuthorizationError):
            self.task_sources.resolve(state, required=True)

    async def test_two_tenants_can_register_same_task_source_type(self) -> None:
        local_installation, _, local_descriptor = self._install_enabled(
            ExtensionType.TASK_SOURCE,
            extension_id="com.example.local-task-source",
        )
        local_source = ReferenceTaskSource("local-instance")
        self.runtime.register_task_source(
            local_installation.id,
            source_type="reference",
            factory=lambda state: local_source,
            descriptor=local_descriptor,
            actor=self.actor,
        )

        foreign_actor = self.actor.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        foreign_manifest = _manifest(
            ExtensionType.TASK_SOURCE,
            extension_id="com.example.foreign-task-source",
        )
        foreign_installation = self.extensions.install(
            ExtensionInstallRequest(
                manifest=foreign_manifest,
                observed_digest=foreign_manifest.provenance.digest,
            ),
            actor=foreign_actor,
        )
        self.extensions.grant(
            foreign_installation.id,
            ExtensionGrantRequest(capabilities=("runtime.use",)),
            actor=foreign_actor,
        )
        self.extensions.enable(
            foreign_installation.id,
            actor=foreign_actor,
        )
        foreign_descriptor = ExtensionRuntimeDescriptor(
            extension_id=foreign_manifest.id,
            version=foreign_manifest.version,
            extension_type=ExtensionType.TASK_SOURCE,
            capabilities=("runtime.use",),
        )
        foreign_source = ReferenceTaskSource("foreign-instance")
        self.runtime.register_task_source(
            foreign_installation.id,
            source_type="reference",
            factory=lambda state: foreign_source,
            descriptor=foreign_descriptor,
            actor=foreign_actor,
        )

        local_state = WorkItemState(
            ref="local",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            source_identity=TaskSourceIdentity(
                source_type="reference",
                source_instance="local-instance",
                external_id="A",
            ),
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        foreign_state = local_state.model_copy(
            update={
                "ref": "foreign",
                "organization_id": "other",
                "workspace_id": "other",
                "source_identity": TaskSourceIdentity(
                    source_type="reference",
                    source_instance="foreign-instance",
                    external_id="B",
                ),
            }
        )

        self.assertIs(
            self.task_sources.resolve(local_state, required=True),
            local_source,
        )
        self.assertIs(
            self.task_sources.resolve(foreign_state, required=True),
            foreign_source,
        )

    async def test_action_provider_dispatch_rechecks_current_extension_authority(self) -> None:
        installation, grants, descriptor = self._install_enabled(
            ExtensionType.ACTION_PROVIDER,
            extension_id="com.example.runtime-action-provider",
        )
        provider = ReferenceActionProvider()
        wrapped = self.runtime.register_action_provider(
            installation.id,
            provider=provider,
            descriptor=descriptor,
            actor=self.actor,
        )
        self.assertIs(
            self.action_providers.provider(
                provider.provider_type,
                provider.provider_instance,
                actor=self.actor,
            ),
            wrapped,
        )

        resource = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.OTHER,
                name="Extension action target",
            ),
            actor=self.actor,
        )
        binding = self.action_providers.bind(
            ActionProviderBindingCreate(
                provider_type=provider.provider_type,
                provider_instance=provider.provider_instance,
                resource_ids=(resource.id,),
            ),
            actor=self.actor,
            resources=self.resources,
        )
        request = ActionRequest(
            action_id="reference.set",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            resource_ids=(resource.id,),
            parameters={
                "key": "runtime",
                "value": "authorized",
            },
        )

        prepared = await self.action_execution.prepare(
            binding.id,
            request,
            actor=self.actor,
        )
        self.assertEqual(prepared.provider_plan["operation"], "set")
        result = await self.action_execution.execute(
            binding.id,
            request,
            actor=self.actor,
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(provider.values["runtime"], "authorized")

        self.extensions.revoke_grant(
            installation.id,
            grants[0].id,
            actor=self.actor,
            reason="runtime authority revoked",
        )
        with self.assertRaises(ActionResolutionError):
            await self.action_execution.prepare(
                binding.id,
                request,
                actor=self.actor,
            )

    async def test_action_provider_registration_is_tenant_scoped(self) -> None:
        installation, _, descriptor = self._install_enabled(
            ExtensionType.ACTION_PROVIDER,
            extension_id="com.example.tenant-action-provider",
        )
        provider = ReferenceActionProvider()
        self.runtime.register_action_provider(
            installation.id,
            provider=provider,
            descriptor=descriptor,
            actor=self.actor,
        )

        foreign_actor = self.actor.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        with self.assertRaises(ActionProviderNotFoundError):
            self.action_providers.provider(
                provider.provider_type,
                provider.provider_instance,
                actor=foreign_actor,
            )

    async def test_unregister_removes_process_local_runtime_registration(self) -> None:
        task_installation, _, task_descriptor = self._install_enabled(
            ExtensionType.TASK_SOURCE,
            extension_id="com.example.unregister-task-source",
        )
        source = ReferenceTaskSource("unregister-instance")
        self.runtime.register_task_source(
            task_installation.id,
            source_type="reference",
            factory=lambda state: source,
            descriptor=task_descriptor,
            actor=self.actor,
        )
        state = WorkItemState(
            ref="unregister-task",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            source_identity=TaskSourceIdentity(
                source_type="reference",
                source_instance="unregister-instance",
                external_id="T",
            ),
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.assertIsNotNone(self.task_sources.resolve(state, required=True))

        self.runtime.unregister_installation(
            task_installation.id,
            actor=self.actor,
        )
        with self.assertRaises(TaskSourceResolutionError):
            self.task_sources.resolve(state, required=True)


if __name__ == "__main__":
    unittest.main()
