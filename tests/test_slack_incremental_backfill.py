from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import BotBinding, BotConnection, BotReplyTarget
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.slack_provider import SlackProviderService
from codex_web.storage.slack_backfill import SlackBackfillStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Routing:
    def __init__(self) -> None:
        self.messages = []

    async def handle_inbound(self, message):
        self.messages.append(message)
        return {"ok": True, "threadId": message.external_thread_id}


class _PagedSlack:
    def __init__(self) -> None:
        self.history_calls = []
        self.reply_calls = []
        self.block_history: asyncio.Event | None = None
        self.release_history: asyncio.Event | None = None
        self.rate_limited = False

    async def history(
        self,
        token,
        channel,
        *,
        oldest,
        limit=50,
        cursor=None,
    ):
        self.history_calls.append(
            {
                "channel": channel,
                "oldest": oldest,
                "limit": limit,
                "cursor": cursor,
            }
        )
        if self.block_history is not None:
            self.block_history.set()
            assert self.release_history is not None
            await self.release_history.wait()
        if self.rate_limited:
            return {
                "ok": False,
                "error": "ratelimited",
                "_http_status": 429,
                "_retry_after": 60.0,
            }
        now = time.time()
        if cursor == "page-2":
            return {
                "ok": True,
                "messages": [
                    {
                        "ts": f"{now - 3:.6f}",
                        "user": "U1",
                        "text": "third",
                    },
                    {
                        "ts": f"{now - 4:.6f}",
                        "user": "U1",
                        "text": "fourth",
                    },
                ],
                "response_metadata": {"next_cursor": ""},
            }
        return {
            "ok": True,
            "messages": [
                {
                    "ts": f"{now - 1:.6f}",
                    "user": "U1",
                    "text": "first",
                },
                {
                    "ts": f"{now - 2:.6f}",
                    "user": "U1",
                    "text": "second",
                },
            ],
            "response_metadata": {"next_cursor": "page-2"},
        }

    async def replies(
        self,
        token,
        channel,
        thread_ts,
        *,
        oldest,
        limit=50,
        cursor=None,
    ):
        self.reply_calls.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "oldest": oldest,
                "limit": limit,
                "cursor": cursor,
            }
        )
        return {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }


class _WebhookSecurity:
    async def verify_slack(self, request, body) -> None:
        return None


def _connection() -> BotConnection:
    return BotConnection(
        id="slack-1",
        name="Slack",
        provider="slack",
        project_id="home",
        bot_token="test-token",
        default_external_conversation_id="C1",
        default_external_name="#general",
        created_at=1.0,
        updated_at=1.0,
    )


def _binding() -> BotBinding:
    return BotBinding(
        id="binding-1",
        provider="slack",
        external_conversation_id="C1",
        thread_id="thread-1",
        project_id="home",
        connection_id="slack-1",
        created_at=1.0,
        updated_at=1.0,
    )


def _target() -> BotReplyTarget:
    return BotReplyTarget(
        thread_id="thread-1",
        provider="slack",
        external_conversation_id="C1",
        external_thread_id="111.22",
        message_id="111.22",
        updated_at=1.0,
    )


def _service(
    root: Path,
    *,
    slack=None,
    routing=None,
    telemetry=None,
    store=None,
    bindings=None,
    targets=None,
) -> SlackProviderService:
    connection = _connection()
    telemetry = telemetry or BotRuntimeTelemetry(
        events_file=root / "events.jsonl"
    )
    store = store or SlackBackfillStore(
        SQLiteStateStore(root / "state.sqlite3")
    )
    binding_values = bindings if bindings is not None else []
    binding_service = SimpleNamespace(
        for_project=lambda provider, project_id: [
            item
            for item in binding_values
            if item.provider == provider
            and item.project_id == project_id
        ],
        load_bindings=lambda: (_ for _ in ()).throw(
            AssertionError("full binding scan must not be used")
        ),
        first_for_connection=lambda provider, channel: None,
    )
    if targets is None:
        targets = SimpleNamespace(
            active_reply_target_for_binding=lambda binding: None,
            reply_target_for_binding=lambda binding: None,
            delivery_target_for_binding=lambda binding: None,
        )
    return SlackProviderService(
        slack_client=slack or _PagedSlack(),
        routing_service=routing or _Routing(),
        connections=SimpleNamespace(
            load_connections=lambda: [connection],
            runtime_actor=lambda project_id: SimpleNamespace(
                organization_id="local",
                workspace_id="default",
            ),
        ),
        bindings=binding_service,
        targets=targets,
        presentation=SimpleNamespace(
            strip_slack_mentions=lambda value: str(value).strip(),
        ),
        telemetry=telemetry,
        webhook_security=_WebhookSecurity(),
        backfill_store=store,
    )


class SlackIncrementalBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_recent_message_lookup_reads_only_bounded_tail(self) -> None:
        telemetry = BotRuntimeTelemetry(
            events_file=self.root / "large-events.jsonl"
        )
        payload = "x" * 180
        with telemetry.events_file.open("w") as handle:
            for index in range(20_000):
                handle.write(
                    json.dumps(
                        {
                            "provider": "slack",
                            "message_id": str(index),
                            "padding": payload,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            handle.write("{ malformed tail record\n")

        service = _service(
            self.root,
            telemetry=telemetry,
        )
        values = service._recent_inbound_message_ids()
        metrics = telemetry.recent_metrics()

        self.assertIn("19999", values)
        self.assertLess(
            metrics["bytesRead"],
            metrics["fileSize"],
        )
        self.assertLessEqual(
            metrics["validEvents"],
            300,
        )

    def test_thread_target_discovery_uses_exact_keyed_lookups(self) -> None:
        target = _target()
        loads = []

        def forbidden():
            loads.append(True)
            raise AssertionError("full target registry load")

        targets = SimpleNamespace(
            active_reply_target_for_binding=lambda binding: target,
            reply_target_for_binding=lambda binding: target,
            delivery_target_for_binding=lambda binding: target,
            load_reply_targets=forbidden,
            load_delivery_targets=forbidden,
            load_active_turns=forbidden,
        )
        service = _service(
            self.root,
            bindings=[_binding()],
            targets=targets,
        )

        values = service._thread_targets({"home"})

        self.assertEqual(loads, [])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0][1:], ("C1", "111.22"))

    async def test_restart_resumes_provider_cursor_without_history_gap(self) -> None:
        state_store = SQLiteStateStore(
            self.root / "resume.sqlite3"
        )
        store = SlackBackfillStore(state_store)
        first_slack = _PagedSlack()
        first_routing = _Routing()
        service = _service(
            self.root,
            slack=first_slack,
            routing=first_routing,
            store=store,
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_BACKFILL_MAX_CALLS_PER_CYCLE": "1",
                "CODEX_WEB_SLACK_BACKFILL_MAX_CYCLE_SECONDS": "30",
            },
            clear=False,
        ):
            await service.run_backfill_cycle()

        key = "home:slack-1:history:C1"
        first_checkpoint = store.load().checkpoints[key]
        self.assertEqual(
            first_checkpoint.provider_cursor,
            "page-2",
        )
        self.assertEqual(first_checkpoint.watermark, 0.0)
        self.assertGreater(
            first_checkpoint.pending_watermark,
            0.0,
        )
        pinned_oldest = first_checkpoint.scan_oldest
        self.assertIsNotNone(pinned_oldest)

        second_slack = _PagedSlack()
        second_routing = _Routing()
        restarted = _service(
            self.root,
            slack=second_slack,
            routing=second_routing,
            store=store,
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_BACKFILL_MAX_CALLS_PER_CYCLE": "1",
                "CODEX_WEB_SLACK_BACKFILL_MAX_CYCLE_SECONDS": "30",
            },
            clear=False,
        ):
            await restarted.run_backfill_cycle()

        self.assertEqual(
            second_slack.history_calls[0]["cursor"],
            "page-2",
        )
        self.assertAlmostEqual(
            float(second_slack.history_calls[0]["oldest"]),
            float(pinned_oldest),
            places=5,
        )
        completed = store.load()
        checkpoint = completed.checkpoints[key]
        self.assertIsNone(checkpoint.provider_cursor)
        self.assertGreater(checkpoint.watermark, 0.0)
        self.assertIsNotNone(
            completed.last_successful_completion_at
        )
        self.assertEqual(len(first_routing.messages), 2)
        self.assertEqual(len(second_routing.messages), 2)

    async def test_duplicate_cycle_trigger_is_coalesced(self) -> None:
        state_store = SQLiteStateStore(
            self.root / "coalesce.sqlite3"
        )
        store = SlackBackfillStore(state_store)
        slack = _PagedSlack()
        slack.block_history = asyncio.Event()
        slack.release_history = asyncio.Event()
        service = _service(
            self.root,
            slack=slack,
            store=store,
        )

        first = asyncio.create_task(
            service.run_backfill_cycle()
        )
        await asyncio.wait_for(
            slack.block_history.wait(),
            timeout=1.0,
        )
        await service.run_backfill_cycle()
        slack.release_history.set()
        await first

        self.assertEqual(len(slack.history_calls), 1)
        self.assertEqual(
            store.load().coalesced_cycles,
            1,
        )

    async def test_cancellation_keeps_checkpoint_truthful(self) -> None:
        state_store = SQLiteStateStore(
            self.root / "cancel.sqlite3"
        )
        store = SlackBackfillStore(state_store)
        slack = _PagedSlack()
        slack.block_history = asyncio.Event()
        slack.release_history = asyncio.Event()
        service = _service(
            self.root,
            slack=slack,
            store=store,
        )

        task = asyncio.create_task(
            service.run_backfill_cycle()
        )
        await asyncio.wait_for(
            slack.block_history.wait(),
            timeout=1.0,
        )
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        state = store.load()
        self.assertIsNotNone(state.last_completion_at)
        self.assertIsNone(
            state.last_successful_completion_at
        )
        self.assertEqual(state.checkpoints, {})

    async def test_rate_limit_backoff_survives_restart(self) -> None:
        state_store = SQLiteStateStore(
            self.root / "rate.sqlite3"
        )
        store = SlackBackfillStore(state_store)
        slack = _PagedSlack()
        slack.rate_limited = True
        service = _service(
            self.root,
            slack=slack,
            store=store,
        )

        await service.run_backfill_cycle()

        state = store.load()
        self.assertEqual(state.rate_limit_failures, 1)
        self.assertIsNotNone(state.cooldown_until)

        restarted = _service(
            self.root,
            slack=_PagedSlack(),
            store=store,
        )
        self.assertGreater(
            restarted.cooldown_remaining_seconds(),
            0.0,
        )

    async def test_blocking_discovery_work_does_not_starve_event_loop(self) -> None:
        service = _service(self.root)
        original = service._recent_inbound_message_ids

        def slow_recent():
            time.sleep(0.15)
            return original()

        service._recent_inbound_message_ids = slow_recent
        started = time.perf_counter()
        task = asyncio.create_task(
            service.run_backfill_cycle()
        )
        await asyncio.sleep(0.01)
        elapsed = time.perf_counter() - started
        await task

        self.assertLess(elapsed, 0.10)


if __name__ == "__main__":
    unittest.main()
