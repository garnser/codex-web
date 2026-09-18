from __future__ import annotations

import re
import secrets
import time
from typing import Protocol

from codex_web.artifact_evidence import (
    EvidenceLifecycle,
    EvidenceResult,
    EvidenceType,
)
from codex_web.extensions import (
    EXTENSION_HOST_COMPATIBILITY_VERSION,
    ExtensionAuditEvent,
    ExtensionCapabilityGrant,
    ExtensionConfigureRequest,
    ExtensionDeploymentMode,
    ExtensionGrantRequest,
    ExtensionHealthReport,
    ExtensionHealthStatus,
    ExtensionInstallRequest,
    ExtensionInstallation,
    ExtensionLifecycleState,
    ExtensionManifest,
    ExtensionPackageVerification,
    ExtensionRemoveRequest,
    ExtensionSignatureStatus,
    ExtensionType,
    ExtensionUpgradeRequest,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.configuration import ConfigurationService
from codex_web.services.identity import AuthorizationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.extensions import ExtensionStateStore


class ExtensionError(RuntimeError):
    pass


class ExtensionNotFoundError(ExtensionError):
    pass


class ExtensionConflictError(ExtensionError):
    pass


class ExtensionCompatibilityError(ExtensionError):
    pass


class ExtensionIntegrityError(ExtensionError):
    pass


class ExtensionAuthorizationError(ExtensionError):
    pass


class ExtensionPackageVerifier(Protocol):
    name: str

    def verify(
        self,
        manifest: ExtensionManifest,
        observed_digest: str,
    ) -> ExtensionPackageVerification:
        ...


class DigestOnlyExtensionPackageVerifier:
    """Self-hosted baseline verifier.

    It authenticates the observed package digest against the immutable manifest
    declaration but deliberately does not claim cryptographic signature
    verification. Hosted mode therefore rejects this verifier's signed packages
    until a real signature verifier is configured.
    """

    name = "digest-only-v1"

    def verify(
        self,
        manifest: ExtensionManifest,
        observed_digest: str,
    ) -> ExtensionPackageVerification:
        digest_ok = secrets.compare_digest(
            manifest.provenance.digest,
            observed_digest,
        )
        signature_status = (
            ExtensionSignatureStatus.UNVERIFIED
            if manifest.provenance.signature
            else ExtensionSignatureStatus.UNSIGNED
        )
        return ExtensionPackageVerification(
            digest_verified=digest_ok,
            signature_status=signature_status,
            verifier=self.name,
            observed_digest=observed_digest,
            detail=None if digest_ok else "observed package digest does not match manifest",
        )


_SEMVER_CORE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?(.+)$")


def _semver(value: str) -> tuple[int, int, int, tuple[str, ...]]:
    match = _SEMVER_CORE.fullmatch(value.strip())
    if not match:
        raise ExtensionCompatibilityError(f"unsupported semantic version: {value!r}")
    prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        prerelease,
    )


def _compare_semver(left: str, right: str) -> int:
    l_major, l_minor, l_patch, l_pre = _semver(left)
    r_major, r_minor, r_patch, r_pre = _semver(right)
    l_core = (l_major, l_minor, l_patch)
    r_core = (r_major, r_minor, r_patch)
    if l_core != r_core:
        return -1 if l_core < r_core else 1
    if l_pre == r_pre:
        return 0
    if not l_pre:
        return 1
    if not r_pre:
        return -1
    return -1 if l_pre < r_pre else 1


