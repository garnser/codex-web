from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.configuration import (
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationScope,
)
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.codex_execution_authentication import (
    CODEX_EXECUTION_AUTH_MODE_CONFIG,
    CODEX_WORKER_API_KEY_CONFIG,
    CodexAuthenticationPolicyError,
    CodexExecutionAuthenticationMode,
    CodexExecutionAuthenticationResolver,
    install_codex_execution_authentication_configuration,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
)
from codex_web.services.configuration import ConfigurationService
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class CodexExecutionAuthenticationResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.configuration = ConfigurationService(
            ConfigurationRegistryStore(state)
        )
        install_codex_execution_authentication_configuration(
            self.configuration
        )
        self.actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.resolver = CodexExecutionAuthenticationResolver(
            self.configuration,
            actor=self.actor,
        )
        self.codex = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def publish_mode(self, mode: str) -> None:
        draft = self.configuration.create_draft(
            ConfigurationDraftCreate(
                key=CODEX_EXECUTION_AUTH_MODE_CONFIG,
                scope_type=ConfigurationScope.PROJECT,
                scope_id="project-a",
                value=mode,
                actor=self.actor.identity_id,
            )
        )
        self.configuration.publish(
            draft.id,
            ConfigurationPublishRequest(actor=self.actor.identity_id),
        )

    def test_default_is_delegated_worker_and_never_falls_back(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            decision = self.resolver.resolve(
                project_id="project-a",
                source="web",
                runtime_binding=self.codex,
            )
        self.assertEqual(
            decision.mode,
            CodexExecutionAuthenticationMode.DELEGATED_WORKER,
        )
        self.assertEqual(
            decision.credential_config_key,
            CODEX_WORKER_ACCESS_TOKEN_CONFIG,
        )
        self.assertEqual(decision.authentication_source, "delegated_worker")
        self.assertFalse(decision.local_session)

    def test_legacy_local_switch_migrates_only_the_default_mode(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION": "1",
                "CODEX_WEB_DEPLOYMENT_MODE": "local",
            },
        ):
            decision = self.resolver.resolve(
                project_id="project-a",
                source="queued:steer:web",
                runtime_binding=self.codex,
            )
        self.assertEqual(
            decision.mode,
            CodexExecutionAuthenticationMode.TRUSTED_LOCAL_SESSION,
        )
        self.assertEqual(decision.configuration_source, "legacy_environment")
        self.assertIsNone(decision.credential_config_key)

    def test_published_delegated_mode_overrides_legacy_local_switch(self) -> None:
        self.publish_mode("delegated_worker")
        with patch.dict(
            "os.environ",
            {
                "CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION": "1",
                "CODEX_WEB_DEPLOYMENT_MODE": "local",
            },
        ):
            decision = self.resolver.resolve(
                project_id="project-a",
                source="web",
                runtime_binding=self.codex,
            )
        self.assertEqual(
            decision.mode,
            CodexExecutionAuthenticationMode.DELEGATED_WORKER,
        )
        self.assertEqual(decision.configuration_source, "published")

    def test_trusted_local_is_denied_for_unattended_source(self) -> None:
        self.publish_mode("trusted_local_session")
        with patch.dict(
            "os.environ",
            {"CODEX_WEB_DEPLOYMENT_MODE": "local"},
        ):
            with self.assertRaises(CodexAuthenticationPolicyError) as caught:
                self.resolver.resolve(
                    project_id="project-a",
                    source="slack",
                    runtime_binding=self.codex,
                )
        self.assertEqual(caught.exception.code, "authentication_method_denied")

    def test_trusted_local_is_denied_outside_local_deployment(self) -> None:
        self.publish_mode("trusted_local_session")
        with patch.dict(
            "os.environ",
            {"CODEX_WEB_DEPLOYMENT_MODE": "distributed"},
        ):
            with self.assertRaises(CodexAuthenticationPolicyError) as caught:
                self.resolver.resolve(
                    project_id="project-a",
                    source="web",
                    runtime_binding=self.codex,
                )
        self.assertEqual(caught.exception.code, "authentication_method_denied")

    def test_api_key_mode_has_distinct_credential_contract(self) -> None:
        self.publish_mode("api_key")
        decision = self.resolver.resolve(
            project_id="project-a",
            source="web",
            runtime_binding=self.codex,
        )
        self.assertEqual(
            decision.mode,
            CodexExecutionAuthenticationMode.API_KEY,
        )
        self.assertEqual(
            decision.credential_config_key,
            CODEX_WORKER_API_KEY_CONFIG,
        )
        self.assertEqual(decision.authentication_source, "api_key")
        self.assertFalse(decision.local_session)
        self.assertTrue(decision.requires_codex_runtime)

    def test_non_codex_runtime_rejects_codex_specific_mode(self) -> None:
        self.publish_mode("api_key")
        with self.assertRaises(CodexAuthenticationPolicyError) as caught:
            self.resolver.resolve(
                project_id="project-a",
                source="web",
                runtime_binding=ExecutionRuntimeBinding(
                    provider_id="anthropic",
                    runtime_id="claude-code",
                    capability_revision=1,
                ),
            )
        self.assertEqual(
            caught.exception.code,
            "authentication_method_unsupported",
        )

    def test_invalid_published_mode_fails_closed(self) -> None:
        self.publish_mode("automatic")
        with self.assertRaises(CodexAuthenticationPolicyError) as caught:
            self.resolver.resolve(
                project_id="project-a",
                source="web",
                runtime_binding=self.codex,
            )
        self.assertEqual(
            caught.exception.code,
            "authentication_method_unsupported",
        )


if __name__ == "__main__":
    unittest.main()
