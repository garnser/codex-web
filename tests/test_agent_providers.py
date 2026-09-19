from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.agent_providers import (
    AgentProviderCapability,
    AgentProviderCompatibility,
    AgentProviderLifecycle,
    AgentProviderUpsert,
)
from codex_web.extensions import (
    ExtensionHealthStatus,
    ExtensionLifecycleState,
    ExtensionType,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.model_gateway import (
    ModelProviderRecord,
    ModelProviderStatus,
)
from codex_web.services.agent_providers import AgentProviderService
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Extensions:
    def __init__(self) -> None:
        self.installations = []

    def load(self):
        return SimpleNamespace(installations=self.installations)


def _actor(
    organization_id: str = "org-a",
    workspace_id: str = "workspace-a",
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id=organization_id,
        workspace_id=workspace_id,
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class AgentProviderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.store = AgentProviderStore(sqlite)
        self.models = ModelGatewayStore(sqlite)
        self.extensions = _Extensions()
        self.service = AgentProviderService(
            self.store,
            model_gateway=self.models,
            extensions=self.extensions,
            clock=lambda: 100.0,
        )
        self.actor = _actor()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _model_provider(
        self,
        provider_id: str = "openai",
        *,
        status: ModelProviderStatus = ModelProviderStatus.ACTIVE,
        organization_id: str = "org-a",
        workspace_id: str = "workspace-a",
    ) -> None:
        def apply(state):
            state.providers.append(
                ModelProviderRecord(
                    id=provider_id,
                    adapter_type="openai",
                    display_name=provider_id.title(),
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    updated_by="admin-a",
                    status=status,
                )
            )
            return state

        self.models.update(apply)

    def test_existing_model_provider_is_mapped_without_replacing_model_gateway(self) -> None:
        self._model_provider()

        providers = self.service.list(self.actor)

        self.assertEqual(len(providers), 1)
        provider = providers[0]
        self.assertEqual(provider.id, "openai")
        self.assertTrue(provider.synthesized_from_model_gateway)
        self.assertEqual(
            provider.declared_capabilities,
            (AgentProviderCapability.MODEL_INFERENCE,),
        )
        discovered = self.service.discover(
            self.actor,
            required_capabilities=(AgentProviderCapability.MODEL_INFERENCE,),
        )[0]
        self.assertTrue(discovered.eligible)
        self.assertEqual(
            discovered.effective_capabilities,
            (AgentProviderCapability.MODEL_INFERENCE,),
        )

    def test_model_and_execution_capabilities_are_independent_and_grant_constrained(self) -> None:
        self._model_provider()
        self.service.upsert(
            AgentProviderUpsert(
                id="openai",
                display_name="OpenAI + Codex",
                declared_capabilities=(
                    AgentProviderCapability.MODEL_INFERENCE,
                    AgentProviderCapability.AGENT_EXECUTION,
                    AgentProviderCapability.FILESYSTEM_EDITING,
                ),
                granted_capabilities=(
                    AgentProviderCapability.MODEL_INFERENCE,
                ),
                model_provider_ids=("openai",),
            ),
            actor=self.actor,
        )

        model = self.service.discover(
            self.actor,
            required_capabilities=(AgentProviderCapability.MODEL_INFERENCE,),
        )[0]
        execution = self.service.discover(
            self.actor,
            required_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
        )[0]

        self.assertTrue(model.eligible)
        self.assertEqual(
            model.effective_capabilities,
            (AgentProviderCapability.MODEL_INFERENCE,),
        )
        self.assertFalse(execution.eligible)
        self.assertIn(
            "missing required capabilities: agent_execution",
            execution.reasons,
        )

    def test_execution_only_provider_is_valid(self) -> None:
        self.service.upsert(
            AgentProviderUpsert(
                id="local-agent",
                display_name="Local Agent",
                declared_capabilities=(
                    AgentProviderCapability.AGENT_EXECUTION,
                    AgentProviderCapability.SHELL_TOOLS,
                ),
                granted_capabilities=(
                    AgentProviderCapability.AGENT_EXECUTION,
                    AgentProviderCapability.SHELL_TOOLS,
                ),
            ),
            actor=self.actor,
        )

        result = self.service.discover(
            self.actor,
            required_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
        )[0]
        self.assertTrue(result.eligible)
        self.assertNotIn(
            AgentProviderCapability.MODEL_INFERENCE,
            result.effective_capabilities,
        )

    def test_disabled_and_incompatible_provider_fail_closed(self) -> None:
        for provider_id, lifecycle, compatibility in (
            (
                "disabled",
                AgentProviderLifecycle.DISABLED,
                AgentProviderCompatibility.COMPATIBLE,
            ),
            (
                "incompatible",
                AgentProviderLifecycle.ACTIVE,
                AgentProviderCompatibility.INCOMPATIBLE,
            ),
        ):
            self.service.upsert(
                AgentProviderUpsert(
                    id=provider_id,
                    display_name=provider_id,
                    declared_capabilities=(
                        AgentProviderCapability.AGENT_EXECUTION,
                    ),
                    granted_capabilities=(
                        AgentProviderCapability.AGENT_EXECUTION,
                    ),
                    lifecycle=lifecycle,
                    compatibility=compatibility,
                ),
                actor=self.actor,
            )

        results = {
            item.provider.id: item
            for item in self.service.discover(self.actor)
        }
        self.assertFalse(results["disabled"].eligible)
        self.assertEqual(results["disabled"].effective_capabilities, ())
        self.assertFalse(results["incompatible"].eligible)
        self.assertEqual(results["incompatible"].effective_capabilities, ())

    def test_tenant_scoping_hides_other_tenant_providers(self) -> None:
        self._model_provider()
        self._model_provider(
            "other-model",
            organization_id="org-b",
            workspace_id="workspace-b",
        )
        self.service.upsert(
            AgentProviderUpsert(
                id="exec-a",
                display_name="Exec A",
                declared_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
                granted_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
            ),
            actor=self.actor,
        )

        self.assertEqual(
            {item.id for item in self.service.list(self.actor)},
            {"openai", "exec-a"},
        )
        self.assertEqual(
            {item.id for item in self.service.list(_actor("org-b", "workspace-b"))},
            {"other-model"},
        )

    def test_extension_lifecycle_constrains_effective_capabilities(self) -> None:
        manifest = SimpleNamespace(
            types=(ExtensionType.WORKER,),
            provenance=SimpleNamespace(digest="sha256:" + ("0" * 64)),
        )
        installation = SimpleNamespace(
            id="extension-1",
            organization_id="org-a",
            workspace_id="workspace-a",
            extension_id="example.agent",
            version="1.0.0",
            manifest=manifest,
            lifecycle=ExtensionLifecycleState.ENABLED,
            health_status=ExtensionHealthStatus.HEALTHY,
        )
        self.extensions.installations.append(installation)
        self.service.upsert(
            AgentProviderUpsert(
                id="extension-agent",
                display_name="Extension Agent",
                declared_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
                granted_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
                extension_installation_id="extension-1",
            ),
            actor=self.actor,
        )

        healthy = self.service.discover(
            self.actor,
            required_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
        )[0]
        self.assertTrue(healthy.eligible)

        installation.lifecycle = ExtensionLifecycleState.DISABLED
        disabled = self.service.discover(
            self.actor,
            required_capabilities=(AgentProviderCapability.AGENT_EXECUTION,),
        )[0]
        self.assertFalse(disabled.eligible)
        self.assertEqual(disabled.effective_capabilities, ())
        self.assertIn("extension lifecycle is disabled", disabled.reasons)


if __name__ == "__main__":
    unittest.main()
