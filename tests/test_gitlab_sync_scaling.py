from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from codex_web.identity import TenantScope
from codex_web.models import (
    TaskSourceIdentity,
    WorkItemHandoff,
    WorkItemState,
)
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_work_items import (
    TaskSourceWorkItemProjector,
)
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_items import WorkItemService
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_item_list_index import WorkItemListIndex
from codex_web.storage.work_item_source_identity_index import (
    WorkItemSourceIdentityIndex,
)


API_BASE = "https://gitlab.example/api/v4"
PROJECT_ID = "project-a"
SCOPE = TenantScope(organization_id="org-a", workspace_id="ws-a")
IMPORT_COUNT = 95


class _CountingSQLiteStateStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        self.operation_counts = {
            "record_get": 0,
            "record_apply": 0,
            "record_replace": 0,
            "record_items": 0,
        }
        super().__init__(path)

    def reset_operation_counts(self) -> None:
        for key in self.operation_counts:
            self.operation_counts[key] = 0

    def record_get(self, namespace, key):
        self.operation_counts["record_get"] += 1
        return super().record_get(namespace, key)

    def record_apply(self, namespace, *, upserts, deletes=()):
        self.operation_counts["record_apply"] += 1
        return super().record_apply(
            namespace,
            upserts=upserts,
            deletes=deletes,
        )

    def record_replace(self, namespace, records):
        self.operation_counts["record_replace"] += 1
        return super().record_replace(namespace, records)

    def record_items(self, namespace):
        self.operation_counts["record_items"] += 1
        return super().record_items(namespace)


