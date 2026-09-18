from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    DefinitionScope,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.input_plugin_definitions import (
    INPUT_PIPELINE_DEFINITION_ID,
    INPUT_PIPELINE_DEFINITION_KIND,
    INPUT_PIPELINE_SCHEMA_VERSION,
    InputPipelineDefinition,
)
from codex_web.input_plugins import (
    InputEnvelope,
    InputMessage,
    InputPatch,
    InputPhase,
)
from codex_web.model_gateway import ModelInvocationRequest, ModelMessage
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.input_plugin_definitions import (
    InputPluginCatalog,
    InputPluginDefinitionError,
    default_input_plugin_catalog,
    install_input_plugin_definitions,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _SettingsPlugin:
    id = "builtin.settings-test"
    version = "1.0.0"
    transport = "builtin"

    async def transform(self, envelope, context):
        prefix = str(context.settings.get("prefix") or "")
        return InputPatch(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=InputPhase.COMPOSE,
            changes={
                "system_prompt": f"{prefix}{envelope.system_prompt}",
            },
        )


class InputPluginDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(
            DefinitionRegistryStore(self.state)
        )
        self.catalog = default_input_plugin_catalog()
        self.catalog.register(_SettingsPlugin())
        self.service = install_input_plugin_definitions(
            self.registry,
            catalog=self.catalog,
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _request(self, **overrides) -> ModelInvocationRequest:
        payload = {
            "model_class": "primary-coding",
            "messages": (ModelMessage(role="user", content="Build it"),),
            "system_prompt": "  system  ",
            "purpose": "coding",
        }
        payload.update(overrides)
        return ModelInvocationRequest(**payload)

    def _envelope(self, request: ModelInvocationRequest) -> InputEnvelope:
        return InputEnvelope(
            request_id="request-1",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            actor_id=self.actor.identity_id,
            model_class=request.model_class,
            messages=tuple(
                InputMessage(role=item.role, content=item.content)
                for item in request.messages
            ),
            system_prompt=request.system_prompt,
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            required_capabilities=request.required_capabilities,
            required_residency_tags=request.required_residency_tags,
            required_compliance_tags=request.required_compliance_tags,
            preferred_provider_ids=request.preferred_provider_ids,
            max_output_tokens=request.max_output_tokens,
            timeout_seconds=request.timeout_seconds,
            max_cost_usd=request.max_cost_usd,
            allow_fallback=request.allow_fallback,
            work_item_ref=request.work_item_ref,
            goal_id=request.goal_id,
            decision_id=request.decision_id,
            execution_id=request.execution_id,
            purpose=request.purpose,
        )

    def _publish_workspace(self, payload: dict):
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=INPUT_PIPELINE_DEFINITION_ID,
                kind=INPUT_PIPELINE_DEFINITION_KIND,
                definition_schema_version=INPUT_PIPELINE_SCHEMA_VERSION,
                scope_type=DefinitionScope.WORKSPACE,
                scope_id=self.actor.workspace_id,
                payload=payload,
                actor=self.actor.identity_id,
                reason="test pipeline",
            )
        )
        return self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=self.actor.identity_id,
                reason="publish test pipeline",
            ),
        )

    async def test_bootstrap_registers_schema_and_preserves_empty_pipeline(self) -> None:
        record = self.service.record(
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
        )
        definition = self.service.definition(
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
        )

        self.assertEqual(record.definition_id, INPUT_PIPELINE_DEFINITION_ID)
        self.assertEqual(record.kind, INPUT_PIPELINE_DEFINITION_KIND)
        self.assertEqual(definition.registrations, ())
        self.assertIn(
            {
                "kind": INPUT_PIPELINE_DEFINITION_KIND,
                "schema_version": INPUT_PIPELINE_SCHEMA_VERSION,
            },
            self.registry.schemas.metadata(),
        )

        request = self._request()
        result = await self.service.pipeline_for(
            request,
            self.actor,
        ).execute(self._envelope(request))
        self.assertEqual(result.provenance, ())
        self.assertEqual(result.envelope.system_prompt, "  system  ")

    async def test_workspace_definition_selects_exact_builtin_and_pins_revision(self) -> None:
        published = self._publish_workspace(
            {
                "registrations": [
                    {
                        "plugin_id": "builtin.normalize-whitespace",
                        "plugin_version": "1.0.0",
                        "phase": "normalize",
                        "order": 10,
                    }
                ]
            }
        )
        request = self._request()
        result = await self.service.pipeline_for(
            request,
            self.actor,
        ).execute(self._envelope(request))

        self.assertEqual(result.envelope.system_prompt, "system")
        self.assertEqual(len(result.provenance), 1)
        provenance = result.provenance[0]
        self.assertEqual(
            provenance.definition_ref.record_id,
            published.record_id,
        )
        self.assertEqual(
            provenance.definition_ref.revision,
            published.revision,
        )
        self.assertEqual(provenance.plugin_version, "1.0.0")

    async def test_conditions_and_non_secret_settings_are_deterministic(self) -> None:
        self._publish_workspace(
            {
                "registrations": [
                    {
                        "plugin_id": "builtin.settings-test",
                        "plugin_version": "1.0.0",
                        "phase": "compose",
                        "conditions": {
                            "purposes": ["coding"],
                            "model_classes": ["primary-coding"],
                        },
                        "settings": {"prefix": "PREFIX: "},
                    }
                ]
            }
        )

        coding = self._request(purpose="coding")
        applied = await self.service.pipeline_for(
            coding,
            self.actor,
        ).execute(self._envelope(coding))
        self.assertEqual(
            applied.envelope.system_prompt,
            "PREFIX:   system  ",
        )

        other = self._request(purpose="executive-advice")
        skipped = await self.service.pipeline_for(
            other,
            self.actor,
        ).execute(self._envelope(other))
        self.assertEqual(skipped.envelope.system_prompt, "  system  ")
        self.assertEqual(skipped.provenance, ())

    async def test_enabled_unknown_plugin_fails_closed_at_resolution(self) -> None:
        self._publish_workspace(
            {
                "registrations": [
                    {
                        "plugin_id": "external.not-installed",
                        "plugin_version": "9.9.9",
                        "phase": "compose",
                    }
                ]
            }
        )

        with self.assertRaisesRegex(
            InputPluginDefinitionError,
            "implementation unavailable",
        ):
            self.service.pipeline_for(self._request(), self.actor)

    async def test_definition_rejects_duplicates_and_secret_like_settings(self) -> None:
        duplicate = {
            "registrations": [
                {
                    "plugin_id": "builtin.normalize-whitespace",
                    "plugin_version": "1.0.0",
                    "phase": "normalize",
                },
                {
                    "plugin_id": "builtin.normalize-whitespace",
                    "plugin_version": "1.0.0",
                    "phase": "normalize",
                },
            ]
        }
        with self.assertRaisesRegex(ValidationError, "duplicate input plugin"):
            InputPipelineDefinition.model_validate(duplicate)

        with self.assertRaisesRegex(
            ValidationError,
            "credential/secret fields",
        ):
            InputPipelineDefinition.model_validate(
                {
                    "registrations": [
                        {
                            "plugin_id": "builtin.settings-test",
                            "plugin_version": "1.0.0",
                            "phase": "compose",
                            "settings": {
                                "api_key": "raw-secret-must-not-be-definition-data"
                            },
                        }
                    ]
                }
            )

    async def test_catalog_requires_exact_version(self) -> None:
        catalog = InputPluginCatalog()
        catalog.register(_SettingsPlugin())

        self.assertEqual(
            catalog.get("builtin.settings-test", "1.0.0").version,
            "1.0.0",
        )
        with self.assertRaises(InputPluginDefinitionError):
            catalog.get("builtin.settings-test", "2.0.0")


if __name__ == "__main__":
    unittest.main()
