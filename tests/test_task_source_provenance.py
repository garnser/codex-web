from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.work_items import WorkItemService


class _GitLab:
    async def group_issues(self, api_base, group, *, token, labels=None, state="opened"):
        return [
            {
                "id": 987654,
                "iid": 42,
                "title": "External issue",
                "web_url": "https://gitlab.example/group/project/-/issues/42",
                "updated_at": "2026-09-17T19:00:00Z",
                "references": {"full": "group/project#42"},
                "labels": [],
            }
        ]


class _StateMachine:
    def _upsert_work_item_state_from_gitlab_event(self, payload, *, project_id):
        return None


class _Projector:
    def __init__(self) -> None:
        self.saved: WorkItemState | None = None
        self.source = None
        self.snapshot = None

    def upsert(self, source, snapshot, *, project_id):
        self.source = source
        self.snapshot = snapshot
        self.saved = WorkItemState(
            ref="canonical-work-123",
            project_id=project_id,
            project_path="group/project",
            source_identity=snapshot.identity,
            title=snapshot.title,
            url=snapshot.identity.external_url,
            kind="issue",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        return self.saved


class _Host:
    GITLAB_API_BASE = "https://gitlab.example/api/v4/"

    def _load_gitlab_routing_settings(self):
        return SimpleNamespace(projects={"home": SimpleNamespace(enabled=True)})

    def _gitlab_token_for_project(self, project_id):
        return "token"

    def _gitlab_group_path(self, project_settings):
        return "group"


class TaskSourceProvenanceTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_is_strict_frozen_canonical_model(self) -> None:
        identity = TaskSourceIdentity(
            source_type=" gitlab ",
            source_instance=" https://gitlab.example/api/v4 ",
            external_id=" 42 ",
        )
        self.assertEqual(identity.source_type, "gitlab")
        self.assertEqual(identity.external_id, "42")
        with self.assertRaises(Exception):
            identity.external_id = "43"

    def test_existing_work_item_without_source_identity_still_loads(self) -> None:
        state = WorkItemState.model_validate(
            {
                "ref": "legacy#1",
                "last_meaningful_update_at": 1.0,
                "updated_at": 1.0,
                "created_at": 1.0,
            }
        )
        self.assertIsNone(state.source_identity)

    async def test_gitlab_discovery_uses_normalized_source_identity_without_rewriting_canonical_ref(self) -> None:
        host = _Host()
        projector = _Projector()
        service = WorkItemService(
            host,
            gitlab=_GitLab(),
            state_machine=_StateMachine(),
            task_source_projector=projector,
        )

        result = await service._sync_from_gitlab_async()

        self.assertEqual(result, {"synced": 1, "refs": 1})
        self.assertIsNotNone(projector.saved)
        saved = projector.saved
        self.assertEqual(saved.ref, "canonical-work-123")
        self.assertIsNotNone(saved.source_identity)
        self.assertEqual(saved.source_identity.source_type, "gitlab")
        self.assertEqual(saved.source_identity.source_instance, "https://gitlab.example/api/v4")
        self.assertEqual(saved.source_identity.external_id, "group/project#42")
        self.assertEqual(
            saved.source_identity.external_url,
            "https://gitlab.example/group/project/-/issues/42",
        )
        self.assertEqual(saved.source_identity.revision, "2026-09-17T19:00:00Z")
        self.assertEqual(projector.snapshot.identity, saved.source_identity)

    async def test_source_identity_round_trips_with_persisted_work_item(self) -> None:
        host = _Host()
        projector = _Projector()
        service = WorkItemService(
            host,
            gitlab=_GitLab(),
            state_machine=_StateMachine(),
            task_source_projector=projector,
        )
        await service._sync_from_gitlab_async()

        payload = projector.saved.model_dump(mode="json")
        restored = WorkItemState.model_validate(payload)

        self.assertEqual(restored.source_identity, projector.saved.source_identity)
        self.assertEqual(restored.ref, projector.saved.ref)


if __name__ == "__main__":
    unittest.main()