class _ProjectionHost:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {
        "quinn",
        "release manager",
        "orchestrator",
    }

    def __init__(self, root: Path, existing_count: int) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.store = _CountingSQLiteStateStore(root / "state.sqlite3")
        self.runtime = RuntimeStateRepositories(
            self.store,
            thread_settings_file=root / "thread_settings.json",
            active_turns_file=root / "active_turns.json",
            work_item_states_file=root / "work_item_states.json",
        )
        self.list_index = WorkItemListIndex(self.store)
        self.source_index = WorkItemSourceIdentityIndex(self.store)
        self.load_calls = 0
        self.bulk_save_calls = 0
        self.keyed_get_calls = 0
        self.keyed_put_calls = 0
        self.source_lookup_calls = 0
        self._seed(existing_count)
        self._baseline_list_metrics = self.list_index.metrics()
        self._baseline_source_metrics = self.source_index.metrics()
        self.store.reset_operation_counts()

    @staticmethod
    def _identity(external_id: str, revision: str | None = None):
        return TaskSourceIdentity(
            source_type="gitlab",
            source_instance=API_BASE,
            external_id=external_id,
            external_url=(
                "https://gitlab.example/"
                + external_id.replace("#", "/-/issues/")
            ),
            revision=revision,
        )

    def _seed(self, existing_count: int) -> None:
        states: dict[str, WorkItemState] = {}
        for index in range(existing_count):
            external_id = f"unrelated/project#{index}"
            states[f"unrelated-{index}"] = WorkItemState(
                ref=f"unrelated-{index}",
                organization_id=SCOPE.organization_id,
                workspace_id=SCOPE.workspace_id,
                project_id=PROJECT_ID,
                source_identity=self._identity(external_id),
                current_stage="implementation_active",
                last_meaningful_update_at=1.0,
                updated_at=1.0,
                created_at=1.0,
            )

        # Direct-ref existing snapshots: unchanged, changed, stale and
        # accepted-handoff cases.
        for index in range(35):
            external_id = f"group/project#{index}"
            revision = (
                "2026-09-21T08:00:00Z"
                if index < 25
                else "2026-09-21T10:00:00Z"
            )
            handoff = None
            stage = "implementation_active"
            owner = "carl"
            status = "status::in progress"
            if 30 <= index < 35:
                handoff = WorkItemHandoff(
                    from_agent="carl",
                    to_agent="quinn",
                    requested_at=1.0,
                    acknowledged_at=2.0,
                    status="accepted",
                )
                stage = "validation_running"
                owner = "quinn"
                status = "status::awaiting confirmation"
            states[external_id] = WorkItemState(
                ref=external_id,
                organization_id=SCOPE.organization_id,
                workspace_id=SCOPE.workspace_id,
                project_id=PROJECT_ID,
                source_identity=self._identity(external_id, revision),
                current_owner=owner,
                current_stage=stage,
                handoff=handoff,
                status_label=status,
                last_meaningful_update_at=1.0,
                last_gitlab_event_at=(
                    1_795_000_000.0 if 25 <= index < 30 else 1.0
                ),
                updated_at=1.0,
                created_at=1.0,
            )

        # Source-identity matches whose canonical ref intentionally differs.
        for index in range(35, 40):
            external_id = f"group/project#{index}"
            ref = f"canonical-alias-{index}"
            states[ref] = WorkItemState(
                ref=ref,
                organization_id=SCOPE.organization_id,
                workspace_id=SCOPE.workspace_id,
                project_id=PROJECT_ID,
                source_identity=self._identity(
                    external_id,
                    "2026-09-21T08:00:00Z",
                ),
                current_owner="carl",
                current_stage="implementation_active",
                last_meaningful_update_at=1.0,
                last_gitlab_event_at=1.0,
                updated_at=1.0,
                created_at=1.0,
            )

        self.store.record_replace(
            "work_item_states",
            {
                key: value.model_dump(mode="json")
                for key, value in states.items()
            },
        )
        self.list_index.rebuild(states)
        self.source_index.rebuild(states)

    def _load_work_item_states(self):
        self.load_calls += 1
        return self.runtime.work_item_states.load()

    def _save_work_item_states(self, states):
        self.bulk_save_calls += 1
        self.runtime.work_item_states.save(states)
        self.list_index.rebuild(states)
        self.source_index.rebuild(states)

    def _get_work_item_state_record(self, ref):
        self.keyed_get_calls += 1
        return self.runtime.work_item_states.get(ref)

    def _get_work_item_state_by_source_identity(self, identity):
        self.source_lookup_calls += 1
        ref = self.source_index.ref_for_identity(identity)
        return self.runtime.work_item_states.get(ref) if ref else None

    def _put_work_item_state_record(self, state):
        self.keyed_put_calls += 1
        self.runtime.work_item_states.put(state.ref, state)
        self.source_index.upsert(state)
        self.list_index.upsert(state)
        return state

    def _leading_owner_cue_in_action(self, _action):
        return None

    def _resource_ids_for_project(self, _project_id):
        return ["resource-repo"]

    def _load_projects(self):
        return [
            SimpleNamespace(
                id=PROJECT_ID,
                organization_id=SCOPE.organization_id,
                workspace_id=SCOPE.workspace_id,
            )
        ]

    def result_metrics(self, latencies: list[float], total: float):
        list_metrics = self.list_index.metrics()
        source_metrics = self.source_index.metrics()
        projector_metrics = self.projector.metrics()
        ordered = sorted(latencies)

        def percentile(fraction: float) -> float:
            if not ordered:
                return 0.0
            index = min(
                len(ordered) - 1,
                max(0, int(round((len(ordered) - 1) * fraction))),
            )
            return ordered[index]

        return {
            "total_seconds": total,
            "projection_latency_seconds": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "max": max(ordered, default=0.0),
            },
            "keyed_gets": projector_metrics["keyed_gets"],
            "keyed_saves": projector_metrics["keyed_saves"],
            "source_identity_index_lookups": (
                projector_metrics["source_identity_index_lookups"]
            ),
            "collection_loads": self.load_calls,
            "bulk_saves": self.bulk_save_calls,
            "list_index_upserts": (
                list_metrics["upserts"]
                - self._baseline_list_metrics["upserts"]
            ),
            "list_index_rebuilds": (
                list_metrics["rebuilds"]
                - self._baseline_list_metrics["rebuilds"]
            ),
            "source_index_upserts": (
                source_metrics["upserts"]
                - self._baseline_source_metrics["upserts"]
            ),
            "source_index_rebuilds": (
                source_metrics["rebuilds"]
                - self._baseline_source_metrics["rebuilds"]
            ),
            "sqlite": dict(self.store.operation_counts),
        }


