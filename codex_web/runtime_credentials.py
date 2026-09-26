from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from codex_web.configuration import (
    ConfigurationContext,
)
from codex_web.configuration import (
    SecretReference as ConfigurationSecretReference,
)
from codex_web.execution_workers import (
    CodexExecutionAuthenticationMode,
    ExecutionRuntimeBinding,
)
from codex_web.services.anthropic_worker_configuration import (
    ANTHROPIC_WORKER_API_KEY_CONFIG,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_DEFAULT_AUTHENTICATION_MODE,
    CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG,
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    CODEX_WORKER_API_KEY_CONFIG,
)
from codex_web.services.mammouth_worker_configuration import (
    MAMMOUTH_WORKER_API_KEY_CONFIG,
)

DEFAULT_RUNTIME_CREDENTIAL_CONFIGS: dict[tuple[str, str], str] = {
    ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    ("anthropic", "claude-code"): ANTHROPIC_WORKER_API_KEY_CONFIG,
    ("mammouth-ai", "mammouth-cli"): MAMMOUTH_WORKER_API_KEY_CONFIG,
}


class RuntimeAuthenticationConfigurationError(ValueError):
    pass


class RuntimeAuthenticationStatus(StrEnum):
    AVAILABLE = "available"
    NOT_REQUIRED = "not_required"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNSUPPORTED = "unsupported"
    DENIED = "denied"


class RuntimeAuthenticationRequirement(BaseModel):
    """Safe resolved authentication contract; never contains credential material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    runtime_id: str
    codex_mode: CodexExecutionAuthenticationMode | None = None
    source: str
    permitted: bool = True
    credential_config_key: str | None = None
    credential_environment_variable: str | None = None
    local_session: bool = False

    @property
    def credential_required(self) -> bool:
        return self.credential_config_key is not None

    def public(self) -> dict[str, object]:
        return {
            "provider": self.provider_id,
            "runtime": self.runtime_id,
            "authentication_mode": (
                self.codex_mode.value if self.codex_mode is not None else None
            ),
            "authentication_source": self.source,
            "authentication_permitted": self.permitted,
            "credential_required": self.credential_required,
            "credential_configuration_key": self.credential_config_key,
            "credential_environment_variable": self.credential_environment_variable,
            "local_session": self.local_session,
        }


class RuntimeAuthenticationPreflight(BaseModel):
    """Safe authentication availability result shared by readiness and execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement: RuntimeAuthenticationRequirement
    status: RuntimeAuthenticationStatus
    code: str
    message: str
    remediation: str | None = None
    remediation_route: str | None = None
    secret_reference_id: str | None = None

    @property
    def available(self) -> bool:
        return self.status in {
            RuntimeAuthenticationStatus.AVAILABLE,
            RuntimeAuthenticationStatus.NOT_REQUIRED,
        }

    def public(self) -> dict[str, object]:
        return {
            **self.requirement.public(),
            "authentication_status": self.status.value,
            "authentication_code": self.code,
            "authentication_available": self.available,
            "secret_reference_id": self.secret_reference_id,
            "remediation": self.remediation,
            "remediation_route": self.remediation_route,
        }


def runtime_credential_config_key(
    runtime_binding: ExecutionRuntimeBinding | None,
    mapping: Mapping[tuple[str, str], str] | None = None,
) -> str | None:
    if runtime_binding is None:
        return None
    table = DEFAULT_RUNTIME_CREDENTIAL_CONFIGS if mapping is None else mapping
    return table.get((runtime_binding.provider_id, runtime_binding.runtime_id))


def _configuration_supports(
    configuration: Any,
    key: str,
) -> bool:
    specs = getattr(configuration, "specs", None)
    if specs is None:
        return False
    try:
        specs.get(key)
    except Exception:
        return False
    return True


