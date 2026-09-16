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
    GITLAB_API_BASE = "https://gitlab.example/api/v4"

    def __init__(self, root: Path) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states: dict[str, WorkItemState] = {}
        self.semantic_updates: list[dict] = []

    def _load_work_item_states(self):
        return {ref: state.model_copy(deep=True) for ref, state in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {ref: state.model_copy(deep=True) for ref, state in states.items()}

    def _gitlab_token_for_project(self, project_id: str):
        return "token" if project_id == "home" else None

    def _remember_gitlab_semantic_issue_state(self, ref, **payload):
        self.semantic_updates.append({"ref": ref, **payload})

    def _leading_owner_cue_in_action(self, value):
        return None

    def _gitlab_label_names(self, payload):
        return list(payload.get("labels") or [])

    def _gitlab_owner_agents(self, labels):
        return [label.split("::", 1)[1] for label in labels if label.startswith("owner::")]

    def _gitlab_url(self, payload):
        return None

    def _mr_refs_from_payload(self, payload):
        return []

    def _project_issue_ref(self, payload):
        return None

    def _append_bot_event(self, event):
        pass


class _GitLab:
    def __init__(self) -> None:
        self.updated_payload: dict | None = None

    async def project_issue(self, api_base, project, iid, *, token):
        return {
            "state": "opened",
            "labels": ["keep", "owner::james", "status::blocked"],
        }

    async def update_project_issue(self, api_base, project, iid, *, token, payload):
        self.updated_payload = payload
        return {
            "state": "opened",
            "labels": ["keep", "owner::dana", "status::in progress"],
        }


class WorkItemStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = _Host(self.root)
        self.gitlab = _GitLab()
        self.machine = WorkItemStateMachine(self.host, self.gitlab)
        self.state = WorkItemState(
            ref="group/project#1",
            project_id="home",
            project_path="group/project",
            kind="issue",
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

    async def test_label_projection_uses_async_gitlab_client_and_persists_result(self) -> None:
        state = await self.machine.sync_gitlab_issue_labels(self.state.model_copy(deep=True))

        self.assertEqual(
            self.gitlab.updated_payload,
            {"labels": "keep,owner::dana,status::in progress"},
        )
        self.assertEqual(state.labels, ["keep", "owner::dana", "status::in progress"])
        self.assertEqual(self.host.states[state.ref].labels, state.labels)
        self.assertEqual(self.host.semantic_updates[-1]["reason"], "codex-web-label-sync")

    def test_installer_rebinds_legacy_entrypoints_to_one_engine(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        machine = install_work_item_state_machine(app, self.host, self.gitlab)

        self.assertIs(app.state.work_item_state_machine, machine)
        self.assertEqual(self.host._structured_handoff.__self__, machine)
        self.assertEqual(self.host._work_item_state_public.__self__, machine)


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
