from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.services.work_item_state import WorkItemStateMachine


class _Client:
    pass


class _Host:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {"quinn", "release manager"}

    def __init__(self, root: Path) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states: dict[str, WorkItemState] = {}
        self.load_calls = 0
        self.save_calls = 0
        self.get_calls = 0
        self.identity_lookup_calls = 0
        self.put_calls = 0

    def _load_work_item_states(self):
        self.load_calls += 1
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.save_calls += 1
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}

    def _get_work_item_state_record(self, ref):
        self.get_calls += 1
        value = self.states.get(ref)
        return value.model_copy(deep=True) if value is not None else None

    def _get_work_item_state_by_source_identity(self, identity):
        self.identity_lookup_calls += 1
        for value in self.states.values():
            current = value.source_identity
            if current is None:
                continue
            if (
                current.source_type.strip().casefold()
                == identity.source_type.strip().casefold()
                and current.source_instance.strip().rstrip("/")
                == identity.source_instance.strip().rstrip("/")
                and current.external_id.strip()
                == identity.external_id.strip()
            ):
                return value.model_copy(deep=True)
        return None

    def _put_work_item_state_record(self, state):
        self.put_calls += 1
        self.states[state.ref] = state.model_copy(deep=True)

    def _leading_owner_cue_in_action(self, action):
        return None

    def _resource_ids_for_project(
        self,
        project_id,
        alias_value=None,
        provider=None,
    ):
        if alias_value is not None:
            self.resource_lookup = (project_id, alias_value, provider)
            return ["resource-repo"]
        return ["resource-repo", "resource-prod"]


class TaskSourceWorkItemProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.host = _Host(Path(self.tempdir.name))
        self.machine = WorkItemStateMachine(self.host)
        self.projector = TaskSourceWorkItemProjector(self.host, self.machine)
        self.source = GitLabTaskSource(
            "https://gitlab.example/api/v4",
            "token",
            client=_Client(),
        )

    def snapshot(self, *, revision: str = "2026-09-17T19:00:00Z") -> TaskSourceSnapshot:
        return TaskSourceSnapshot(
            identity=TaskSourceIdentity(
                source_type="gitlab",
                source_instance="https://gitlab.example/api/v4",
                external_id="group/project#42",
                external_url="https://gitlab.example/group/project/-/issues/42",
                revision=revision,
            ),
            title="External issue",
            source_state="opened",
            labels=("owner::carl", "status::in progress", "priority::P1"),
        )

    def test_new_snapshot_creates_canonical_state_without_provider_payload(self) -> None:
        state = self.projector.upsert(self.source, self.snapshot(), project_id="home")

        self.assertEqual(state.ref, "group/project#42")
        self.assertEqual(state.source_identity.external_id, "group/project#42")
        self.assertEqual(state.current_owner, "carl")
        self.assertEqual(state.current_stage, "implementation_active")
        self.assertEqual(state.priority, "priority::P1")
        self.assertEqual(state.resource_ids, ["resource-repo"])
        self.assertEqual(
            self.host.resource_lookup,
            ("home", "group/project", "gitlab"),
        )
        self.assertTrue(state.release_gate)
        self.assertEqual(self.host.states[state.ref].source_identity, state.source_identity)

    def test_non_gitlab_projection_retains_project_resource_scope(self) -> None:
        self.assertEqual(
            self.projector._project_resource_ids(
                "home",
                source_type="jira",
                project_path="space/project",
            ),
            ["resource-repo", "resource-prod"],
        )

    def test_existing_canonical_ref_is_preserved_by_source_identity(self) -> None:
        identity = self.snapshot().identity
        existing = WorkItemState(
            ref="canonical-work-123",
            project_id="home",
            project_path="group/project",
            source_identity=identity,
            current_owner="carl",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            last_gitlab_event_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host.states[existing.ref] = existing

        state = self.projector.upsert(self.source, self.snapshot(), project_id="home")

        self.assertEqual(state.ref, "canonical-work-123")
        self.assertIn("canonical-work-123", self.host.states)
        self.assertNotIn("group/project#42", self.host.states)
        self.assertEqual(state.source_identity.external_id, "group/project#42")

    def test_older_snapshot_cannot_overwrite_newer_canonical_projection(self) -> None:
        newer = self.snapshot(revision="2026-09-17T20:00:00Z")
        state = self.projector.upsert(self.source, newer, project_id="home")
        newer_timestamp = state.last_gitlab_event_at

        older = TaskSourceSnapshot(
            identity=self.snapshot(revision="2026-09-17T19:00:00Z").identity,
            title="Stale title",
            source_state="opened",
            labels=("owner::someone-else", "status::blocked"),
        )
        result = self.projector.upsert(self.source, older, project_id="home")

        self.assertEqual(result.current_owner, "carl")
        self.assertEqual(result.current_stage, "implementation_active")
        self.assertEqual(result.last_gitlab_event_at, newer_timestamp)

    def test_normal_projection_uses_no_collection_wide_load_or_save(self) -> None:
        self.projector.upsert(
            self.source,
            self.snapshot(),
            project_id="home",
        )

        self.assertEqual(self.host.load_calls, 0)
        self.assertEqual(self.host.save_calls, 0)
        self.assertEqual(self.host.get_calls, 1)
        self.assertEqual(self.host.identity_lookup_calls, 1)
        self.assertEqual(self.host.put_calls, 1)
        metrics = self.projector.metrics()
        self.assertEqual(metrics["compatibility_full_scans"], 0)
        self.assertEqual(metrics["compatibility_bulk_saves"], 0)
        self.assertEqual(metrics["keyed_saves"], 1)

    def test_direct_ref_hit_skips_source_identity_lookup(self) -> None:
        initial = self.projector.upsert(
            self.source,
            self.snapshot(),
            project_id="home",
        )
        self.host.get_calls = 0
        self.host.identity_lookup_calls = 0
        self.host.put_calls = 0

        result = self.projector.upsert(
            self.source,
            self.snapshot(revision="2026-09-17T20:00:00Z"),
            project_id="home",
        )

        self.assertEqual(result.ref, initial.ref)
        self.assertEqual(self.host.get_calls, 1)
        self.assertEqual(self.host.identity_lookup_calls, 0)
        self.assertEqual(self.host.put_calls, 1)

    def test_stale_snapshot_has_no_persistence_write(self) -> None:
        self.projector.upsert(
            self.source,
            self.snapshot(revision="2026-09-17T20:00:00Z"),
            project_id="home",
        )
        self.host.put_calls = 0

        self.projector.upsert(
            self.source,
            self.snapshot(revision="2026-09-17T19:00:00Z"),
            project_id="home",
        )

        self.assertEqual(self.host.put_calls, 0)

    def test_stale_snapshot_repairs_repository_routing_metadata(self) -> None:
        current = self.projector.upsert(
            self.source,
            self.snapshot(revision="2026-09-17T20:00:00Z"),
            project_id="home",
        )
        stored = self.host.states[current.ref]
        stored.resource_ids = []
        stored.project_path = None
        self.host.put_calls = 0

        stale = TaskSourceSnapshot(
            identity=self.snapshot(revision="2026-09-17T19:00:00Z").identity,
            title="Stale title",
            source_state="closed",
            labels=("owner::someone-else", "status::blocked"),
        )
        result = self.projector.upsert(
            self.source,
            stale,
            project_id="home",
        )

        self.assertEqual(result.resource_ids, ["resource-repo"])
        self.assertEqual(result.project_path, "group/project")
        self.assertEqual(result.title, "External issue")
        self.assertEqual(result.current_owner, "carl")
        self.assertEqual(result.current_stage, "implementation_active")
        self.assertEqual(self.host.put_calls, 1)

    def test_compatibility_fallback_scans_and_bulk_saves_when_indexes_absent(self) -> None:
        self.projector.dependencies = replace(
            self.projector.dependencies,
            get_state=None,
            get_state_by_source_identity=None,
            save_state=None,
        )

        state = self.projector.upsert(
            self.source,
            self.snapshot(),
            project_id="home",
        )

        self.assertEqual(state.ref, "group/project#42")
        self.assertEqual(self.host.load_calls, 1)
        self.assertEqual(self.host.save_calls, 1)
        metrics = self.projector.metrics()
        self.assertEqual(metrics["compatibility_full_scans"], 1)
        self.assertEqual(metrics["compatibility_bulk_saves"], 1)

    def test_many_new_snapshots_do_not_scale_with_existing_collection_reads(self) -> None:
        for index in range(200):
            identity = TaskSourceIdentity(
                source_type="gitlab",
                source_instance="https://gitlab.example/api/v4",
                external_id=f"other/project#{index}",
            )
            self.host.states[f"canonical-{index}"] = WorkItemState(
                ref=f"canonical-{index}",
                project_id="home",
                source_identity=identity,
                current_stage="implementation_active",
                last_meaningful_update_at=1.0,
                updated_at=1.0,
                created_at=1.0,
            )

        self.host.load_calls = 0
        self.host.save_calls = 0
        for index in range(95):
            snapshot = TaskSourceSnapshot(
                identity=TaskSourceIdentity(
                    source_type="gitlab",
                    source_instance="https://gitlab.example/api/v4",
                    external_id=f"group/project#{1000 + index}",
                    revision="2026-09-17T19:00:00Z",
                ),
                title=f"External issue {index}",
                source_state="opened",
                labels=("owner::carl",),
            )
            self.projector.upsert(
                self.source,
                snapshot,
                project_id="home",
            )

        self.assertEqual(self.host.load_calls, 0)
        self.assertEqual(self.host.save_calls, 0)
        self.assertEqual(self.host.put_calls, 95)
        metrics = self.projector.metrics()
        self.assertEqual(metrics["keyed_gets"], 95)
        self.assertEqual(metrics["source_identity_index_lookups"], 95)
        self.assertEqual(metrics["compatibility_full_scans"], 0)
        self.assertEqual(metrics["compatibility_bulk_saves"], 0)


if __name__ == "__main__":
    unittest.main()