def _snapshots() -> list[TaskSourceSnapshot]:
    values = []
    for index in range(IMPORT_COUNT):
        if index < 20:
            revision = "2026-09-21T08:00:00Z"  # unchanged-ish
            title = f"Issue {index}"
            labels = ("owner::carl", "status::in progress")
        elif index < 25:
            revision = "2026-09-21T09:00:00Z"  # changed
            title = f"Changed issue {index}"
            labels = ("owner::carl", "status::blocked")
        elif index < 30:
            revision = "2026-09-21T07:00:00Z"  # stale
            title = f"Stale issue {index}"
            labels = ("owner::someone-else", "status::blocked")
        elif index < 35:
            revision = "2026-09-21T09:00:00Z"  # accepted-handoff protection
            title = f"Handoff issue {index}"
            labels = (
                "owner::carl",
                "status::in progress",
            )
        else:
            revision = "2026-09-21T09:00:00Z"
            title = f"Issue {index}"
            labels = ("owner::carl", "status::in progress")
        values.append(
            TaskSourceSnapshot(
                identity=TaskSourceIdentity(
                    source_type="gitlab",
                    source_instance=API_BASE,
                    external_id=f"group/project#{index}",
                    external_url=(
                        f"https://gitlab.example/group/project/-/issues/{index}"
                    ),
                    revision=revision,
                ),
                title=title,
                source_state="opened",
                labels=labels,
            )
        )
    return values


class GitLabSyncScalingQualificationTests(unittest.TestCase):
    def _run_matrix_case(self, existing_count: int):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = _ProjectionHost(root, existing_count)
            machine = WorkItemStateMachine(host)
            projector = TaskSourceWorkItemProjector(host, machine)
            host.projector = projector
            source = GitLabTaskSource(
                API_BASE,
                "fixture-token",
                client=object(),
            )
            latencies = []
            started = time.perf_counter()
            for snapshot in _snapshots():
                item_started = time.perf_counter()
                projector.upsert(
                    source,
                    snapshot,
                    project_id=PROJECT_ID,
                )
                latencies.append(time.perf_counter() - item_started)
            total = time.perf_counter() - started
            return host.result_metrics(latencies, total)

    def test_scaling_matrix_stays_keyed_and_independent_of_unrelated_state(self):
        results = {
            "small": self._run_matrix_case(100),
            "production_like": self._run_matrix_case(1317),
            "large": self._run_matrix_case(5000),
        }

        for name, metrics in results.items():
            with self.subTest(name=name), self._failure_context(results):
                self.assertEqual(metrics["collection_loads"], 0)
                self.assertEqual(metrics["bulk_saves"], 0)
                self.assertEqual(metrics["list_index_rebuilds"], 0)
                self.assertEqual(metrics["source_index_rebuilds"], 0)
                self.assertEqual(metrics["keyed_gets"], IMPORT_COUNT)
                self.assertLessEqual(
                    metrics["source_identity_index_lookups"],
                    IMPORT_COUNT,
                )
                # Stale and protected accepted-handoff snapshots do not write.
                self.assertLessEqual(metrics["keyed_saves"], IMPORT_COUNT)
                self.assertEqual(
                    metrics["list_index_upserts"],
                    metrics["keyed_saves"],
                )
                self.assertEqual(
                    metrics["source_index_upserts"],
                    metrics["keyed_saves"],
                )
                self.assertEqual(
                    metrics["sqlite"]["record_replace"],
                    0,
                    "normal sync performed a collection-wide SQLite replace",
                )

        baseline = results["production_like"]
        large = results["large"]
        for key in (
            "keyed_gets",
            "keyed_saves",
            "source_identity_index_lookups",
            "list_index_upserts",
            "source_index_upserts",
        ):
            self.assertEqual(
                large[key],
                baseline[key],
                f"{key} scaled with unrelated canonical Work Items",
            )

    def test_source_identity_alias_stale_and_handoff_semantics_survive_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            host = _ProjectionHost(Path(temp), 1317)
            machine = WorkItemStateMachine(host)
            projector = TaskSourceWorkItemProjector(host, machine)
            host.projector = projector
            source = GitLabTaskSource(API_BASE, "fixture-token", client=object())

            snapshots = _snapshots()
            alias = projector.upsert(source, snapshots[35], project_id=PROJECT_ID)
            self.assertEqual(alias.ref, "canonical-alias-35")

            stale_before = host.runtime.work_item_states.get("group/project#25")
            stale = projector.upsert(source, snapshots[25], project_id=PROJECT_ID)
            self.assertEqual(stale.current_owner, stale_before.current_owner)
            self.assertEqual(stale.current_stage, stale_before.current_stage)

            protected = projector.upsert(
                source,
                snapshots[30],
                project_id=PROJECT_ID,
            )
            self.assertEqual(protected.current_owner, "quinn")
            self.assertEqual(protected.current_stage, "validation_running")
            self.assertEqual(
                protected.handoff.status if protected.handoff else None,
                "accepted",
            )

    @staticmethod
    def _failure_context(results):
        class _Context:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, _tb):
                if exc is not None and hasattr(exc, "add_note"):
                    exc.add_note(
                        "GitLab sync qualification metrics: "
                        + json.dumps(results, sort_keys=True, default=str)
                    )
                return False

        return _Context()


class GitLabSyncResponsivenessQualificationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_slow_projection_keeps_livez_and_normal_api_responsive(self):
        service = WorkItemService.__new__(WorkItemService)
        service.gitlab = object()
        service.gitlab_dependencies = SimpleNamespace(
            load_routing_settings=lambda: SimpleNamespace(
                projects={
                    PROJECT_ID: SimpleNamespace(enabled=True)
                }
            ),
            token_for_project=lambda _project_id: "fixture-token",
            group_path=lambda _settings: "group",
            api_base_url=API_BASE,
        )
        service.work_items = SimpleNamespace(
            load_projects=lambda: [
                SimpleNamespace(
                    id=PROJECT_ID,
                    organization_id=SCOPE.organization_id,
                    workspace_id=SCOPE.workspace_id,
                )
            ]
        )

        snapshots = _snapshots()[:12]
        provider_calls = {"discover": 0}

        class _Source:
            def __init__(self, *_args, **_kwargs):
                pass

            async def discover(self, *, scope):
                self.scope = scope
                provider_calls["discover"] += 1
                return snapshots

        class _SlowProjector:
            def __init__(self):
                self.calls = 0
                self.latencies = []

            def upsert(self, _source, snapshot, *, project_id):
                del project_id
                started = time.perf_counter()
                time.sleep(0.035)
                self.latencies.append(time.perf_counter() - started)
                self.calls += 1
                return SimpleNamespace(ref=snapshot.identity.external_id)

        projector = _SlowProjector()
        service.task_source_projector = projector

        app = FastAPI()

        @app.get("/api/livez")
        async def livez():
            return {"live": True}

        @app.get("/api/qualification-ping")
        async def ping():
            return {"ok": True}

        transport = httpx.ASGITransport(app=app)
        latencies = {"livez": [], "normal": []}
        loop_lag = []
        stop = asyncio.Event()

        async def probe(path: str, bucket: str):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                while not stop.is_set():
                    started = time.perf_counter()
                    response = await client.get(path)
                    latencies[bucket].append(
                        time.perf_counter() - started
                    )
                    self.assertEqual(response.status_code, 200)
                    await asyncio.sleep(0.004)

        async def lag_probe():
            target = asyncio.get_running_loop().time() + 0.005
            while not stop.is_set():
                await asyncio.sleep(max(0.0, target - asyncio.get_running_loop().time()))
                now = asyncio.get_running_loop().time()
                loop_lag.append(max(0.0, now - target))
                target = now + 0.005

        probes = [
            asyncio.create_task(probe("/api/livez", "livez")),
            asyncio.create_task(
                probe("/api/qualification-ping", "normal")
            ),
            asyncio.create_task(lag_probe()),
        ]
        try:
            with patch(
                "codex_web.services.work_items.GitLabTaskSource",
                _Source,
            ):
                started = time.perf_counter()
                result = await service._sync_from_gitlab_async(SCOPE)
                total = time.perf_counter() - started
        finally:
            stop.set()
            await asyncio.gather(*probes)

        report = {
            "total_sync_seconds": total,
            "provider_calls": provider_calls["discover"],
            "projection_count": projector.calls,
            "projection_p95_seconds": sorted(projector.latencies)[
                max(0, int(len(projector.latencies) * 0.95) - 1)
            ],
            "livez_max_seconds": max(latencies["livez"], default=0.0),
            "normal_api_max_seconds": max(
                latencies["normal"],
                default=0.0,
            ),
            "event_loop_lag_max_seconds": max(loop_lag, default=0.0),
        }

        try:
            self.assertEqual(result["processed"], len(snapshots))
            self.assertEqual(provider_calls["discover"], 1)
            self.assertGreater(len(latencies["livez"]), 10)
            self.assertGreater(len(latencies["normal"]), 10)
            self.assertLess(
                report["livez_max_seconds"],
                2.0,
                "livez exceeded the qualification budget",
            )
            self.assertLess(
                report["normal_api_max_seconds"],
                2.0,
                "representative API exceeded the qualification budget",
            )
            self.assertLess(
                report["event_loop_lag_max_seconds"],
                0.2,
                "event-loop lag indicates blocking sync work",
            )
        except AssertionError as exc:
            exc.add_note(
                "GitLab responsiveness qualification metrics: "
                + json.dumps(report, sort_keys=True)
            )
            raise


if __name__ == "__main__":
    unittest.main()