def runtime_authentication_requirement(
    runtime_binding: ExecutionRuntimeBinding | None,
    *,
    configuration: Any | None = None,
    context: ConfigurationContext | None = None,
    credential_mapping: Mapping[tuple[str, str], str] | None = None,
    permitted_codex_modes: Collection[CodexExecutionAuthenticationMode] | None = None,
) -> RuntimeAuthenticationRequirement | None:
    """Resolve one deterministic authentication contract before credential lookup.

    Existing callers/test doubles that do not expose the new configuration spec use
    delegated-worker-token as the compatibility default. Once the spec is installed,
    its effective configured value is authoritative and no credential fallback occurs.
    """

    if runtime_binding is None:
        return None

    provider_id = runtime_binding.provider_id
    runtime_id = runtime_binding.runtime_id
    if (provider_id, runtime_id) != ("openai", "codex"):
        key = runtime_credential_config_key(runtime_binding, credential_mapping)
        return RuntimeAuthenticationRequirement(
            provider_id=provider_id,
            runtime_id=runtime_id,
            source="runtime_credential_mapping",
            credential_config_key=key,
        )

    mode = CodexExecutionAuthenticationMode(CODEX_DEFAULT_AUTHENTICATION_MODE)
    source = "compatibility_default"
    if runtime_binding.authentication_mode:
        try:
            mode = CodexExecutionAuthenticationMode(runtime_binding.authentication_mode)
        except ValueError as exc:
            raise RuntimeAuthenticationConfigurationError(
                "persisted Codex execution authentication mode is invalid"
            ) from exc
        source = "execution_binding"
    elif (
        configuration is not None
        and _configuration_supports(
            configuration,
            CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG,
        )
    ):
        try:
            effective = configuration.resolve(
                CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG,
                context or ConfigurationContext(),
            )
            mode = CodexExecutionAuthenticationMode(str(effective.value))
        except Exception as exc:
            raise RuntimeAuthenticationConfigurationError(
                "Codex execution authentication mode is missing or invalid"
            ) from exc
        source = getattr(effective, "source", None) or "configuration"

    allowed = (
        set(CodexExecutionAuthenticationMode)
        if permitted_codex_modes is None
        else {CodexExecutionAuthenticationMode(value) for value in permitted_codex_modes}
    )
    permitted = mode in allowed

    if mode == CodexExecutionAuthenticationMode.TRUSTED_LOCAL_SESSION:
        return RuntimeAuthenticationRequirement(
            provider_id=provider_id,
            runtime_id=runtime_id,
            codex_mode=mode,
            source=source,
            permitted=permitted,
            local_session=True,
        )
    if mode == CodexExecutionAuthenticationMode.API_KEY:
        return RuntimeAuthenticationRequirement(
            provider_id=provider_id,
            runtime_id=runtime_id,
            codex_mode=mode,
            source=source,
            permitted=permitted,
            credential_config_key=CODEX_WORKER_API_KEY_CONFIG,
            credential_environment_variable="OPENAI_API_KEY",
        )
    return RuntimeAuthenticationRequirement(
        provider_id=provider_id,
        runtime_id=runtime_id,
        codex_mode=mode,
        source=source,
        permitted=permitted,
        credential_config_key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
        credential_environment_variable="CODEX_ACCESS_TOKEN",
    )


