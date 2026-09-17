from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import WorkItemHandoffCreate, WorkItemState
from codex_web.services.work_item_state import WorkItemStateMachine, install_work_item_state_machine


class _Host:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {"quinn", "release manager", "orchestrator"}
    OWNER_QUEUE_AGENTS = ("dana", "quinn")

    def __init__(self, root: Path) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states: dict[str, WorkItemState] = {}

    def _load_work_item_states(self):
        return {ref: state.model_copy(deep=True) for ref, state in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {ref: state.model_copy(deep=True) for ref, state in states.items()}

    def _leading_owner_cue_in_action(self, value):
        return None


class WorkItemStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = _Host(self.root)
        self.machine = WorkItemStateMachine(self.host)
        self.state = WorkItemState(
            ref="canonical-1",
            project_id="home",
            current_owner="dana",
            current_stage="implementation_active",
            implementation_owner="dana",
            validation_owner="quinn",
            release_owner="release manager",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            last_owner_activity_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host.states[self.state.ref] = self.state.model_copy(deep=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_handoff_transition_is_owned_by_state_machine(self) -> None:
        state = self.machine._structured_handoff(
            self.state.ref,
            WorkItemHandoffCreate(
                from_agent="dana",
                to_agent="quinn",
                expected_action="Validate merge request",
                artifact_state="branch",
            ),
        )

        self.assertIsNotNone(state.handoff)
        self.assertEqual(state.handoff.status, "pending")
        self.assertEqual(state.next_owner, "quinn")
        self.assertEqual(state.status_label, "status::awaiting confirmation")
        self.assertEqual(self.host.states[self.state.ref].next_owner, "quinn")
        self.assertTrue(self.host.WORK_ITEM_EVENTS_FILE.exists())

    def test_invalid_implementation_to_release_handoff_is_rejected(self) -> None:
        finding = self.machine._validate_handoff_edge(
            self.state,
            from_agent="dana",
            to_agent="release manager",
            artifact_state="branch",
        )
        self.assertEqual(finding["code"], "branch_only_artifact")

    def test_state_machine_has_no_provider_transport_or_projection_methods(self) -> None:
        provider_methods = (
            "sync_gitlab_issue_labels",
            "schedule_gitlab_issue_label_sync",
            "_upsert_work_item_state_from_gitlab_issue",
            "_upsert_work_item_state_from_gitlab_event",
            "_parse_gitlab_timestamp",
            "_gitlab_projection_is_stale",
            "_infer_artifact_state_from_gitlab_payload",
        )
        for name in provider_methods:
            self.assertFalse(hasattr(self.machine, name), name)
        self.assertFalse(hasattr(self.machine, "gitlab"))

    def test_split_brain_checks_do_not_infer_owner_from_provider_labels(self) -> None:
        state = self.state.model_copy(deep=True)
        state.labels = ["owner::someone-else"]
        state.status_label = "status::in progress"
        findings = self.machine._work_item_split_brain_findings(state)
        self.assertFalse(any("owner drift" in finding for finding in findings))

    def test_installer_rebinds_only_canonical_entrypoints(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        machine = install_work_item_state_machine(app, self.host)

        self.assertIs(app.state.work_item_state_machine, machine)
        self.assertEqual(self.host._structured_handoff.__self__, machine)
        self.assertEqual(self.host._work_item_state_public.__self__, machine)
        self.assertFalse(hasattr(self.host, "_upsert_work_item_state_from_gitlab_issue"))
        self.assertFalse(hasattr(self.host, "_upsert_work_item_state_from_gitlab_event"))
        self.assertFalse(hasattr(self.host, "_sync_gitlab_issue_labels_from_work_item"))


class GitLabClientIssueUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_project_issue_uses_async_put_json(self) -> None:
        observed = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["method"] = request.method
            observed["path"] = request.url.raw_path.decode()
            observed["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"iid": 7, "labels": ["owner::dana"]})

        client = GitLabClient(transport=httpx.MockTransport(handler))
        result = await client.update_project_issue(
            "https://gitlab.example/api/v4",
            "group/project",
            7,
            token="secret",
            payload={"labels": "owner::dana"},
        )

        self.assertEqual(observed["method"], "PUT")
        self.assertEqual(observed["path"], "/api/v4/projects/group%2Fproject/issues/7")
        self.assertEqual(observed["body"], {"labels": "owner::dana"})
        self.assertEqual(result["iid"], 7)


if __name__ == "__main__":
    unittest.main()
