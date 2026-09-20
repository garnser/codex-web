from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceWritebackService,
)
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceSnapshot,
    TaskSourceWriteResult,
)
from codex_web.services.work_item_dependencies import (
    WorkItemRuntimeDependencies,
)


def _state(
    *,
    owner: str = "james",
    stage: str = "implementation_active",
    updated_at: float = 10.0,
    source_type: str = "fake",
    source_instance: str = "https://source.example",
) -> WorkItemState:
    return WorkItemState(
        ref="group/project#1",
        project_id="home",
        source_identity=TaskSourceIdentity(
            source_type=source_type,
            source_instance=source_instance,
            external_id="group/project#1",
            revision="r1",
        ),
        current_owner=owner,
        current_stage=stage,
        last_meaningful_update_at=updated_at,
        updated_at=updated_at,
        created_at=1.0,
    )


def _dependencies(
    root: Path,
    states: dict[str, WorkItemState],
) -> WorkItemRuntimeDependencies:
    def get_state(ref: str):
        state = states.get(ref)
        return state.model_copy(deep=True) if state is not None else None

    def save_state(state: WorkItemState) -> None:
        states[state.ref] = state.model_copy(deep=True)

    return WorkItemRuntimeDependencies(
        data_dir=root,
        events_file=root / "events.jsonl",
        load_states=lambda: {
            key: value.model_copy(deep=True)
            for key, value in states.items()
        },
        save_states=lambda values: states.update(
            {
                key: value.model_copy(deep=True)
                for key, value in values.items()
            }
        ),
        load_projects=lambda: [],
        resource_ids_for_project=lambda _project_id: [],
        leading_owner_cue_in_action=lambda _action: None,
        default_validation_owner="quinn",
        default_release_owner="release manager",
        non_implementation_owners=frozenset(),
        get_state=get_state,
        save_state=save_state,
    )


class _CombinedSource:
    source_type = "fake"
    source_instance = "https://source.example"
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.READ,
                TaskSourceCapability.OWNER_WRITE,
                TaskSourceCapability.STATE_WRITE,
            }
        )
    )

    def __init__(self) -> None:
        self.calls = 0
        self.failures_remaining = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def write_projection(
        self,
        identity,
        *,
        owner,
        state,
        current_snapshot=None,
    ):
        del current_snapshot
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary provider failure")
        return TaskSourceWriteResult(
            snapshot=TaskSourceSnapshot(
                identity=identity.model_copy(
                    update={"revision": f"r{self.calls + 1}"}
                ),
                source_state=(
                    "closed" if state == "closed" else "opened"
                ),
                labels=(
                    f"owner::{owner}",
                    (
                        "status::blocked"
                        if state == "failed_with_action_owner"
                        else "status::in progress"
                    ),
                ),
            ),
            changed=True,
            provider_reads=1,
            provider_writes=1,
        )


class _GitLabClient:
    def __init__(self, issue: dict) -> None:
        self.issue = dict(issue)
        self.reads = 0
        self.writes = 0
        self.payloads: list[dict] = []

    async def project_issue(
        self,
        _api_base,
        _project,
        _iid,
        *,
        token,
    ):
        del token
        self.reads += 1
        return dict(self.issue)

    async def update_project_issue(
        self,
        _api_base,
        _project,
        _iid,
        *,
        token,
        payload,
    ):
        del token
        self.writes += 1
        self.payloads.append(dict(payload))
        labels = payload.get("labels")
        if labels is not None:
            self.issue["labels"] = labels.split(",") if labels else []
        if payload.get("state_event") == "close":
            self.issue["state"] = "closed"
        elif payload.get("state_event") == "reopen":
            self.issue["state"] = "opened"
        self.issue["updated_at"] = f"r{self.writes + 1}"
        return dict(self.issue)


def _gitlab_issue(
    *,
    labels: list[str],
    state: str = "opened",
) -> dict:
    return {
        "iid": 1,
        "references": {"full": "group/project#1"},
        "title": "Task",
        "state": state,
        "labels": list(labels),
        "assignees": [],
        "web_url": "https://gitlab.example/group/project/-/issues/1",
        "updated_at": "r1",
    }


class TaskSourceWritebackPerformanceTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_100_rapid_updates_coalesce_to_latest_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = _state()
            states = {initial.ref: initial.model_copy(deep=True)}
            source = _CombinedSource()
            registry = TaskSourceRegistry()
            registry.register("fake", lambda _state: source)
            service = TaskSourceWritebackService(
                None,
                registry,
                dependencies=_dependencies(root, states),
            )

            for index in range(100):
                state = _state(
                    owner=f"owner-{index}",
                    updated_at=20.0 + index,
                )
                states[state.ref] = state.model_copy(deep=True)
                service.schedule(state)

            tasks = list(service._tasks.values())
            self.assertEqual(len(tasks), 1)
            await asyncio.gather(*tasks)

            self.assertEqual(source.calls, 1)
            self.assertEqual(
                states[initial.ref].current_owner,
                "owner-99",
            )
            status = service.status()
            self.assertEqual(status["scheduled"], 100)
            self.assertEqual(status["coalesced"], 99)
            self.assertEqual(status["providerReads"], 1)
            self.assertEqual(status["providerWrites"], 1)

    async def test_stale_completion_only_merges_provider_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = _state(owner="old", updated_at=10.0)
            states = {stale.ref: stale.model_copy(deep=True)}
            source = _CombinedSource()
            source.release = asyncio.Event()
            registry = TaskSourceRegistry()
            registry.register("fake", lambda _state: source)
            service = TaskSourceWritebackService(
                None,
                registry,
                dependencies=_dependencies(root, states),
            )

            task = asyncio.create_task(service.sync(stale))
            await source.started.wait()

            newer = _state(
                owner="new",
                stage="ready_for_validation",
                updated_at=20.0,
            )
            states[newer.ref] = newer.model_copy(deep=True)
            source.release.set()
            await task

            saved = states[stale.ref]
            self.assertEqual(saved.current_owner, "new")
            self.assertEqual(
                saved.current_stage,
                "ready_for_validation",
            )
            self.assertEqual(saved.source_identity.revision, "r2")

    async def test_retry_is_bounded_and_observable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = _state()
            states = {state.ref: state.model_copy(deep=True)}
            source = _CombinedSource()
            source.failures_remaining = 2
            registry = TaskSourceRegistry()
            registry.register("fake", lambda _state: source)
            service = TaskSourceWritebackService(
                None,
                registry,
                dependencies=_dependencies(root, states),
            )
            service.BASE_RETRY_SECONDS = 0
            service.MAX_RETRY_SECONDS = 0

            service.schedule(state)
            await asyncio.gather(*list(service._tasks.values()))

            self.assertEqual(source.calls, 3)
            status = service.status()
            self.assertEqual(status["retried"], 2)
            self.assertEqual(status["failures"], 2)
            self.assertEqual(status["applied"], 1)

    async def test_gitlab_owner_and_stage_change_use_one_get_one_put(self) -> None:
        client = _GitLabClient(
            _gitlab_issue(
                labels=[
                    "owner::old",
                    "status::blocked",
                    "keep-me",
                ]
            )
        )
        source = GitLabTaskSource(
            "https://gitlab.example/api/v4",
            "token",
            client=client,
        )
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#1",
            revision="r1",
        )

        result = await source.write_projection(
            identity,
            owner="james",
            state="implementation_active",
        )

        self.assertTrue(result.changed)
        self.assertEqual(client.reads, 1)
        self.assertEqual(client.writes, 1)
        self.assertEqual(result.provider_reads, 1)
        self.assertEqual(result.provider_writes, 1)
        labels = set(client.payloads[0]["labels"].split(","))
        self.assertIn("owner::james", labels)
        self.assertIn("status::in progress", labels)
        self.assertIn("keep-me", labels)
        self.assertNotIn("owner::old", labels)
        self.assertNotIn("status::blocked", labels)

    async def test_gitlab_unchanged_projection_has_zero_mutations(self) -> None:
        client = _GitLabClient(
            _gitlab_issue(
                labels=[
                    "owner::james",
                    "status::in progress",
                    "keep-me",
                ]
            )
        )
        source = GitLabTaskSource(
            "https://gitlab.example/api/v4",
            "token",
            client=client,
        )
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#1",
            revision="r1",
        )

        result = await source.write_projection(
            identity,
            owner="james",
            state="implementation_active",
        )

        self.assertFalse(result.changed)
        self.assertEqual(client.reads, 1)
        self.assertEqual(client.writes, 0)
        self.assertEqual(result.provider_writes, 0)


if __name__ == "__main__":
    unittest.main()
