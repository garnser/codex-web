from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.configuration import ConfigurationContext
from codex_web.execution_workers import (
    CodexExecutionAuthenticationMode,
    ExecutionRuntimeBinding,
)
from codex_web.runtime_credentials import (
    RuntimeAuthenticationStatus,
    runtime_authentication_preflight,
    runtime_authentication_requirement,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG,
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    CODEX_WORKER_API_KEY_CONFIG,
)


class _Specs:
    def get(self, key: str) -> object:
        if key != CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG:
            raise KeyError(key)
        return object()


class _Configuration:
    specs = _Specs()

    def __init__(self, value: str) -> None:
        self.value = value

    def resolve(self, key: str, context: ConfigurationContext) -> object:
        if key != CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG:
            raise KeyError(key)
        return SimpleNamespace(value=self.value, source="project")


class _CredentialConfiguration:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[str] = []

    def resolve(self, key: str, context: ConfigurationContext) -> object:
        del context
        self.calls.append(key)
        if not self.available:
            raise LookupError("configuration missing")
        return SimpleNamespace(
            value={"kind": "secret", "secret_id": "secret-auth"},
            source="project",
        )


class _SecretMetadata:
    def __init__(self, status: str) -> None:
        self._status = status

    def status(self) -> object:
        return SimpleNamespace(value=self._status)


def _codex_binding(authentication_mode: str | None = None) -> ExecutionRuntimeBinding:
    return ExecutionRuntimeBinding(
        provider_id="openai",
        runtime_id="codex",
        capability_revision=1,
        authentication_mode=authentication_mode,
    )


