from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_web.models import TaskSourceIdentity, WorkItemHandoff, WorkItemProgressUpdate
from codex_web.services.work_items import WorkItemService
from tests import test_work_item_state_machine as state_fixture
from tests import test_task_source_writeback as writeback_fixture
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_runtime import TaskSourceWritebackService


class ProgressEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = state_fixture.WorkItemStateMachineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.machine = self.fixture.machine
        state = self.fixture.host.states[self.fixture.state.ref]
        state.current_owner = "sally"
        state.current_stage = "failed_with_action_owner"
        state.next_owner = "carl"
        state.next_action = "Carl: repair the existing renderer prerequisite"
        state.blocker = "Pinned renderer prerequisite unavailable"
        state.blocking_findings = ["Await explicit prerequisite result"]
        self.payload = WorkItemProgressUpdate(
            actor="sally", current_owner="sally", current_stage=state.current_stage,
            next_owner="carl", next_action=state.next_action,
            blocker=state.blocker, blocking_findings=state.blocking_findings,
        )
        self.ref = state.ref
        with patch("codex_web.services.work_item_state.time.time", return_value=100.0):
            self.machine._structured_progress(self.ref, self.payload.model_copy(update={"note": "Initial exact prerequisite evidence"}))

    def test_identical_progress_keeps_meaningful_clock_and_retains_activity_and_audit(self):
        with patch("codex_web.services.work_item_state.time.time", return_value=200.0):
            result = self.machine._structured_progress(self.ref, self.payload)
        self.assertEqual(result.last_meaningful_update_at, 100.0)
        self.assertEqual(result.last_owner_activity_at, 200.0)
        self.assertEqual(result.updated_at, 200.0)
        events = [json.loads(line) for line in self.fixture.host.WORK_ITEM_EVENTS_FILE.read_text().splitlines()]
        self.assertEqual(len(events), 2)
        self.assertFalse(events[-1]["payload"]["meaningful_change"])
        self.assertEqual(events[-1]["actor"], "sally")
        self.assertEqual(result.implementation_owner, "dana")
        self.assertEqual(result.validation_owner, "quinn")

    def test_new_note_action_owner_finding_or_artifact_is_meaningful(self):
        for field, value in [("note", "New concrete prerequisite result"),
                             ("next_action", "Carl: use the now available renderer"),
                             ("next_owner", "orchestrator"),
                             ("blocking_findings", ["New exact failed renderer test"]),
                             ("artifact_state", "merge_request")]:
            with self.subTest(field=field):
                with patch("codex_web.services.work_item_state.time.time", return_value=300.0):
                    result = self.machine._structured_progress(self.ref, self.payload.model_copy(update={field: value}))
                self.assertEqual(result.last_meaningful_update_at, 300.0)
                with patch("codex_web.services.work_item_state.time.time", return_value=100.0):
                    self.machine._structured_progress(self.ref, self.payload)

    def test_new_handoff_evidence_changes_the_same_canonical_item(self):
        state = self.machine._work_item_state(self.ref)
        state.handoff = WorkItemHandoff(from_agent="sally", to_agent="carl",
            status="accepted", requested_at=100.0, acknowledged_at=150.0)
        state.current_owner = "carl"
        self.machine._save_work_item_state(state)
        with patch("codex_web.services.work_item_state.time.time", return_value=200.0):
            result = self.machine._structured_progress(self.ref, WorkItemProgressUpdate(
                actor="carl", next_owner="orchestrator", note="New accepted prerequisite result"))
        self.assertEqual(result.last_meaningful_update_at, 200.0)
        self.assertEqual(result.handoff.status, "accepted")
        self.assertEqual(result.handoff.to_agent, "carl")
        self.assertEqual(result.implementation_owner, "dana")



class ProgressSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_override_does_not_require_a_canonical_record(self):
        fixture = ProgressEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        synthetic = fixture.machine._work_item_state(fixture.ref).model_copy(deep=True)
        fixture.fixture.host.states.clear()
        service = object.__new__(WorkItemService)
        service.state_machine = fixture.machine
        service.task_source_writeback = SimpleNamespace(sync=AsyncMock(side_effect=lambda state: state))
        override = Mock(return_value=synthetic)
        continuity = SimpleNamespace(schedule_actionable_owner_dispatch=Mock(), schedule_actionable_owner_continuity_check=Mock())
        result = await service._progress_with(fixture.ref, fixture.payload,
            continuity=continuity, recovery=SimpleNamespace(schedule=Mock()),
            structured_progress=override, public_state=lambda state: {"ref": state.ref},
            split_brain_findings=lambda state: [], publish_event=AsyncMock())
        self.assertTrue(result["ok"])
        override.assert_called_once_with(fixture.ref, fixture.payload)
        continuity.schedule_actionable_owner_dispatch.assert_called_once()
        continuity.schedule_actionable_owner_continuity_check.assert_called_once()

    async def test_noop_acknowledges_without_scheduling_but_new_evidence_dispatches(self):
        fixture = ProgressEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = object.__new__(WorkItemService)
        service.state_machine = fixture.machine
        service.task_source_writeback = SimpleNamespace(sync=AsyncMock(side_effect=lambda state: state))
        continuity = SimpleNamespace(schedule_actionable_owner_dispatch=Mock(), schedule_actionable_owner_continuity_check=Mock())
        recovery = SimpleNamespace(schedule=Mock())
        publish = AsyncMock()
        async def progress(payload):
            return await service._progress_with(
                fixture.ref, payload, continuity=continuity, recovery=recovery,
                structured_progress=fixture.machine._structured_progress,
                public_state=fixture.machine._work_item_state_public,
                split_brain_findings=lambda state: [], publish_event=publish,
            )
        result = await progress(fixture.payload)
        self.assertTrue(result["ok"])
        service.task_source_writeback.sync.assert_awaited_once()
        publish.assert_awaited_once()
        continuity.schedule_actionable_owner_dispatch.assert_not_called()
        continuity.schedule_actionable_owner_continuity_check.assert_not_called()
        await progress(fixture.payload.model_copy(update={"note": "Fresh prerequisite completion evidence"}))
        continuity.schedule_actionable_owner_dispatch.assert_called_once()
        continuity.schedule_actionable_owner_continuity_check.assert_called_once()

    async def test_real_gitlab_writeback_receipt_clock_does_not_retrigger_owner(self):
        fixture = ProgressEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state = fixture.fixture.host.states[fixture.ref]
        state.labels = ["owner::sally", "status::blocked"]
        state.source_identity = TaskSourceIdentity(
            source_type="gitlab", source_instance="https://gitlab.example/api/v4",
            external_id="group/project#1", revision="r1",
            external_url="https://gitlab.example/group/project/-/issues/1")
        client = writeback_fixture._GitLabClient({
            "references": {"full": "group/project#1"}, "iid": 1,
            "title": "Task", "state": "opened", "updated_at": "r1",
            "labels": list(state.labels),
            "web_url": "https://gitlab.example/group/project/-/issues/1"})
        source = GitLabTaskSource("https://gitlab.example/api/v4", "test-token", client=client)
        service = object.__new__(WorkItemService)
        service.state_machine = fixture.machine
        service.task_source_writeback = TaskSourceWritebackService(
            None, writeback_fixture._registry(source), dependencies=fixture.machine.dependencies)
        continuity = SimpleNamespace(schedule_actionable_owner_dispatch=Mock(), schedule_actionable_owner_continuity_check=Mock())
        async def progress():
            return await service._progress_with(fixture.ref, fixture.payload,
                continuity=continuity, recovery=SimpleNamespace(schedule=Mock()),
                structured_progress=fixture.machine._structured_progress,
                public_state=fixture.machine._work_item_state_public,
                split_brain_findings=lambda state: [], publish_event=AsyncMock())
        result = await progress()
        self.assertTrue(result["ok"])
        self.assertEqual(client.read_calls, 1)
        self.assertEqual(client.write_calls, 0)
        self.assertEqual(result["item"]["last_meaningful_update_at"], 100.0)
        continuity.schedule_actionable_owner_dispatch.assert_not_called()
        continuity.schedule_actionable_owner_continuity_check.assert_not_called()
        # Actual provider revision change remains new evidence on the SAME item.
        client.issue["updated_at"] = "r2"
        service.task_source_writeback._last_applied.clear()
        await progress()
        continuity.schedule_actionable_owner_dispatch.assert_called_once()

    async def test_fresh_provider_revision_remains_actionable_after_unchanged_local_receipt(self):
        fixture = ProgressEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = object.__new__(WorkItemService)
        service.state_machine = fixture.machine
        async def sync(state):
            state.source_identity = TaskSourceIdentity(source_type="gitlab", source_instance="https://gitlab.example", external_id="42", revision="new-exact-head")
            return state
        service.task_source_writeback = SimpleNamespace(sync=sync)
        continuity = SimpleNamespace(schedule_actionable_owner_dispatch=Mock(), schedule_actionable_owner_continuity_check=Mock())
        await service._progress_with(fixture.ref, fixture.payload,
            continuity=continuity, recovery=SimpleNamespace(schedule=Mock()),
            structured_progress=fixture.machine._structured_progress,
            public_state=fixture.machine._work_item_state_public,
            split_brain_findings=lambda state: [], publish_event=AsyncMock())
        continuity.schedule_actionable_owner_dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
