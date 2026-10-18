from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.autonomy import AutonomyControl, AutonomyScopedPauseCreate
from codex_web.autonomy_policy import AutonomyPolicy
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.autonomy import AutonomyService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class WatchdogRecoveryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, kind="work_item_sla", *, handoff=False):
        state = WorkItemState(ref="example/repo#1", project_id="project-a",
            current_owner="james", current_stage="implementation_active",
            last_meaningful_update_at=1, created_at=1, updated_at=1)
        if handoff:
            state.handoff = WorkItemHandoff(from_agent="alice", to_agent="james",
                requested_at=time.time(), status="pending")
        if kind == "release_gate":
            state.current_owner = "release manager"
            state.current_stage = "ready_for_validation"
        old = SimpleNamespace(thread_id="predecessor", thread_name="James")
        new = SimpleNamespace(thread_id="successor", thread_name="James")
        return SimpleNamespace(
            load_gitlab_routing_settings=lambda: SimpleNamespace(enabled=True,
                projects={"project-a": SimpleNamespace(enabled=True)}),
            state=state,
            load_work_item_states=lambda: {state.ref: state},
            gitlab_token_for_project=lambda _: "unused-test-credential",
            gitlab_group_path=lambda _: "example",
            gitlab_group_issues=AsyncMock(return_value=[]),
            append_bot_event=Mock(), append_work_item_event=Mock(),
            coerce_owner=lambda v: v.lower() if v else None,
            owner_queue_agents=("james",), handoff_coordination_channel=None,
            binding_for_agent=lambda *_args, **_kw: old,
            orchestrator_binding=lambda _: old, binding_prefix=lambda _: "James",
            replace_nonperforming_thread=AsyncMock(return_value=new),
            dispatch_event=AsyncMock(return_value={"ok": True, "queued": True}),
            release_stale_active_turn=Mock(),
            thread_is_active=lambda _: False, thread_queue_depth=lambda _: 0,
            thread_recently_active=lambda _: False,
            watchdog_dispatch_allowed=lambda _: True, record_watchdog_dispatch=Mock(),
            handoff_timeout_seconds=lambda: 300,
            handoff_delivery_timeout_seconds=lambda: 300,
            release_validation_sla_seconds=lambda: 1,
            work_item_sla_threshold_seconds=lambda _: 1,
            owner_activity_timestamp=lambda _: 1,
            orchestrator_watchdog_candidates=lambda _: [("stalled", state, 100)],
            split_brain_watchdog_candidates=lambda _: [(state, ["conflicting_owner"])],
            format_orchestrator_watchdog_prompt=lambda *_: "continue original work",
            format_split_brain_watchdog_prompt=lambda *_: "reconcile existing owners",
            work_item_dispatch_text=lambda _: "continue original work",
            project_scope=lambda _: ("org-a", "workspace-a"),
        )

    def service(self, runtime, folder, denial=None):
        store = AutonomyStateStore(SQLiteStateStore(Path(folder) / "state.sqlite3"))
        controller = AutonomyController(store)
        settings = {"mode": "active", "cooldown_seconds": 0, "reasoning_backoff_seconds": 0}
        if denial in ("paused", "killed"):
            settings["mode"] = denial
        elif denial in ("dry_run", "simulation"):
            settings[denial] = True
        elif denial == "observe":
            settings["policy"] = AutonomyPolicy(level="observe")
        store.set_control(AutonomyControl(**settings), actor_id="operator")
        if denial == "project_pause":
            controller.add_scoped_pause(AutonomyScopedPauseCreate(scope="project",
                scope_id="project-a", reason="maintenance"), actor_id="operator")

        async def ingest(**kw):
            return SimpleNamespace(inserted=True, event=CanonicalEventEnvelope(
                event_id="event-" + uuid.uuid4().hex, event_type=kw["event_type"],
                occurred_at=time.time(), source=kw["source"], correlation_id="test",
                tenant_id=kw["tenant_id"], workspace_id=kw["workspace_id"],
                payload=kw["payload"]))
        return AutonomyService(runtime=runtime, controller=controller,
            canonical_events=SimpleNamespace(ingest=ingest))

    async def test_controller_denials_prevent_recovery_and_dispatch_for_every_watchdog(self):
        for kind in ("owner_work", "release_gate", "orchestrator", "split_brain", "work_item_sla", "handoff"):
            for denial in ("paused", "killed", "project_pause", "observe", "dry_run", "simulation"):
                with self.subTest(kind=kind, denial=denial), tempfile.TemporaryDirectory() as folder:
                    runtime = self.runtime(kind, handoff=kind == "handoff")
                    service = self.service(runtime, folder, denial)
                    cycle = "work_item_sla" if kind == "handoff" else kind
                    await getattr(service, "run_" + cycle + "_cycle")()
                    runtime.replace_nonperforming_thread.assert_not_awaited()
                    runtime.dispatch_event.assert_not_awaited()
                    runtime.release_stale_active_turn.assert_not_called()
                    runtime.record_watchdog_dispatch.assert_not_called()

    async def test_allowed_watchdogs_recover_and_dispatch_actual_successor_once(self):
        for kind in ("owner_work", "release_gate", "orchestrator", "split_brain", "work_item_sla", "handoff"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                runtime = self.runtime(kind, handoff=kind == "handoff")
                service = self.service(runtime, folder)
                cycle = "work_item_sla" if kind == "handoff" else kind
                await getattr(service, "run_" + cycle + "_cycle")()
                runtime.replace_nonperforming_thread.assert_awaited_once()
                runtime.dispatch_event.assert_awaited_once()
                self.assertEqual(runtime.dispatch_event.await_args.args[0].thread_id, "successor")
                self.assertEqual(runtime.append_bot_event.call_args.args[0]["thread_id"], "successor")

    async def test_reasoning_retry_cannot_repeat_failed_worker_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime()
            runtime.replace_nonperforming_thread.side_effect = RuntimeError("unknown recovery outcome")
            service = self.service(runtime, folder)
            await service.run_work_item_sla_cycle()
            runtime.replace_nonperforming_thread.assert_awaited_once()
            runtime.dispatch_event.assert_not_awaited()
            runtime.record_watchdog_dispatch.assert_not_called()
            self.assertEqual(service.controller.status()["recent_cycles"][0]["reasoning_attempts"], 2)

    async def test_sla_dispatch_keeps_work_item_and_idle_admission_context(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime()
            service = self.service(runtime, folder)
            await service.run_work_item_sla_cycle()
            context = runtime.dispatch_event.await_args.kwargs
            self.assertEqual(context["work_item_ref"], "example/repo#1")
            self.assertTrue(context["require_idle"])

    async def test_work_item_resource_pause_prevents_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime()
            runtime.state.resource_ids = ["data-repository"]
            service = self.service(runtime, folder)
            service.controller.add_scoped_pause(AutonomyScopedPauseCreate(
                scope="resource", scope_id="data-repository", reason="repository hold"),
                actor_id="operator")
            await service.run_work_item_sla_cycle()
            runtime.replace_nonperforming_thread.assert_not_awaited()
            runtime.dispatch_event.assert_not_awaited()

    async def test_newly_busy_successor_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime()
            runtime.thread_queue_depth = lambda tid: int(tid == "successor")
            service = self.service(runtime, folder)
            await service.run_work_item_sla_cycle()
            runtime.replace_nonperforming_thread.assert_awaited_once()
            runtime.dispatch_event.assert_not_awaited()
            runtime.record_watchdog_dispatch.assert_not_called()

    async def test_sla_binding_and_liveness_reads_leave_loop_available(self):
        for callback in ("binding_for_agent", "thread_is_active", "thread_queue_depth", "thread_recently_active"):
            for handoff in (False, True):
                with self.subTest(callback=callback, handoff=handoff):
                    runtime = self.runtime(handoff=handoff)
                    original = getattr(runtime, callback)
                    started, release = threading.Event(), threading.Event()
                    thread_ids = []
                    loop_thread = threading.get_ident()
                    def blocked(*args, **kw):
                        thread_ids.append(threading.get_ident())
                        started.set()
                        if not release.wait(3):
                            raise RuntimeError("watchdog blocked observer loop")
                        return original(*args, **kw)
                    setattr(runtime, callback, blocked)
                    task = asyncio.create_task(AutonomyService(runtime=runtime).run_work_item_sla_cycle())
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 2))
                        self.assertFalse(task.done())
                        self.assertNotEqual(thread_ids[0], loop_thread)
                        # A lease/health callback can run while storage waits.
                        observed = asyncio.Event()
                        asyncio.get_running_loop().call_soon(observed.set)
                        await asyncio.wait_for(observed.wait(), .5)
                    finally:
                        release.set()
                    await task
