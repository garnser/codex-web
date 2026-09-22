from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from codex_web.configuration import ConfigurationContext
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.services.anthropic_worker_configuration import (
    ANTHROPIC_WORKER_API_KEY_CONFIG,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_DEFAULT_AUTHENTICATION_MODE,
    CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG,
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    CODEX_WORKER_API_KEY_CONFIG,
)


DEFAULT_RUNTIME_CREDENTIAL_CONFIGS: dict[tuple[str, str], str] = {
    ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    ("anthropic", "claude-code"): ANTHROPIC_WORKER_API_KEY_CONFIG,
}


class CodexExecutionAuthenticationMode(StrEnum):
    TRUSTED_LOCAL_SESSION = "trusted_local_session"
    DELEGATED_WORKER_TOKEN = "delegated_worker_token"
    API_KEY = "api_key"


class RuntimeAuthenticationConfigurationError(ValueError):
    pass


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
    if (
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
