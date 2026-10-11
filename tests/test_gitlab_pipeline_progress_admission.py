import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from codex_web.autonomy import AutonomyCycleOutcome
from codex_web.models import GitLabRoutingSettings, GitLabProjectRoutingSettings
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.gitlab import GitLabService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class PipelineProgressAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.ingestion = CanonicalEventIngestionService(CanonicalEventBus(
            CanonicalEventStore(SQLiteStateStore(Path(self.directory.name) / "state.sqlite3"))))
        host = NS(_project=lambda _:NS(organization_id="org", workspace_id="workspace"),
                  _load_gitlab_routing_settings=lambda:GitLabRoutingSettings(enabled=True),
                  _gitlab_event_id=lambda request, payload:"event-" + payload["object_attributes"]["status"])
        self.service = GitLabService(host, canonical_events=self.ingestion)
        self.service.project_settings_for_payload = lambda *_:("project", GitLabProjectRoutingSettings())
        self.service.event_target_agents = lambda *_:["quinn"]
        self.service.routing_bindings_for_agent = lambda *_:[NS(thread_id="thread")]
        self.state = NS(resource_ids=("repo",), ref="group/project@pipeline:1")
        self.service.work_items = NS(project_event=Mock(return_value=self.state))
        self.dispatch = AsyncMock(return_value={"ok":True,"queued":True,"threadId":"thread"})
        self.notice = AsyncMock(return_value={"sent":True})
        self.recovery = Mock()
        self.events = []
        original = self.service.operations
        self.service.operations = replace(original,
            verify_webhook=Mock(), format_event_prompt=lambda *_:"pipeline event",
            dispatch_event=self.dispatch, dispatch_scoped_event=self.dispatch,
            send_event_notice=self.notice, append_event=self.events.append,
            publish_event=AsyncMock(), schedule_recovery=self.recovery)

    async def handle(self, status, *, controller=False, kind="pipeline"):
        self.payload = {"object_kind":kind, "project":{"path_with_namespace":"group/project"},
                        "object_attributes":{"id":1,"iid":1,"status":status,"ref":"main","sha":"a" * 40}}
        if controller:
            async def process(event, observation, **kwargs):
                if observation.deterministic_resolved:
                    self.assertIsNone(kwargs.get("reasoner"))
                    return NS(outcome=AutonomyCycleOutcome.DETERMINISTIC)
                await kwargs["reasoner"]()
                return NS(outcome=AutonomyCycleOutcome.COMPLETED)
            self.service.autonomy_controller = NS(process=AsyncMock(side_effect=process))
        return await self.service.handle_event(NS(json=AsyncMock(return_value=self.payload)))

    async def test_pending_and_running_are_projected_without_native_turns(self):
        for status in ("pending", "running"):
            with self.subTest(status=status):
                await self.asyncSetUp()
                result = await self.handle(status)
                self.assertTrue(result["accepted"])
                self.dispatch.assert_not_awaited()
                self.recovery.assert_not_called()
                self.service.work_items.project_event.assert_called_once_with(self.payload, project_id="project")
                self.notice.assert_awaited_once()
                self.assertTrue(result["deterministic"])
                self.assertFalse(result["targets"][0]["queued"])
                events = self.ingestion.bus.store.load().events
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].payload["status"], status)

    async def test_duplicate_progress_preserves_ingestion_deduplication(self):
        await self.handle("running")
        duplicate = await self.service.handle_event(NS(json=AsyncMock(return_value=self.payload)))
        self.assertTrue(duplicate["ignored"])
        self.assertEqual(duplicate["reason"], "duplicate")
        self.assertEqual(len(self.ingestion.bus.store.load().events), 1)
        self.service.work_items.project_event.assert_called_once()
        self.notice.assert_awaited_once()
        self.dispatch.assert_not_awaited()

    async def test_progress_uses_the_existing_deterministic_autonomy_gate(self):
        result = await self.handle("running", controller=True)
        self.assertTrue(result["accepted"])
        self.dispatch.assert_not_awaited()
        observation = self.service.autonomy_controller.process.call_args.args[1]
        self.assertTrue(observation.deterministic_resolved)
        self.assertEqual(observation.reasoning_score, 0)

    async def test_terminal_unknown_and_other_event_statuses_still_dispatch(self):
        for status, kind in (("success","pipeline"), ("failed","pipeline"), ("canceled","pipeline"),
                             ("future-status","pipeline"), ("running","deployment")):
            with self.subTest(status=status, kind=kind):
                await self.asyncSetUp()
                result = await self.handle(status, kind=kind)
                self.assertTrue(result["accepted"])
                self.dispatch.assert_awaited_once()
                self.recovery.assert_called_once()
                self.assertTrue(result["targets"][0]["queued"])

