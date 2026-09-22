from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.configuration import ConfigurationContext
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime_credentials import (
    CodexExecutionAuthenticationMode,
    RuntimeAuthenticationConfigurationError,
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

    def test_invalid_explicit_mode_fails_closed(self) -> None:
        with self.assertRaises(RuntimeAuthenticationConfigurationError):
            runtime_authentication_requirement(_codex_binding("implicit-fallback"))

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


if __name__ == "__main__":
    unittest.main()
