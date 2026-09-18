from __future__ import annotations

from dataclasses import dataclass

from codex_web.extensions import ExtensionManifest, ExtensionType
from codex_web.identity import AuthenticationActor
from codex_web.services.extensions import (
    ExtensionAuthorizationError,
    ExtensionCapabilityGrant,
    ExtensionService,
)


@dataclass(frozen=True, slots=True)
class ExtensionRuntimeDescriptor:
    """Provider-neutral identity presented by an extension runtime adapter."""

    extension_id: str
    version: str
    extension_type: ExtensionType
    capabilities: tuple[str, ...] = ()


class ExtensionConformanceSuite:
    """Shared pre-dispatch contract for every extension category."""

    @staticmethod
    def validate_descriptor(
        manifest: ExtensionManifest,
        descriptor: ExtensionRuntimeDescriptor,
    ) -> None:
        if descriptor.extension_id != manifest.id:
            raise ExtensionAuthorizationError(
                "extension runtime identity does not match installed manifest"
            )
        if descriptor.version != manifest.version:
            raise ExtensionAuthorizationError(
                "extension runtime version does not match installed manifest"
            )
        if descriptor.extension_type not in manifest.types:
            raise ExtensionAuthorizationError(
                f"extension runtime type {descriptor.extension_type.value} "
                "is not declared by installed manifest"
            )
        undeclared = (
            set(descriptor.capabilities)
            - set(manifest.capabilities.requested)
        )
        if undeclared:
            raise ExtensionAuthorizationError(
                "extension runtime requested undeclared capabilities: "
                + ", ".join(sorted(undeclared))
            )

    def authorize_runtime(
        self,
        service: ExtensionService,
        installation_id: str,
        descriptor: ExtensionRuntimeDescriptor,
        *,
        actor: AuthenticationActor,
        resource_ids: tuple[str, ...] = (),
    ) -> tuple[ExtensionCapabilityGrant, ...]:
        installation = service.get(installation_id, actor)
        self.validate_descriptor(installation.manifest, descriptor)
        grants = tuple(
            service.require_runtime_capability(
                installation_id,
                capability,
                actor=actor,
                resource_ids=resource_ids,
                extension_type=descriptor.extension_type,
            )
            for capability in descriptor.capabilities
        )
        return grants
