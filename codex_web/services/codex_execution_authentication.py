from __future__ import annotations

import os
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.identity import AuthenticationActor
from codex_web.services.codex_worker_configuration import (
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
)
from codex_web.services.configuration import (
    ConfigurationError,
    ConfigurationNotFoundError,
    ConfigurationService,
)


CODEX_EXECUTION_AUTH_MODE_CONFIG = "codex.execution.authentication_mode"
CODEX_WORKER_API_KEY_CONFIG = "codex.worker.api_key_secret"


class CodexExecutionAuthenticationMode(StrEnum):
    DELEGATED_WORKER = "delegated_worker"
    TRUSTED_LOCAL_SESSION = "trusted_local_session"
    API_KEY = "api_key"


class CodexAuthenticationPolicyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "authentication_mode_unavailable",
    ) -> None:
        super().__init__(message)
        self.code = code


class CodexAuthenticationDecision(BaseModel):
    """Credential-free, auditable decision for one Codex execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: CodexExecutionAuthenticationMode
    authentication_source: str
    credential_config_key: str | None = None
    configuration_source: str
    requires_codex_runtime: bool = False
    local_session: bool = False


def install_codex_execution_authentication_configuration(
    service: ConfigurationService,
) -> tuple[ConfigurationSpec, ConfigurationSpec]:
    mode = service.register_spec(
        ConfigurationSpec(
            key=CODEX_EXECUTION_AUTH_MODE_CONFIG,
            value_kind=ConfigurationValueKind.STRING,
            required=True,
            default=CodexExecutionAuthenticationMode.DELEGATED_WORKER.value,
            allowed_scopes=[
                ConfigurationScope.DEPLOYMENT,
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
                ConfigurationScope.PROJECT,
            ],
            description=(
                "Explicit Codex execution authentication mode. Supported values are "
                "delegated_worker, trusted_local_session, and api_key. Modes never "
                "fall back to one another when unavailable."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
    api_key = service.register_spec(
        ConfigurationSpec(
            key=CODEX_WORKER_API_KEY_CONFIG,
            value_kind=ConfigurationValueKind.SECRET_REF,
            required=False,
            default=None,
            allowed_scopes=[
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
                ConfigurationScope.PROJECT,
            ],
            description=(
                "Secret reference used when Codex execution authentication mode is "
                "api_key. Configuration selects only a reference and does not grant "
                "secret-use authority."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
    return mode, api_key


class CodexExecutionAuthenticationResolver:
    """Resolve one explicit Codex authentication mode before credential lookup."""

    def __init__(
        self,
        configuration: ConfigurationService,
        *,
        actor: AuthenticationActor,
    ) -> None:
        self.configuration = configuration
        self.actor = actor

    @staticmethod
    def _original_source(source: str) -> str:
        original = str(source or "").strip().casefold()
        while True:
            prefix, separator, remainder = original.partition(":")
            if separator and prefix in {"queued", "steer"}:
                original = remainder
                continue
            return original

    @staticmethod
    def _legacy_local_enabled() -> bool:
        return (
            os.environ.get("CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION", "")
            .strip()
            .casefold()
            in {"1", "true", "yes", "on"}
        )

    @staticmethod
    def _deployment_mode() -> str:
        return (
            os.environ.get("CODEX_WEB_DEPLOYMENT_MODE", "local")
            .strip()
            .casefold()
        )

    @staticmethod
    def _validate_codex_runtime(
        runtime_binding: ExecutionRuntimeBinding | None,
    ) -> None:
        if runtime_binding is None:
            return
        if (
            runtime_binding.provider_id != "openai"
            or runtime_binding.runtime_id != "codex"
        ):
            raise CodexAuthenticationPolicyError(
                "selected authentication mode is only supported for openai/codex",
                code="authentication_method_unsupported",
            )

    def resolve(
        self,
        *,
        project_id: str,
        source: str,
        runtime_binding: ExecutionRuntimeBinding | None = None,
    ) -> CodexAuthenticationDecision:
        try:
            effective = self.configuration.resolve(
                CODEX_EXECUTION_AUTH_MODE_CONFIG,
                ConfigurationContext(
                    organization_id=self.actor.organization_id,
                    workspace_id=self.actor.workspace_id,
                    project_id=project_id,
                ),
            )
            raw_mode = str(effective.value or "").strip().casefold()
            configuration_source = effective.source
        except (ConfigurationError, ConfigurationNotFoundError) as exc:
            raise CodexAuthenticationPolicyError(
                "Codex authentication mode configuration is unavailable",
                code="authentication_mode_missing",
            ) from exc

        # Migration bridge for installations that explicitly enabled the
        # trusted-local compatibility switch before the typed mode existed.
        # A published mode always wins; this bridge applies only to the default.
        if (
            configuration_source == "default"
            and raw_mode == CodexExecutionAuthenticationMode.DELEGATED_WORKER.value
            and self._legacy_local_enabled()
        ):
            raw_mode = CodexExecutionAuthenticationMode.TRUSTED_LOCAL_SESSION.value
            configuration_source = "legacy_environment"

        try:
            mode = CodexExecutionAuthenticationMode(raw_mode)
        except ValueError as exc:
            raise CodexAuthenticationPolicyError(
                f"unsupported Codex authentication mode: {raw_mode or '<empty>'}",
                code="authentication_method_unsupported",
            ) from exc

        if mode == CodexExecutionAuthenticationMode.TRUSTED_LOCAL_SESSION:
            self._validate_codex_runtime(runtime_binding)
            if self._deployment_mode() != "local":
                raise CodexAuthenticationPolicyError(
                    "trusted local Codex session authentication requires local deployment",
                    code="authentication_method_denied",
                )
            if self._original_source(source) != "web":
                raise CodexAuthenticationPolicyError(
                    "trusted local Codex session authentication is limited to interactive web turns",
                    code="authentication_method_denied",
                )
            return CodexAuthenticationDecision(
                mode=mode,
                authentication_source="local_codex_session",
                credential_config_key=None,
                configuration_source=configuration_source,
                requires_codex_runtime=True,
                local_session=True,
            )

        if mode == CodexExecutionAuthenticationMode.API_KEY:
            self._validate_codex_runtime(runtime_binding)
            return CodexAuthenticationDecision(
                mode=mode,
                authentication_source="api_key",
                credential_config_key=CODEX_WORKER_API_KEY_CONFIG,
                configuration_source=configuration_source,
                requires_codex_runtime=True,
                local_session=False,
            )

        return CodexAuthenticationDecision(
            mode=mode,
            authentication_source="delegated_worker",
            credential_config_key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            configuration_source=configuration_source,
            requires_codex_runtime=False,
            local_session=False,
        )
