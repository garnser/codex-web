from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request

from codex_web.api.system import build_system_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import BotReplyTarget
from codex_web.services.runtime_diagnostics import RuntimeDiagnosticsService
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


def _target(thread_id: str, index: int) -> BotReplyTarget:
    return BotReplyTarget(
        thread_id=thread_id,
        provider="slack",
        external_conversation_id=f"C{index % 7}",
        external_thread_id=str(index),
        message_id=str(index),
        updated_at=float(index),
    )


class _Ready:
    def is_set(self):
        return True


class _DiagnosticsFixture:
    def __init__(self, root: Path, count: int = 5000) -> None:
        self.store = SQLiteStateStore(root / "state.sqlite3")
        self.reply = ModelMapRepository(
            self.store,
            namespace="bot_reply_targets",
            legacy_path=root / "reply.json",
            model=BotReplyTarget,
        )
        self.delivery = ModelMapRepository(
            self.store,
            namespace="bot_delivery_targets",
            legacy_path=root / "delivery.json",
            model=BotReplyTarget,
        )
        rows = {
            f"slack:C{index % 7}:external:{index}": _target(
                f"thread-{index % 20}",
                index,
            ).model_dump(mode="json")
            for index in range(count)
        }
        self.store.record_replace("bot_reply_targets", rows)
        self.store.record_replace("bot_delivery_targets", rows)
        self.full_reply_loads = 0
        self.full_delivery_loads = 0
        self.bindings = [
            SimpleNamespace(
                thread_id=f"thread-{index}",
                project_id="project-a",
                model_dump=lambda index=index: {
                    "thread_id": f"thread-{index}",
                    "project_id": "project-a",
                },
            )
            for index in range(5)
        ]

    def load_reply(self):
        self.full_reply_loads += 1
        raise AssertionError("default diagnostics loaded all reply targets")

    def load_delivery(self):
        self.full_delivery_loads += 1
        raise AssertionError("default diagnostics loaded all delivery targets")

    def service(self) -> RuntimeDiagnosticsService:
        ready = _Ready()
        return RuntimeDiagnosticsService(
            version=lambda: "test",
            health=lambda: {"ok": True},
            codex=SimpleNamespace(
                ready=ready,
                proc=SimpleNamespace(pid=42),
                last_error=None,
                pending_approvals={},
            ),
            bot_runtime=SimpleNamespace(tasks={}),
            telemetry=SimpleNamespace(snapshot=lambda: {}),
            runtime_policy=SimpleNamespace(
                owner_work_watchdog_interval=lambda: 60.0,
                release_gate_watchdog_interval=lambda: 60.0,
                work_item_sla_watchdog_interval=lambda: 60.0,
                orchestrator_watchdog_interval=lambda: 60.0,
                split_brain_watchdog_interval=lambda: 60.0,
            ),
            supervisor=SimpleNamespace(task_status=lambda: {}),
            thread_message_limit=lambda: 100,
            slack_provider_health=lambda: {
                "intervalSeconds": 15.0,
                "running": True,
                "cooldownRemainingSeconds": 0.0,
                "cooldownUntil": None,
            },
            project_lookup=lambda project_id: SimpleNamespace(
                id=project_id,
                organization_id="org-a",
                workspace_id="ws-a",
            ),
            load_projects=lambda: [],
            load_thread_index=lambda: [],
            load_active_turns=lambda: {},
            load_queues=lambda: {},
            queued_turn_public=lambda item: item,
            queue_tasks={},
            load_connections=lambda: [],
            connection_public=lambda item: item,
            load_bindings=lambda: list(self.bindings),
            binding_public=lambda item: item.model_dump(),
            load_agent_presence=lambda: SimpleNamespace(),
            agent_presence_public=lambda _item: {},
            load_reply_targets=self.load_reply,
            load_delivery_targets=self.load_delivery,
            load_work_item_states=lambda: {},
            count_reply_targets=self.reply.count,
            count_delivery_targets=self.delivery.count,
            page_reply_targets_raw=self.reply.raw_page,
            page_delivery_targets_raw=self.delivery.raw_page,
            work_item_public=lambda item: item,
            recent_events=lambda _limit: [],
        )


class RuntimeDiagnosticsTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = _DiagnosticsFixture(Path(self.temp.name))
        self.service = self.fixture.service()

    def test_default_snapshot_uses_counts_and_bounded_samples(self):
        started = time.perf_counter()
        snapshot = self.service.snapshot()
        elapsed = time.perf_counter() - started

        self.assertEqual(self.fixture.full_reply_loads, 0)
        self.assertEqual(self.fixture.full_delivery_loads, 0)
        self.assertLessEqual(len(snapshot["replyTargets"]), 25)
        self.assertLessEqual(len(snapshot["deliveryTargets"]), 25)
        self.assertEqual(
            snapshot["targetMetadata"]["reply"]["globalTotalCount"],
            5000,
        )
        self.assertEqual(
            snapshot["targetMetadata"]["delivery"]["globalTotalCount"],
            5000,
        )
        self.assertTrue(snapshot["targetMetadata"]["reply"]["truncated"])
        self.assertTrue(snapshot["targetMetadata"]["delivery"]["truncated"])
        self.assertLess(elapsed, 2.0)

    def test_target_inspection_is_cursor_bounded_and_deterministic(self):
        first = self.service.target_page("reply", limit=12)
        second = self.service.target_page(
            "reply",
            after=first["nextCursor"],
            limit=12,
        )

        self.assertEqual(len(first["items"]), 12)
        self.assertEqual(len(second["items"]), 12)
        first_keys = [item["key"] for item in first["items"]]
        second_keys = [item["key"] for item in second["items"]]
        self.assertEqual(first_keys, sorted(first_keys))
        self.assertEqual(second_keys, sorted(second_keys))
        self.assertTrue(set(first_keys).isdisjoint(second_keys))
        self.assertEqual(first["globalTotalCount"], 5000)
        self.assertLessEqual(first["scanned"], self.service.TARGET_SCAN_BUDGET)

    def test_project_filter_happens_on_raw_rows_before_serialization(self):
        page = self.service.target_page(
            "delivery",
            project_id="project-a",
            limit=20,
        )

        self.assertTrue(page["items"])
        self.assertTrue(
            all(
                item["thread_id"]
                in {f"thread-{index}" for index in range(5)}
                for item in page["items"]
            )
        )
        self.assertFalse(page["scopeCountExact"])
        self.assertEqual(page["globalTotalCount"], 5000)
        self.assertEqual(self.fixture.full_delivery_loads, 0)

    def test_malformed_raw_target_is_skipped_without_failing_page(self):
        self.fixture.store.record_apply(
            "bot_reply_targets",
            upserts={
                "000-malformed": "not-a-mapping",
                "001-valid": _target("thread-1", 9999).model_dump(mode="json"),
            },
        )

        page = self.service.target_page("reply", limit=5)

        self.assertTrue(page["items"])
        self.assertTrue(
            all(isinstance(item, dict) for item in page["items"])
        )


class RuntimeDiagnosticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_snapshot_does_not_block_livez(self):
        actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        diagnostics = SimpleNamespace(
            project_lookup=lambda _project_id: SimpleNamespace(
                organization_id="org-a",
                workspace_id="ws-a",
            ),
            snapshot=lambda _project_id=None: (
                time.sleep(0.15) or {"ok": True}
            ),
            target_page=lambda *_args, **_kwargs: {"items": []},
        )
        static_assets = SimpleNamespace(version=lambda: "test")
        health = SimpleNamespace(health=lambda: {"ok": True})
        routing = SimpleNamespace(preview=lambda _payload: {})

        app = FastAPI()

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)

        app.include_router(
            build_system_router(
                static_assets,
                health,
                diagnostics,
                routing,
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            slow = asyncio.create_task(client.get("/api/diagnostics"))
            await asyncio.sleep(0.02)
            started = time.perf_counter()
            live = await client.get("/api/livez")
            live_elapsed = time.perf_counter() - started
            diagnostic = await slow

        self.assertEqual(live.status_code, 200)
        self.assertEqual(diagnostic.status_code, 200)
        self.assertLess(live_elapsed, 0.1)

    async def test_cross_tenant_project_diagnostics_is_hidden(self):
        actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        diagnostics = SimpleNamespace(
            project_lookup=lambda _project_id: SimpleNamespace(
                organization_id="org-b",
                workspace_id="ws-b",
            ),
            snapshot=lambda _project_id=None: {"ok": True},
            target_page=lambda *_args, **_kwargs: {"items": []},
        )
        app = FastAPI()

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)

        app.include_router(
            build_system_router(
                SimpleNamespace(version=lambda: "test"),
                SimpleNamespace(health=lambda: {"ok": True}),
                diagnostics,
                SimpleNamespace(preview=lambda _payload: {}),
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/diagnostics",
                params={"project_id": "project-b"},
            )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
