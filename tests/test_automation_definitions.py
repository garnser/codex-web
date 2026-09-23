from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
    AutomationLifecycle,
    AutomationTriggerType,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.automation_definitions import (
    AutomationEventTriggerService,
    install_automation_definitions,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AutomationDefinitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(store))
        self.automations = install_automation_definitions(self.registry)
        self.event_bus = CanonicalEventBus(CanonicalEventStore(store))
        self.events = CanonicalEventIngestionService(self.event_bus)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(self) -> dict[str, object]:
        return {
            "name": "Nightly repository health",
            "description": "Run canonical repository checks.",
            "lifecycle": "enabled",
            "trigger": {
                "type": "recurring_schedule",
                "cron": "0 2 * * *",
                "timezone": "Europe/Stockholm",
            },
            "target": {"kind": "agent_profile", "id": "agent-james"},
            "instructions": "Inspect repository health and create work only for actionable failures.",
            "budget": {
                "max_input_tokens": 20000,
                "max_output_tokens": 4000,
                "max_cost_usd": 2.5,
                "max_duration_seconds": 900,
                "max_concurrency": 1,
            },
            "retry": {"max_attempts": 3, "backoff_seconds": 60},
            "dedupe_key_template": "repo-health:{project}:{occurrence}",
            "work_item_policy": "reuse_or_create",
            "failure_attention": True,
        }

    def test_definition_registry_versions_and_resolves_automation(self) -> None:
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id="nightly-repository-health",
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=self._payload(),
                actor="operator",
            )
        )
        published = self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

        automation, reference = self.automations.resolve(
            "nightly-repository-health",
        )

        self.assertEqual(automation.lifecycle, AutomationLifecycle.ENABLED)
        self.assertEqual(
            automation.trigger.type,
            AutomationTriggerType.RECURRING_SCHEDULE,
        )
        self.assertEqual(reference.record_id, published.record_id)
        self.assertEqual(reference.revision, published.revision)
        self.assertEqual(reference.checksum, published.checksum)

    def test_trigger_validation_fails_before_scheduler_state_exists(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "recurring_schedule",
            "cron": "0 2 * * *",
        }

        with self.assertRaises(ValidationError):
            self.registry.create_draft(
                DefinitionDraftCreate(
                    definition_id="invalid-schedule",
                    kind=AUTOMATION_KIND,
                    definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                    payload=payload,
                    actor="operator",
                )
            )

        self.assertEqual(self.registry.list_records(kind=AUTOMATION_KIND), [])

    def _publish_payload(self, definition_id: str, payload: dict[str, object]) -> None:
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=definition_id,
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=payload,
                actor="operator",
            )
        )
        self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

    async def test_canonical_event_trigger_filters_and_dedupes_dispatch(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "canonical_event",
            "event_type": CanonicalEventType.CI_PIPELINE.value,
            "event_filter": {"project_id": "project-a"},
        }
        self._publish_payload("ci-remediation", payload)

        matches = []
        triggers = AutomationEventTriggerService(self.registry, self.event_bus)
        unsubscribe = triggers.install(matches.append)
        try:
            first = await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-1",
                payload={"project_id": "project-a", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
            duplicate = await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-1",
                payload={"project_id": "project-a", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
            await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-2",
                payload={"project_id": "project-b", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
        finally:
            unsubscribe()

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].automation_id, "ci-remediation")
        self.assertEqual(matches[0].event_id, first.event.event_id)
        self.assertEqual(matches[0].definition_ref.revision, 1)
        self.assertEqual(
            matches[0].dedupe_key,
            f"{matches[0].definition_ref.record_id}:{first.event.event_id}",
        )

    async def test_provider_event_trigger_uses_normalized_provider_identity(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "provider_event",
            "event_type": "issue.updated",
            "provider_id": "gitlab:primary",
            "event_filter": {"project_id": "project-a"},
        }
        self._publish_payload("gitlab-issue-routing", payload)

        matches = []
        triggers = AutomationEventTriggerService(self.registry, self.event_bus)
        unsubscribe = triggers.install(matches.append)
        try:
            await self.events.ingest(
                event_type=CanonicalEventType.TASK_SOURCE,
                source="task-source:gitlab",
                idempotency_key="issue-42-v2",
                payload={
                    "project_id": "project-a",
                    "provider_event_type": "issue.updated",
                    "identity": {
                        "source_type": "gitlab",
                        "source_instance": "primary",
                    },
                },
                tenant_id="local",
                workspace_id="default",
            )
        finally:
            unsubscribe()

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].automation_id, "gitlab-issue-routing")

    def test_manual_paused_automation_is_valid_without_scheduler_configuration(self) -> None:
        payload = self._payload()
        payload["lifecycle"] = "paused"
        payload["trigger"] = {"type": "manual"}

        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id="manual-paused",
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=payload,
                actor="operator",
            )
        )

        self.assertEqual(draft.payload["lifecycle"], "paused")
        self.assertEqual(draft.payload["trigger"]["type"], "manual")


if __name__ == "__main__":
    unittest.main()