def runtime_authentication_preflight(
    runtime_binding: ExecutionRuntimeBinding | None,
    *,
    configuration: Any | None = None,
    context: ConfigurationContext | None = None,
    credential_mapping: Mapping[tuple[str, str], str] | None = None,
    permitted_codex_modes: Collection[CodexExecutionAuthenticationMode] | None = None,
    secret_metadata: Callable[[str], Any] | None = None,
    local_session_probe: Callable[[], bool] | None = None,
) -> RuntimeAuthenticationPreflight | None:
    """Resolve authentication selection and validate safe availability metadata."""

    requirement = runtime_authentication_requirement(
        runtime_binding,
        configuration=configuration,
        context=context,
        credential_mapping=credential_mapping,
        permitted_codex_modes=permitted_codex_modes,
    )
    if requirement is None:
        return None

    target = f"{requirement.provider_id}/{requirement.runtime_id}"
    if not requirement.permitted:
        mode = (
            requirement.codex_mode.value
            if requirement.codex_mode is not None
            else "configured"
        )
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.DENIED,
            code="authentication_mode_denied",
            message=f"authentication mode {mode} is denied by execution policy",
            remediation="Select an authentication mode permitted by execution policy.",
            remediation_route="/api/configuration",
        )

    if requirement.local_session:
        if local_session_probe is None:
            return RuntimeAuthenticationPreflight(
                requirement=requirement,
                status=RuntimeAuthenticationStatus.UNSUPPORTED,
                code="authentication_mode_unsupported",
                message=(
                    "trusted local Codex session authentication is not supported "
                    "by this execution path"
                ),
                remediation=(
                    "Use a supported authentication mode or enable a trusted "
                    "local-session execution path."
                ),
                remediation_route="/api/configuration",
            )
        try:
            available = bool(local_session_probe())
        except Exception:
            available = False
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=(
                RuntimeAuthenticationStatus.AVAILABLE
                if available
                else RuntimeAuthenticationStatus.UNAVAILABLE
            ),
            code=(
                "local_session_available"
                if available
                else "local_session_unavailable"
            ),
            message=(
                "Trusted local Codex session authentication is available."
                if available
                else "Trusted local Codex session authentication is unavailable."
            ),
            remediation=(
                None
                if available
                else "Authenticate the local Codex CLI session and retry."
            ),
            remediation_route=None if available else "/api/configuration",
        )

    if not requirement.credential_required:
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.NOT_REQUIRED,
            code="credential_reference_not_required",
            message="The effective runtime does not require a configured credential reference.",
        )

    config_key = requirement.credential_config_key
    if configuration is None or config_key is None:
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.MISSING,
            code="credential_reference_missing",
            message=f"Required runtime credential reference for {target} is not configured.",
            remediation="Configure the credential required by the selected authentication mode.",
            remediation_route="/api/configuration",
        )

    try:
        effective = configuration.resolve(
            config_key,
            context or ConfigurationContext(),
        )
        configured = ConfigurationSecretReference.model_validate(effective.value)
    except Exception:
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.MISSING,
            code="credential_reference_missing",
            message=(
                "worker credential reference configuration is unavailable "
                f"for {target}: {config_key} is missing or malformed"
            ),
            remediation=(
                "Configure an authorized canonical SecretReference for the "
                "selected authentication mode."
            ),
            remediation_route="/api/configuration",
        )

    secret_id = configured.secret_id
    if secret_metadata is None:
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.AVAILABLE,
            code="credential_reference_ready",
            message=f"Runtime credential reference {config_key} is configured.",
            secret_reference_id=secret_id,
        )

    try:
        metadata = secret_metadata(secret_id)
        raw_status = metadata.status()
        status = getattr(raw_status, "value", str(raw_status))
    except Exception:
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.UNAVAILABLE,
            code="authentication_unavailable",
            message=(
                f"Runtime credential reference {config_key} is configured but "
                "unavailable or unauthorized."
            ),
            remediation="Repair or replace the configured credential reference.",
            remediation_route="/api/secrets",
            secret_reference_id=secret_id,
        )

    if status == "expired":
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.EXPIRED,
            code="authentication_expired",
            message=f"Runtime authentication for {target} is expired.",
            remediation="Rotate or replace the expired credential reference.",
            remediation_route="/api/secrets",
            secret_reference_id=secret_id,
        )
    if status == "revoked":
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.REVOKED,
            code="authentication_revoked",
            message=f"Runtime authentication for {target} is revoked.",
            remediation="Replace the revoked credential reference before retrying.",
            remediation_route="/api/secrets",
            secret_reference_id=secret_id,
        )
    if status != "active":
        return RuntimeAuthenticationPreflight(
            requirement=requirement,
            status=RuntimeAuthenticationStatus.UNAVAILABLE,
            code="authentication_unavailable",
            message=f"Runtime authentication for {target} is unavailable.",
            remediation="Repair or replace the configured credential reference.",
            remediation_route="/api/secrets",
            secret_reference_id=secret_id,
        )

    return RuntimeAuthenticationPreflight(
        requirement=requirement,
        status=RuntimeAuthenticationStatus.AVAILABLE,
        code="credential_reference_ready",
        message=f"Runtime credential reference {config_key} is active and authorized.",
        secret_reference_id=secret_id,
    )