def extension_version_satisfies(version: str, constraint: str) -> bool:
    """Evaluate the intentionally small v1 compatibility grammar.

    Supported forms are whitespace/comma separated exact or comparator clauses,
    for example >=3.0.0 <4.0.0. Unknown range syntaxes fail closed instead of
    being guessed.
    """

    clauses = [
        item
        for raw in constraint.replace(",", " ").split()
        if (item := raw.strip())
    ]
    if not clauses:
        raise ExtensionCompatibilityError("extension compatibility constraint is empty")
    for clause in clauses:
        match = _COMPARATOR.fullmatch(clause)
        if not match:
            raise ExtensionCompatibilityError(
                f"unsupported compatibility clause: {clause!r}"
            )
        operator = match.group(1) or "="
        target = match.group(2)
        comparison = _compare_semver(version, target)
        accepted = {
            "=": comparison == 0,
            ">": comparison > 0,
            ">=": comparison >= 0,
            "<": comparison < 0,
            "<=": comparison <= 0,
        }[operator]
        if not accepted:
            return False
    return True


class ExtensionService:
    def __init__(
        self,
        store: ExtensionStateStore,
        *,
        verifier: ExtensionPackageVerifier | None = None,
        host_version: str = EXTENSION_HOST_COMPATIBILITY_VERSION,
        secrets: SecretBroker | None = None,
        configuration: ConfigurationService | None = None,
        resources: ResourceCatalogService | None = None,
        artifact_evidence: ArtifactEvidenceService | None = None,
        unhealthy_quarantine_threshold: int = 3,
    ) -> None:
        self.store = store
        self.verifier = verifier or DigestOnlyExtensionPackageVerifier()
        self.host_version = host_version
        self.secrets = secrets
        self.configuration = configuration
        self.resources = resources
        self.artifact_evidence = artifact_evidence
        self.unhealthy_quarantine_threshold = max(1, unhealthy_quarantine_threshold)

    @staticmethod
    def _same_scope(item, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "extensions:admin" not in actor.service_scopes:
                raise AuthorizationError("extensions:admin service scope required")
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")

    @staticmethod
    def _require_health_reporter(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not {"extensions:health", "extensions:admin"}.intersection(
                actor.service_scopes
            ):
                raise AuthorizationError(
                    "extensions:health or extensions:admin service scope required"
                )
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")

    def _event(
        self,
        state,
        installation: ExtensionInstallation,
        actor: AuthenticationActor,
        event_type: str,
        **details,
    ) -> None:
        state.events.append(
            ExtensionAuditEvent(
                organization_id=installation.organization_id,
                workspace_id=installation.workspace_id,
                installation_id=installation.id,
                extension_id=installation.manifest.id,
                event_type=event_type,
                actor_id=actor.identity_id,
                details=details,
            )
        )

    def _installation(
        self,
        state,
        installation_id: str,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        item = next(
            (
                value
                for value in state.installations
                if value.id == installation_id
                and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise ExtensionNotFoundError("extension installation not found")
        return item

    def _replace(self, state, installation: ExtensionInstallation) -> None:
        state.installations = [
            installation if item.id == installation.id else item
            for item in state.installations
        ]

    def _active_grants(
        self,
        state,
        installation: ExtensionInstallation,
    ) -> list[ExtensionCapabilityGrant]:
        return [
            item
            for item in state.grants
            if item.installation_id == installation.id
            and item.organization_id == installation.organization_id
            and item.workspace_id == installation.workspace_id
            and item.active
        ]

    def _verify_package(
        self,
        manifest: ExtensionManifest,
        observed_digest: str,
        deployment_mode: ExtensionDeploymentMode,
    ) -> ExtensionPackageVerification:
        verification = self.verifier.verify(manifest, observed_digest)
        if not verification.digest_verified:
            raise ExtensionIntegrityError(
                verification.detail or "extension package digest verification failed"
            )
        if verification.signature_status == ExtensionSignatureStatus.INVALID:
            raise ExtensionIntegrityError("extension package signature is invalid")
        if (
            deployment_mode == ExtensionDeploymentMode.HOSTED
            and verification.signature_status != ExtensionSignatureStatus.VERIFIED
        ):
            raise ExtensionIntegrityError(
                "hosted deployment requires a cryptographically verified extension signature"
            )
        return verification

    def _compatibility_reason(self, manifest: ExtensionManifest) -> str | None:
        try:
            compatible = extension_version_satisfies(
                self.host_version,
                manifest.compatibility.codex_web,
            )
        except ExtensionCompatibilityError as exc:
            return str(exc)
        if compatible:
            return None
        return (
            f"extension requires codex-web {manifest.compatibility.codex_web}; "
            f"host compatibility version is {self.host_version}"
        )

    def list(self, actor: AuthenticationActor) -> list[ExtensionInstallation]:
        return sorted(
            [
                item
                for item in self.store.load().installations
                if self._same_scope(item, actor)
            ],
            key=lambda item: (item.manifest.id, item.installed_at, item.id),
        )

    def get(
        self,
        installation_id: str,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        return self._installation(self.store.load(), installation_id, actor)

    def _validate_package_verification(
        self,
        manifest: ExtensionManifest,
        verification: ExtensionPackageVerification,
        deployment_mode: ExtensionDeploymentMode,
    ) -> ExtensionPackageVerification:
        if verification.observed_digest != manifest.provenance.digest:
            raise ExtensionIntegrityError(
                "server-observed package digest does not match manifest"
            )
        if not verification.digest_verified:
            raise ExtensionIntegrityError(
                verification.detail or "extension package digest verification failed"
            )
        if verification.signature_status == ExtensionSignatureStatus.INVALID:
            raise ExtensionIntegrityError("extension package signature is invalid")
        if (
            deployment_mode == ExtensionDeploymentMode.HOSTED
            and verification.signature_status != ExtensionSignatureStatus.VERIFIED
        ):
            raise ExtensionIntegrityError(
                "hosted deployment requires a cryptographically verified extension signature"
            )
        return verification

    def _persist_installation(
        self,
        *,
        manifest: ExtensionManifest,
        verification: ExtensionPackageVerification,
        deployment_mode: ExtensionDeploymentMode,
        actor: AuthenticationActor,
        package_ref: str | None = None,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        verification = self._validate_package_verification(
            manifest,
            verification,
            deployment_mode,
        )
        incompatible_reason = self._compatibility_reason(manifest)
        created: list[ExtensionInstallation] = []

        def apply(state):
            duplicate = next(
                (
                    item
                    for item in state.installations
                    if self._same_scope(item, actor)
                    and item.manifest.id == manifest.id
                    and item.lifecycle != ExtensionLifecycleState.REMOVED
                ),
                None,
            )
            if duplicate is not None:
                raise ExtensionConflictError(
                    "extension is already installed; use the upgrade lifecycle"
                )
            lifecycle = (
                ExtensionLifecycleState.INCOMPATIBLE
                if incompatible_reason
                else ExtensionLifecycleState.INSTALLED
            )
            item = ExtensionInstallation(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                manifest=manifest,
                package_verification=verification,
                package_ref=package_ref,
                deployment_mode=deployment_mode,
                lifecycle=lifecycle,
                installed_by=actor.identity_id,
                incompatible_reason=incompatible_reason,
            )
            state.installations.append(item)
            self._event(
                state,
                item,
                actor,
                "extension_installed",
                version=item.manifest.version,
                lifecycle=item.lifecycle.value,
                deployment_mode=item.deployment_mode.value,
                signature_status=item.package_verification.signature_status.value,
                package_ref=package_ref,
                verifier=item.package_verification.verifier,
            )
            created.append(item)
            return state

        self.store.update(apply)
        return created[0]

    def install_verified_package(
        self,
        *,
        manifest: ExtensionManifest,
        verification: ExtensionPackageVerification,
        deployment_mode: ExtensionDeploymentMode,
        actor: AuthenticationActor,
        package_ref: str,
    ) -> ExtensionInstallation:
        """Install from a server-observed package candidate.

        This seam is intentionally internal/provider-facing: public package
        installation obtains manifest and verification from a configured
        package catalog, never from request fields.
        """
        return self._persist_installation(
            manifest=manifest,
            verification=verification,
            deployment_mode=deployment_mode,
            actor=actor,
            package_ref=package_ref,
        )

    def install(
        self,
        payload: ExtensionInstallRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        verification = self._verify_package(
            payload.manifest,
            payload.observed_digest,
            payload.deployment_mode,
        )
        return self._persist_installation(
            manifest=payload.manifest,
            verification=verification,
            deployment_mode=payload.deployment_mode,
            actor=actor,
        )

    def configure(
        self,
        installation_id: str,
        payload: ExtensionConfigureRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle in {
                ExtensionLifecycleState.REMOVED,
                ExtensionLifecycleState.INCOMPATIBLE,
                ExtensionLifecycleState.UPGRADING,
            }:
                raise ExtensionConflictError(
                    f"cannot configure extension while {item.lifecycle.value}"
                )
            declared_slots = set(item.manifest.configuration.secret_refs)
            incoming_slots = set(payload.secret_bindings)
            unknown_slots = incoming_slots - declared_slots
            if unknown_slots:
                raise ExtensionConflictError(
                    "undeclared extension secret slot: "
                    + ", ".join(sorted(unknown_slots))
                )
            if payload.secret_bindings and self.secrets is None:
                raise ExtensionConflictError("secret broker is unavailable")
            if self.secrets is not None:
                for secret_id in payload.secret_bindings.values():
                    self.secrets.metadata(
                        secret_id,
                        actor=actor,
                        require_use=True,
                    )
            if payload.configuration_record_ids and self.configuration is None:
                raise ExtensionConflictError("configuration registry is unavailable")
            if self.configuration is not None:
                for record_id in payload.configuration_record_ids:
                    self.configuration.get_record(record_id)

            lifecycle = (
                ExtensionLifecycleState.DISABLED
                if item.lifecycle == ExtensionLifecycleState.DISABLED
                else ExtensionLifecycleState.CONFIGURED
            )
            replacement = item.model_copy(
                update={
                    "configuration_record_ids": tuple(
                        dict.fromkeys(payload.configuration_record_ids)
                    ),
                    "secret_bindings": dict(payload.secret_bindings),
                    "lifecycle": lifecycle,
                    "updated_at": time.time(),
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_configured",
                configuration_reference_count=len(
                    replacement.configuration_record_ids
                ),
                secret_binding_count=len(replacement.secret_bindings),
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def grant(
        self,
        installation_id: str,
        payload: ExtensionGrantRequest,
        *,
        actor: AuthenticationActor,
    ) -> list[ExtensionCapabilityGrant]:
        self._require_admin(actor)
        created: list[ExtensionCapabilityGrant] = []

        if payload.resource_ids:
            if self.resources is None:
                raise ExtensionConflictError("resource catalog is unavailable")
            for resource_id in payload.resource_ids:
                self.resources.get(resource_id, actor)

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle == ExtensionLifecycleState.REMOVED:
                raise ExtensionConflictError("removed extension cannot receive grants")
            declared = set(item.manifest.capabilities.requested)
            unknown = set(payload.capabilities) - declared
            if unknown:
                raise ExtensionAuthorizationError(
                    "cannot grant undeclared extension capabilities: "
                    + ", ".join(sorted(unknown))
                )
            active = {
                grant.capability: grant
                for grant in self._active_grants(state, item)
            }
            for capability in payload.capabilities:
                existing = active.get(capability)
                if (
                    existing is not None
                    and existing.resource_ids == payload.resource_ids
                ):
                    created.append(existing)
                    continue
                if existing is not None:
                    now = time.time()
                    replacement = existing.model_copy(
                        update={
                            "revoked_at": now,
                            "revoked_by": actor.identity_id,
                            "revoke_reason": "scope replaced",
                        }
                    )
                    state.grants = [
                        replacement if grant.id == existing.id else grant
                        for grant in state.grants
                    ]
                grant = ExtensionCapabilityGrant(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    installation_id=item.id,
                    capability=capability,
                    resource_ids=payload.resource_ids,
                    granted_by=actor.identity_id,
                )
                state.grants.append(grant)
                created.append(grant)
                self._event(
                    state,
                    item,
                    actor,
                    "extension_capability_granted",
                    capability=capability,
                    resource_count=len(grant.resource_ids),
                )
            return state

        self.store.update(apply)
        return created

    def revoke_grant(
        self,
        installation_id: str,
        grant_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> ExtensionCapabilityGrant:
        self._require_admin(actor)
        updated: list[ExtensionCapabilityGrant] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            grant = next(
                (
                    value
                    for value in state.grants
                    if value.id == grant_id
                    and value.installation_id == item.id
                    and value.organization_id == actor.organization_id
                    and value.workspace_id == actor.workspace_id
                ),
                None,
            )
            if grant is None:
                raise ExtensionNotFoundError("extension capability grant not found")
            if not grant.active:
                updated.append(grant)
                return state
            now = time.time()
            replacement = grant.model_copy(
                update={
                    "revoked_at": now,
                    "revoked_by": actor.identity_id,
                    "revoke_reason": reason,
                }
            )
            state.grants = [
                replacement if value.id == grant.id else value
                for value in state.grants
            ]
            self._event(
                state,
                item,
                actor,
                "extension_capability_revoked",
                capability=grant.capability,
                reason=reason,
            )
            if (
                item.lifecycle == ExtensionLifecycleState.ENABLED
                and grant.capability in item.manifest.capabilities.mandatory
            ):
                quarantined = item.model_copy(
                    update={
                        "lifecycle": ExtensionLifecycleState.QUARANTINED,
                        "quarantine_reason": (
                            f"mandatory capability revoked: {grant.capability}"
                        ),
                        "updated_at": now,
                    }
                )
                self._replace(state, quarantined)
                self._event(
                    state,
                    quarantined,
                    actor,
                    "extension_quarantined",
                    reason=quarantined.quarantine_reason,
                )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def grants(
        self,
        installation_id: str,
        actor: AuthenticationActor,
    ) -> list[ExtensionCapabilityGrant]:
        state = self.store.load()
        item = self._installation(state, installation_id, actor)
        return sorted(
            [
                grant
                for grant in state.grants
                if grant.installation_id == item.id
                and grant.organization_id == actor.organization_id
                and grant.workspace_id == actor.workspace_id
            ],
            key=lambda grant: (grant.capability, grant.granted_at, grant.id),
        )

    def _assert_enable_ready(
        self,
        state,
        item: ExtensionInstallation,
    ) -> None:
        if item.incompatible_reason is not None:
            raise ExtensionCompatibilityError(item.incompatible_reason)
        if item.package_verification.signature_status == ExtensionSignatureStatus.INVALID:
            raise ExtensionIntegrityError("extension signature is invalid")
        if (
            item.deployment_mode == ExtensionDeploymentMode.HOSTED
            and item.package_verification.signature_status
            != ExtensionSignatureStatus.VERIFIED
        ):
            raise ExtensionIntegrityError(
                "hosted extension signature is not cryptographically verified"
            )
        if item.health_status == ExtensionHealthStatus.UNHEALTHY:
            raise ExtensionConflictError("unhealthy extension cannot be enabled")
        grants = {
            grant.capability
            for grant in self._active_grants(state, item)
        }
        missing_capabilities = (
            set(item.manifest.capabilities.mandatory) - grants
        )
        if missing_capabilities:
            raise ExtensionAuthorizationError(
                "mandatory extension capabilities are not granted: "
                + ", ".join(sorted(missing_capabilities))
            )
        required_slots = set(item.manifest.configuration.secret_refs)
        missing_slots = required_slots - set(item.secret_bindings)
        if missing_slots:
            raise ExtensionConflictError(
                "required extension secret bindings are missing: "
                + ", ".join(sorted(missing_slots))
            )
        if (
            item.manifest.configuration.schema_path
            and not item.configuration_record_ids
        ):
            raise ExtensionConflictError(
                "extension requires typed configuration before enablement"
            )

    def enable(
        self,
        installation_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle == ExtensionLifecycleState.ENABLED:
                updated.append(item)
                return state
            if item.lifecycle not in {
                ExtensionLifecycleState.INSTALLED,
                ExtensionLifecycleState.CONFIGURED,
                ExtensionLifecycleState.DISABLED,
            }:
                raise ExtensionConflictError(
                    f"cannot enable extension while {item.lifecycle.value}"
                )
            self._assert_enable_ready(state, item)
            replacement = item.model_copy(
                update={
                    "lifecycle": ExtensionLifecycleState.ENABLED,
                    "disabled_reason": None,
                    "quarantine_reason": None,
                    "updated_at": time.time(),
                }
            )
            self._replace(state, replacement)
            self._event(state, replacement, actor, "extension_enabled")
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def disable(
        self,
        installation_id: str,
        *,
        actor: AuthenticationActor,
        reason: str | None = None,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle in {
                ExtensionLifecycleState.REMOVED,
                ExtensionLifecycleState.INCOMPATIBLE,
            }:
                raise ExtensionConflictError(
                    f"cannot disable extension while {item.lifecycle.value}"
                )
            replacement = item.model_copy(
                update={
                    "lifecycle": ExtensionLifecycleState.DISABLED,
                    "disabled_reason": reason,
                    "updated_at": time.time(),
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_disabled",
                reason=reason,
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def quarantine(
        self,
        installation_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle == ExtensionLifecycleState.REMOVED:
                raise ExtensionConflictError("removed extension cannot be quarantined")
            replacement = item.model_copy(
                update={
                    "lifecycle": ExtensionLifecycleState.QUARANTINED,
                    "quarantine_reason": reason,
                    "updated_at": time.time(),
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_quarantined",
                reason=reason,
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def clear_quarantine(
        self,
        installation_id: str,
        *,
        actor: AuthenticationActor,
        reason: str | None = None,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle != ExtensionLifecycleState.QUARANTINED:
                raise ExtensionConflictError("extension is not quarantined")
            replacement = item.model_copy(
                update={
                    "lifecycle": ExtensionLifecycleState.DISABLED,
                    "quarantine_reason": None,
                    "disabled_reason": reason or "quarantine cleared",
                    "consecutive_health_failures": 0,
                    "updated_at": time.time(),
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_quarantine_cleared",
                reason=reason,
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def report_health(
        self,
        installation_id: str,
        report: ExtensionHealthReport,
        *,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        self._require_health_reporter(actor)
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle == ExtensionLifecycleState.REMOVED:
                raise ExtensionConflictError("removed extension cannot report health")
            failures = (
                0
                if report.status == ExtensionHealthStatus.HEALTHY
                else item.consecutive_health_failures
                + (1 if report.status == ExtensionHealthStatus.UNHEALTHY else 0)
            )
            lifecycle = item.lifecycle
            quarantine_reason = item.quarantine_reason
            if (
                report.status == ExtensionHealthStatus.UNHEALTHY
                and failures >= self.unhealthy_quarantine_threshold
                and item.lifecycle == ExtensionLifecycleState.ENABLED
            ):
                lifecycle = ExtensionLifecycleState.QUARANTINED
                quarantine_reason = (
                    f"extension health circuit breaker tripped after {failures} failures"
                )
            replacement = item.model_copy(
                update={
                    "health_status": report.status,
                    "consecutive_health_failures": failures,
                    "last_health_at": time.time(),
                    "last_health_detail": report.detail,
                    "lifecycle": lifecycle,
                    "quarantine_reason": quarantine_reason,
                    "updated_at": time.time(),
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_health_reported",
                health_status=report.status.value,
                consecutive_failures=failures,
            )
            if (
                lifecycle == ExtensionLifecycleState.QUARANTINED
                and item.lifecycle != ExtensionLifecycleState.QUARANTINED
            ):
                self._event(
                    state,
                    replacement,
                    actor,
                    "extension_quarantined",
                    reason=quarantine_reason,
                )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def _upgrade_with_verification(
        self,
        installation_id: str,
        *,
        manifest: ExtensionManifest,
        verification: ExtensionPackageVerification,
        package_ref: str | None,
        migration_evidence_id: str | None,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        current = self.get(installation_id, actor)
        if current.lifecycle == ExtensionLifecycleState.ENABLED:
            raise ExtensionConflictError("disable extension before upgrade")
        if current.lifecycle == ExtensionLifecycleState.REMOVED:
            raise ExtensionConflictError("removed extension cannot be upgraded")
        if manifest.id != current.manifest.id:
            raise ExtensionConflictError("upgrade manifest extension id changed")
        if manifest.version == current.manifest.version:
            raise ExtensionConflictError("upgrade version must change")
        verification = self._validate_package_verification(
            manifest,
            verification,
            current.deployment_mode,
        )
        incompatible_reason = self._compatibility_reason(manifest)
        verified_migration_evidence_id = None
        if manifest.migrations.entrypoint:
            if migration_evidence_id is None:
                raise ExtensionConflictError(
                    "extension declares a migration entrypoint; canonical migration evidence is required"
                )
            if self.artifact_evidence is None:
                raise ExtensionConflictError(
                    "artifact/evidence service is unavailable for extension migration verification"
                )
            evidence = next(
                (
                    item
                    for item in self.artifact_evidence.list_evidence(
                        actor,
                        include_inactive=False,
                    )
                    if item.id == migration_evidence_id
                ),
                None,
            )
            if evidence is None:
                raise ExtensionConflictError("extension migration evidence not found")
            if (
                evidence.lifecycle != EvidenceLifecycle.VALID
                or evidence.result != EvidenceResult.PASS
                or evidence.evidence_type
                not in {
                    EvidenceType.POLICY_EVALUATION,
                    EvidenceType.ARTIFACT_VERIFICATION,
                    EvidenceType.TEST_RESULT,
                    EvidenceType.CI_CHECK,
                }
                or evidence.metadata.get("extension_id") != current.manifest.id
                or evidence.metadata.get("to_version") != manifest.version
            ):
                raise ExtensionConflictError(
                    "extension migration evidence does not verify this target version"
                )
            verified_migration_evidence_id = evidence.id
        elif migration_evidence_id is not None:
            raise ExtensionConflictError(
                "migration evidence supplied but target manifest declares no migration"
            )

        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.manifest.version != current.manifest.version:
                raise ExtensionConflictError(
                    "extension changed while upgrade was being prepared"
                )
            requested = set(manifest.capabilities.requested)
            now = time.time()
            state.grants = [
                grant.model_copy(
                    update={
                        "revoked_at": now,
                        "revoked_by": actor.identity_id,
                        "revoke_reason": "capability removed by extension upgrade",
                    }
                )
                if (
                    grant.installation_id == item.id
                    and grant.active
                    and grant.capability not in requested
                )
                else grant
                for grant in state.grants
            ]
            allowed_slots = set(manifest.configuration.secret_refs)
            replacement = item.model_copy(
                update={
                    "manifest_history": (*item.manifest_history, item.manifest),
                    "manifest": manifest,
                    "package_verification": verification,
                    "package_ref": package_ref,
                    "lifecycle": (
                        ExtensionLifecycleState.INCOMPATIBLE
                        if incompatible_reason
                        else ExtensionLifecycleState.DISABLED
                    ),
                    "incompatible_reason": incompatible_reason,
                    "secret_bindings": {
                        key: value
                        for key, value in item.secret_bindings.items()
                        if key in allowed_slots
                    },
                    "health_status": ExtensionHealthStatus.UNKNOWN,
                    "consecutive_health_failures": 0,
                    "quarantine_reason": None,
                    "updated_at": now,
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_upgraded",
                from_version=item.manifest.version,
                to_version=manifest.version,
                lifecycle=replacement.lifecycle.value,
                migration_evidence_id=verified_migration_evidence_id,
                package_ref=package_ref,
                verifier=verification.verifier,
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def upgrade(
        self,
        installation_id: str,
        payload: ExtensionUpgradeRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        current = self.get(installation_id, actor)
        verification = self._verify_package(
            payload.manifest,
            payload.observed_digest,
            current.deployment_mode,
        )
        return self._upgrade_with_verification(
            installation_id,
            manifest=payload.manifest,
            verification=verification,
            package_ref=None,
            migration_evidence_id=payload.migration_evidence_id,
            actor=actor,
        )

    def upgrade_verified_package(
        self,
        installation_id: str,
        *,
        manifest: ExtensionManifest,
        verification: ExtensionPackageVerification,
        package_ref: str,
        migration_evidence_id: str | None,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        """Upgrade from server-observed package metadata and digest."""
        return self._upgrade_with_verification(
            installation_id,
            manifest=manifest,
            verification=verification,
            package_ref=package_ref,
            migration_evidence_id=migration_evidence_id,
            actor=actor,
        )

    def remove(
        self,
        installation_id: str,
        payload: ExtensionRemoveRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExtensionInstallation:
        self._require_admin(actor)
        if not payload.preserve_tombstone:
            raise ExtensionConflictError(
                "hard extension deletion is unsupported; canonical references require a tombstone"
            )
        updated: list[ExtensionInstallation] = []

        def apply(state):
            item = self._installation(state, installation_id, actor)
            if item.lifecycle == ExtensionLifecycleState.ENABLED:
                raise ExtensionConflictError("disable extension before removal")
            if item.lifecycle == ExtensionLifecycleState.REMOVED:
                updated.append(item)
                return state
            now = time.time()
            state.grants = [
                grant.model_copy(
                    update={
                        "revoked_at": now,
                        "revoked_by": actor.identity_id,
                        "revoke_reason": "extension removed",
                    }
                )
                if grant.installation_id == item.id and grant.active
                else grant
                for grant in state.grants
            ]
            replacement = item.model_copy(
                update={
                    "lifecycle": ExtensionLifecycleState.REMOVED,
                    "removed_at": now,
                    "removal_reason": payload.reason,
                    "updated_at": now,
                }
            )
            self._replace(state, replacement)
            self._event(
                state,
                replacement,
                actor,
                "extension_removed",
                reason=payload.reason,
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def require_runtime_capability(
        self,
        installation_id: str,
        capability: str,
        *,
        actor: AuthenticationActor,
        resource_ids: tuple[str, ...] = (),
        extension_type: ExtensionType | None = None,
    ) -> ExtensionCapabilityGrant:
        state = self.store.load()
        item = self._installation(state, installation_id, actor)
        if item.lifecycle != ExtensionLifecycleState.ENABLED:
            raise ExtensionAuthorizationError(
                f"extension is not enabled: {item.lifecycle.value}"
            )
        if extension_type is not None and extension_type not in item.manifest.types:
            raise ExtensionAuthorizationError(
                f"extension does not implement {extension_type.value}"
            )
        grant = next(
            (
                value
                for value in self._active_grants(state, item)
                if value.capability == capability
                and (
                    not value.resource_ids
                    or (
                        bool(resource_ids)
                        and set(resource_ids).issubset(set(value.resource_ids))
                    )
                )
            ),
            None,
        )
        if grant is None:
            raise ExtensionAuthorizationError(
                f"extension capability is not granted: {capability}"
            )
        return grant

    def events(self, actor: AuthenticationActor) -> list[ExtensionAuditEvent]:
        self._require_admin(actor)
        return sorted(
            [
                event
                for event in self.store.load().events
                if event.organization_id == actor.organization_id
                and event.workspace_id == actor.workspace_id
            ],
            key=lambda event: (event.occurred_at, event.id),
            reverse=True,
        )