class RuntimeAuthenticationRequirementTests(unittest.TestCase):
    def test_existing_installations_default_to_delegated_worker_token(self) -> None:
        requirement = runtime_authentication_requirement(_codex_binding())

        self.assertIsNotNone(requirement)
        assert requirement is not None
        self.assertEqual(
            requirement.codex_mode,
            CodexExecutionAuthenticationMode.DELEGATED_WORKER_TOKEN,
        )
        self.assertEqual(requirement.source, "compatibility_default")
        self.assertEqual(
            requirement.credential_config_key,
            CODEX_WORKER_ACCESS_TOKEN_CONFIG,
        )
        self.assertEqual(
            requirement.credential_environment_variable,
            "CODEX_ACCESS_TOKEN",
        )
        self.assertFalse(requirement.local_session)

    def test_explicit_binding_mode_wins_over_configuration(self) -> None:
        requirement = runtime_authentication_requirement(
            _codex_binding("trusted_local_session"),
            configuration=_Configuration("api_key"),
            context=ConfigurationContext(project_id="project-1"),
        )

        self.assertIsNotNone(requirement)
        assert requirement is not None
        self.assertEqual(
            requirement.codex_mode,
            CodexExecutionAuthenticationMode.TRUSTED_LOCAL_SESSION,
        )
        self.assertEqual(requirement.source, "execution_binding")
        self.assertFalse(requirement.credential_required)
        self.assertTrue(requirement.local_session)

    def test_api_key_mode_has_distinct_credential_contract(self) -> None:
        requirement = runtime_authentication_requirement(
            _codex_binding(),
            configuration=_Configuration("api_key"),
            context=ConfigurationContext(project_id="project-1"),
        )

        self.assertIsNotNone(requirement)
        assert requirement is not None
        self.assertEqual(
            requirement.codex_mode,
            CodexExecutionAuthenticationMode.API_KEY,
        )
        self.assertEqual(requirement.credential_config_key, CODEX_WORKER_API_KEY_CONFIG)
        self.assertEqual(
            requirement.credential_environment_variable,
            "OPENAI_API_KEY",
        )
        self.assertFalse(requirement.local_session)

    def test_policy_denial_does_not_fall_back_to_delegated_authentication(self) -> None:
        requirement = runtime_authentication_requirement(
            _codex_binding("api_key"),
            permitted_codex_modes=(
                CodexExecutionAuthenticationMode.DELEGATED_WORKER_TOKEN,
            ),
        )

        self.assertIsNotNone(requirement)
        assert requirement is not None
        self.assertEqual(
            requirement.codex_mode,
            CodexExecutionAuthenticationMode.API_KEY,
        )
        self.assertFalse(requirement.permitted)
        self.assertEqual(requirement.credential_config_key, CODEX_WORKER_API_KEY_CONFIG)

    def test_invalid_explicit_mode_fails_closed_at_runtime_binding_boundary(self) -> None:
        with self.assertRaises(ValueError):
            _codex_binding("implicit-fallback")

    def test_preflight_reports_trusted_local_session_available(self) -> None:
        result = runtime_authentication_preflight(
            _codex_binding("trusted_local_session"),
            local_session_probe=lambda: True,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.available)
        self.assertEqual(result.status, RuntimeAuthenticationStatus.AVAILABLE)
        self.assertEqual(result.code, "local_session_available")
        self.assertIsNone(result.secret_reference_id)

    def test_preflight_distinguishes_unsupported_and_unavailable_local_session(self) -> None:
        unsupported = runtime_authentication_preflight(
            _codex_binding("trusted_local_session")
        )
        unavailable = runtime_authentication_preflight(
            _codex_binding("trusted_local_session"),
            local_session_probe=lambda: False,
        )

        self.assertEqual(
            unsupported.status,
            RuntimeAuthenticationStatus.UNSUPPORTED,
        )
        self.assertEqual(unsupported.code, "authentication_mode_unsupported")
        self.assertEqual(
            unavailable.status,
            RuntimeAuthenticationStatus.UNAVAILABLE,
        )
        self.assertEqual(unavailable.code, "local_session_unavailable")

    def test_preflight_missing_delegated_reference_is_typed(self) -> None:
        result = runtime_authentication_preflight(
            _codex_binding("delegated_worker_token"),
            configuration=_CredentialConfiguration(available=False),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.available)
        self.assertEqual(result.status, RuntimeAuthenticationStatus.MISSING)
        self.assertEqual(result.code, "credential_reference_missing")
        self.assertEqual(
            result.requirement.credential_config_key,
            CODEX_WORKER_ACCESS_TOKEN_CONFIG,
        )

    def test_preflight_api_key_uses_distinct_configuration(self) -> None:
        configuration = _CredentialConfiguration()
        result = runtime_authentication_preflight(
            _codex_binding("api_key"),
            configuration=configuration,
            secret_metadata=lambda _secret_id: _SecretMetadata("active"),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.available)
        self.assertEqual(result.code, "credential_reference_ready")
        self.assertEqual(configuration.calls, [CODEX_WORKER_API_KEY_CONFIG])
        self.assertEqual(
            result.requirement.credential_config_key,
            CODEX_WORKER_API_KEY_CONFIG,
        )

    def test_preflight_distinguishes_expired_and_revoked_authentication(self) -> None:
        for status, expected in (
            ("expired", "authentication_expired"),
            ("revoked", "authentication_revoked"),
        ):
            with self.subTest(status=status):
                result = runtime_authentication_preflight(
                    _codex_binding("delegated_worker_token"),
                    configuration=_CredentialConfiguration(),
                    secret_metadata=lambda _secret_id, value=status: _SecretMetadata(value),
                )
                self.assertIsNotNone(result)
                assert result is not None
                self.assertFalse(result.available)
                self.assertEqual(result.code, expected)
                self.assertEqual(result.status.value, status)

    def test_preflight_policy_denial_is_not_reported_as_missing(self) -> None:
        result = runtime_authentication_preflight(
            _codex_binding("api_key"),
            permitted_codex_modes=(
                CodexExecutionAuthenticationMode.DELEGATED_WORKER_TOKEN,
            ),
            configuration=_CredentialConfiguration(available=False),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, RuntimeAuthenticationStatus.DENIED)
        self.assertEqual(result.code, "authentication_mode_denied")

    def test_non_codex_runtime_keeps_existing_mapping_contract(self) -> None:
        binding = ExecutionRuntimeBinding(
            provider_id="anthropic",
            runtime_id="claude-code",
            capability_revision=1,
        )
        requirement = runtime_authentication_requirement(
            binding,
            credential_mapping={
                ("anthropic", "claude-code"): "anthropic.worker.api_key_secret"
            },
        )

        self.assertIsNotNone(requirement)
        assert requirement is not None
        self.assertIsNone(requirement.codex_mode)
        self.assertEqual(requirement.source, "runtime_credential_mapping")
        self.assertEqual(
            requirement.credential_config_key,
            "anthropic.worker.api_key_secret",
        )

    def test_mammouth_runtime_uses_reference_only_worker_credential(self) -> None:
        binding = ExecutionRuntimeBinding(
            provider_id="mammouth-ai",
            runtime_id="mammouth-cli",
            capability_revision=1,
        )

        requirement = runtime_authentication_requirement(binding)

        self.assertIsNotNone(requirement)
        assert requirement is not None
        self.assertEqual(requirement.source, "runtime_credential_mapping")
        self.assertEqual(
            requirement.credential_config_key,
            "mammouth.worker.api_key_secret",
        )


if __name__ == "__main__":
    unittest.main()
