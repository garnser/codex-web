from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_web.models import (
    TaskSourceIdentity,
    WorkItemState,
)
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceWritebackService,
)
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceSnapshot,
    TaskSourceWritebackResult,
)


def _state(
    *,
    owner: str = "alice",
    stage: str = "implementation_active",
    updated_at: float = 1.0,
) -> WorkItemState:
    return WorkItemState(
        ref="group/project#1",
        project_id="project",
        project_path="group/project",
        title="Task",
        url="https://gitlab.example/group/project/-/issues/1",
        kind="issue",
        priority=None,
        current_owner=owner,
        current_stage=stage,
        handoff=None,
        last_meaningful_update_at=updated_at,
        last_gitlab_event_at=None,
        blocker=None,
        next_action="next",
        next_owner=None,
        release_gate=False,
        status_label=None,
        labels=[],
        mr_refs=[],
        notes=[],
        closed_at=None,
        updated_at=updated_at,
        created_at=1.0,
        source_identity=TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#1",
            revision="r1",
        ),
    )


class _Dependencies:
    def __init__(self, state: WorkItemState) -> None:
        self.state = state.model_copy(deep=True)
        self.saved: list[WorkItemState] = []

    def get_state(self, ref: str):
        if ref != self.state.ref:
            return None
        return self.state.model_copy(deep=True)

    def save_state(self, state: WorkItemState) -> None:
        self.state = state.model_copy(deep=True)
        self.saved.append(state.model_copy(deep=True))

    def load_states(self):
        return {self.state.ref: self.state.model_copy(deep=True)}

    def save_states(self, values):
        self.state = values[self.state.ref].model_copy(deep=True)


class _CombinedSource:
    source_type = "gitlab"
    source_instance = "https://gitlab.example/api/v4"
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
        self.read_calls = 0
        self.write_calls = 0
        self.writes: list[tuple[str | None, str]] = []
        self.failures = 0
        self.read_started: asyncio.Event | None = None
        self.read_release: asyncio.Event | None = None

    async def read(self, identity):
        self.read_calls += 1
        if self.read_started is not None:
            self.read_started.set()
        if self.read_release is not None:
            await self.read_release.wait()
        return TaskSourceSnapshot(
            identity=identity,
            source_state="opened",
            owners=(),
            labels=(),
        )

    async def write_projection(
        self,
        identity,
        current,
        *,
        owner,
        stage,
    ):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("provider unavailable")
        self.write_calls += 1
        self.writes.append((owner, stage))
        labels = tuple(
            item
            for item in (
                f"owner::{owner}" if owner else None,
                (
                    "status::awaiting confirmation"
                    if stage == "ready_for_validation"
                    else "status::in progress"
                ),
            )
            if item
        )
        return TaskSourceWritebackResult(
            snapshot=TaskSourceSnapshot(
                identity=identity.model_copy(
                    update={"revision": f"r{self.write_calls + 1}"}
                ),
                source_state="opened",
                labels=labels,
            ),
            mutated=True,
        )

    async def discover(self, *, scope):
        return []

    async def normalize_event(self, payload):
        return None

    def project(self, snapshot, *, current_stage=None):
        raise NotImplementedError

    async def write_owner(self, identity, owner):
        raise AssertionError("combined path expected")

    async def write_state(self, identity, state):
        raise AssertionError("combined path expected")

    async def add_comment(self, identity, body):
        return None

    async def attach_artifact(self, identity, url):
        return None


class _GitLabClient:
    def __init__(self, issue: dict) -> None:
        self.issue = dict(issue)
        self.read_calls = 0
        self.write_calls = 0
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
        self.read_calls += 1
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
        self.write_calls += 1
        self.payloads.append(dict(payload))
        labels = [
            item
            for item in str(payload.get("labels") or "").split(",")
            if item
        ]
        self.issue["labels"] = labels
        if payload.get("state_event") == "close":
            self.issue["state"] = "closed"
        elif payload.get("state_event") == "reopen":
            self.issue["state"] = "opened"
        self.issue["updated_at"] = f"r{self.write_calls + 1}"
        return dict(self.issue)


def _registry(source) -> TaskSourceRegistry:
    registry = TaskSourceRegistry()
    registry.register("gitlab", lambda _state: source)
    return registry


class TaskSourceWritebackCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def test_100_update_burst_uses_one_task_and_latest_desired_state(self) -> None:
        source = _CombinedSource()
        initial = _state()
        dependencies = _Dependencies(initial)
        service = TaskSourceWritebackService(
            None,
            _registry(source),
            dependencies=SimpleNamespace(
                get_state=dependencies.get_state,
                save_state=dependencies.save_state,
                load_states=dependencies.load_states,
                save_states=dependencies.save_states,
            ),
        )

        for index in range(100):
            service.schedule(
                _state(
                    owner=f"owner-{index}",
                    stage="implementation_active",
                    updated_at=float(index + 1),
                )
            )

        tasks = list(service._tasks.values())
        self.assertEqual(len(tasks), 1)
        await asyncio.gather(*tasks)

        self.assertEqual(source.read_calls, 1)
        self.assertEqual(source.write_calls, 1)
        self.assertEqual(
            source.writes,
            [("owner-99", "implementation_active")],
        )
        status = service.status()
        self.assertEqual(status["scheduled"], 100)
        self.assertGreaterEqual(status["coalesced"], 99)
        self.assertEqual(status["queueDepth"], 0)

    async def test_newer_generation_supersedes_state_while_provider_read_is_inflight(self) -> None:
        source = _CombinedSource()
        source.read_started = asyncio.Event()
        source.read_release = asyncio.Event()
        dependencies = _Dependencies(_state(owner="old"))
        service = TaskSourceWritebackService(
            None,
            _registry(source),
            dependencies=SimpleNamespace(
                get_state=dependencies.get_state,
                save_state=dependencies.save_state,
                load_states=dependencies.load_states,
                save_states=dependencies.save_states,
            ),
        )

        service.schedule(_state(owner="old", updated_at=1))
        await asyncio.wait_for(source.read_started.wait(), timeout=0.2)
        dependencies.state = _state(owner="new", updated_at=2)
        service.schedule(_state(owner="new", updated_at=2))
        source.read_release.set()

        await asyncio.gather(*list(service._tasks.values()))

        self.assertEqual(source.write_calls, 1)
        self.assertEqual(
            source.writes,
            [("new", "implementation_active")],
        )
        self.assertEqual(dependencies.state.current_owner, "new")

    async def test_provider_feedback_never_restores_stale_canonical_owner(self) -> None:
        source = _CombinedSource()
        dependencies = _Dependencies(_state(owner="new", updated_at=20))
        service = TaskSourceWritebackService(
            None,
            _registry(source),
            dependencies=SimpleNamespace(
                get_state=dependencies.get_state,
                save_state=dependencies.save_state,
                load_states=dependencies.load_states,
                save_states=dependencies.save_states,
            ),
        )
        stale = _state(owner="old", updated_at=10)
        snapshot = TaskSourceSnapshot(
            identity=stale.source_identity.model_copy(
                update={"revision": "r2"}
            ),
            labels=("owner::old",),
        )

        saved = service._save_snapshot(stale, snapshot)

        self.assertEqual(saved.current_owner, "new")
        self.assertEqual(dependencies.state.current_owner, "new")
        self.assertEqual(
            dependencies.state.source_identity.revision,
            "r2",
        )

    async def test_retry_is_bounded_and_reuses_one_coordinator_task(self) -> None:
        source = _CombinedSource()
        source.failures = 2
        dependencies = _Dependencies(_state())
        service = TaskSourceWritebackService(
            None,
            _registry(source),
            dependencies=SimpleNamespace(
                get_state=dependencies.get_state,
                save_state=dependencies.save_state,
                load_states=dependencies.load_states,
                save_states=dependencies.save_states,
            ),
        )

        with patch(
            "codex_web.services.task_source_runtime.asyncio.sleep",
            new=AsyncMock(),
        ):
            service.schedule(_state(owner="retry"))
            tasks = list(service._tasks.values())
            self.assertEqual(len(tasks), 1)
            await asyncio.gather(*tasks)

        self.assertEqual(source.write_calls, 1)
        self.assertEqual(service.status()["retried"], 2)
        self.assertEqual(service.status()["activeTasks"], 0)


class GitLabCombinedWritebackTests(unittest.IsolatedAsyncioTestCase):
    def _issue(self, labels: list[str]) -> dict:
        return {
            "references": {"full": "group/project#1"},
            "iid": 1,
            "title": "Task",
            "state": "opened",
            "updated_at": "r1",
            "labels": labels,
            "web_url": "https://gitlab.example/group/project/-/issues/1",
        }

    async def test_unchanged_owner_and_stage_reads_once_and_does_not_mutate(self) -> None:
        client = _GitLabClient(
            self._issue(
                ["owner::alice", "status::in progress", "priority::P1"]
            )
        )
        source = GitLabTaskSource(
            "https://gitlab.example/api/v4",
            "token",
            client=client,
        )
        state = _state(owner="alice", stage="implementation_active")
        dependencies = _Dependencies(state)
        service = TaskSourceWritebackService(
            None,
            _registry(source),
            dependencies=SimpleNamespace(
                get_state=dependencies.get_state,
                save_state=dependencies.save_state,
                load_states=dependencies.load_states,
                save_states=dependencies.save_states,
            ),
        )

        await service.sync(state)

        self.assertEqual(client.read_calls, 1)
        self.assertEqual(client.write_calls, 0)
        self.assertEqual(service.status()["providerReads"], 1)
        self.assertEqual(service.status()["providerWrites"], 0)

    async def test_owner_and_stage_change_use_one_read_and_one_combined_update(self) -> None:
        client = _GitLabClient(
            self._issue(
                ["owner::old", "status::in progress", "priority::P1"]
            )
        )
        source = GitLabTaskSource(
            "https://gitlab.example/api/v4",
            "token",
            client=client,
        )
        state = _state(
            owner="new",
            stage="ready_for_validation",
        )
        dependencies = _Dependencies(state)
        service = TaskSourceWritebackService(
            None,
            _registry(source),
            dependencies=SimpleNamespace(
                get_state=dependencies.get_state,
                save_state=dependencies.save_state,
                load_states=dependencies.load_states,
                save_states=dependencies.save_states,
            ),
        )

        await service.sync(state)

        self.assertEqual(client.read_calls, 1)
        self.assertEqual(client.write_calls, 1)
        payload = client.payloads[0]
        self.assertIn("owner::new", payload["labels"])
        self.assertIn(
            "status::awaiting confirmation",
            payload["labels"],
        )
        self.assertIn("priority::P1", payload["labels"])


if __name__ == "__main__":
    unittest.main()
