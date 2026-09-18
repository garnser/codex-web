from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

from codex_web.execution_contract_schema import execution_contract_for_work_item
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.models import (
    Project,
    TaskSourceConfiguration,
    TaskSourceIdentity,
    WorkItemState,
)
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.task_source_runtime import TaskSourceRegistry
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.services.work_item_operator import WorkItemOperatorService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.work_item_execution_models import WorkItemFailureReason


ROLE_CONTRACTS = ExecutionRoleCatalogDefinition.model_validate(
    execution_role_catalog_seed_payload()
).role_map


class _Hub:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, payload):
        self.events.append(payload)


class _Host:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {"quinn", "release manager", "orchestrator"}
    GITLAB_SYNC_LAST_SUCCESS_AT = None
    GITLAB_SYNC_LAST_ERROR = None
    GITLAB_SYNC_LAST_ERROR_AT = None
    GITLAB_SYNC_CONSECUTIVE_FAILURES = 0

    def __init__(self, root: Path, project: Project) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states = {}
        self.projects = [project]
        self.hub = _Hub()
        self.dispatches = []

    def _load_work_item_states(self):
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}

    def _load_projects(self):
        return [project.model_copy(deep=True) for project in self.projects]

    @staticmethod
    def _leading_owner_cue_in_action(value):
        return None

    def _schedule_actionable_owner_dispatch(self, state, *, source, actor=None):
        self.dispatches.append((state.ref, source, actor))

    @staticmethod
    def _work_item_execution_contract(state):
        return execution_contract_for_work_item(state, ROLE_CONTRACTS["james"])


class _WorkItems:
    def __init__(self, host, state_machine, registry, projector):
        self.host = host
        self.state_machine = state_machine
        self.task_source_registry = registry
        self.task_source_projector = projector


class WorkItemOperatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        project = Project(
            id="project-a",
            name="Project A",
            path=self.temp.name,
            authoritative_task_source=TaskSourceConfiguration(
                source_type="reference",
                source_instance="operator-test",
                scope="team-a",
            ),
        )
        self.host = _Host(Path(self.temp.name), project)
        self.machine = WorkItemStateMachine(self.host)
        identity = TaskSourceIdentity(
            source_type="reference",
            source_instance="operator-test",
            external_id="TASK-42",
            revision="1",
            event_cursor="cursor-1",
        )
        self.snapshot = TaskSourceSnapshot(
            identity=identity,
            title="External title",
            source_state="open",
            owners=("james",),
            labels=("priority::P1", "status::implementing"),
        )
        self.source = ReferenceTaskSource("operator-test", snapshots=[self.snapshot])
        self.registry = TaskSourceRegistry()
        self.registry.register("reference", lambda state: self.source)
        self.projector = TaskSourceWorkItemProjector(self.host, self.machine)
        state = WorkItemState(
            ref="TASK-42",
            project_id="project-a",
            source_identity=identity,
            title="Canonical title",
            current_owner="james",
            current_stage="implementation_active",
            implementation_owner="james",
            validation_owner="quinn",
            release_owner="release manager",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        state.execution.failure_reason = WorkItemFailureReason(
            category="provider",
            code="temporary",
            message="Temporary failure",
            retryable=True,
            recorded_at=2.0,
        )
        self.host.states[state.ref] = state
        self.service = WorkItemOperatorService(
            _WorkItems(self.host, self.machine, self.registry, self.projector)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_detail_explains_canonical_external_contract_and_actions(self) -> None:
        detail = self.service.detail("TASK-42")

        self.assertEqual(detail["item"]["current_stage"], "implementation_active")
        self.assertEqual(detail["external"]["identity"]["revision"], "1")
        self.assertTrue(detail["external"]["available"])
        self.assertIn("read", detail["external"]["capabilities"])
        self.assertEqual(detail["execution_contract"]["role_id"], "james")
        self.assertEqual(detail["execution_policy"]["sandbox"], "workspace-write")
        self.assertTrue(detail["actions"]["retry"]["allowed"])
        self.assertTrue(detail["actions"]["reconcile"]["allowed"])
        self.assertEqual(detail["diagnostics"][0]["kind"], "execution_failure")

    async def test_retry_uses_canonical_execution_state_and_dispatch_seam(self) -> None:
        result = await self.service.retry("TASK-42", actor="operator", reason="try again")

        self.assertEqual(result["item"]["execution"]["retry"]["attempt"], 1)
        self.assertIsNone(result["item"]["execution"]["failure_reason"])
        self.assertEqual(self.host.dispatches, [("TASK-42", "operator-retry", "operator")])
        events = self.service.lifecycle.history("TASK-42")["items"]
        self.assertEqual(events[-1]["event_type"], "execution_lifecycle_updated")
        self.assertEqual(events[-1]["actor"], "operator")

    async def test_reconcile_reads_authoritative_source_and_records_attribution(self) -> None:
        updated_snapshot = TaskSourceSnapshot(
            identity=self.snapshot.identity.model_copy(update={"revision": "2"}),
            title="Authoritative update",
            source_state="open",
            owners=("james",),
            labels=("priority::P1", "status::implementing"),
        )
        self.source._snapshots["TASK-42"] = updated_snapshot

        result = await self.service.reconcile(
            "TASK-42",
            actor="operator",
            reason="refresh external truth",
        )

        self.assertEqual(result["item"]["title"], "Authoritative update")
        self.assertEqual(result["external"]["identity"]["revision"], "2")
        events = self.service.lifecycle.history("TASK-42")["items"]
        self.assertEqual(events[-1]["event_type"], "operator_reconciled")
        self.assertEqual(events[-1]["reason"], "refresh external truth")

    async def test_project_sync_consumes_canonical_project_source_configuration(self) -> None:
        second = TaskSourceSnapshot(
            identity=TaskSourceIdentity(
                source_type="reference",
                source_instance="operator-test",
                external_id="TASK-99",
                revision="1",
            ),
            title="Discovered task",
            source_state="open",
            owners=("james",),
        )
        self.source._snapshots["TASK-99"] = second

        result = await self.service.sync_project(
            "project-a",
            actor="operator",
            reason="manual source sync",
        )

        self.assertEqual(result["synced"], 2)
        self.assertIn("TASK-99", self.host.states)
        self.assertEqual(self.host.hub.events[-1]["type"], "work-item.sync")

    async def test_retry_fails_closed_when_policy_is_exhausted(self) -> None:
        state = self.host.states["TASK-42"]
        state.execution.retry.attempt = state.execution.retry.policy.max_attempts
        self.host.states[state.ref] = state

        with self.assertRaises(HTTPException) as raised:
            await self.service.retry("TASK-42", actor="operator", reason=None)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "work_item_retry_not_allowed")


if __name__ == "__main__":
    unittest.main()
