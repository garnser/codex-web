from __future__ import annotations

import asyncio
import contextlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import server
from codex_web.security import security_boundary_instructions
from codex_web.models import (
    BotBinding,
    BotConnection,
    BotReplyTarget,
    IndexedThread,
    Project,
    QueuedTurn,
    ThreadRunSettings,
    TurnCreate,
    WorkItemAckCreate,
    WorkItemHandoff,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
    WorkItemState,
)


class ThreadDeveloperInstructionsTests(unittest.TestCase):
    def test_slack_backfill_thread_targets_include_delivery_targets(self) -> None:
        connection = BotConnection(
            id="conn-1",
            provider="slack",
            name="Slack",
            project_id="home",
            bot_token="xoxb-token",
            created_at=1,
            updated_at=1,
        )
        binding = BotBinding(
            id="binding-1",
            connection_id="conn-1",
            provider="slack",
            external_conversation_id="C123",
            thread_id="thread-1",
            project_id="home",
            created_at=1,
            updated_at=1,
        )
        target = BotReplyTarget(
            thread_id="thread-1",
            provider="slack",
            external_conversation_id="C123",
            external_thread_id="1782851704.376029",
            message_id="1782851704.376029",
            updated_at=1,
        )
        with (
            patch.object(server, "_load_bot_connections", return_value=[connection]),
            patch.object(server, "_load_bot_bindings", return_value=[binding]),
            patch.object(server, "_load_bot_reply_targets", return_value={}),
            patch.object(server, "_load_bot_delivery_targets", return_value={"slack:C123:1782851704.376029": target}),
            patch.object(server, "_load_active_turns", return_value={}),
        ):
            self.assertEqual(
                server._slack_backfill_thread_targets(),
                [(connection, "C123", "1782851704.376029")],
            )

    def test_base_developer_instructions_strips_repeated_work_item_contract(self) -> None:
        contract = "codex-web structured work-item contract"
        instructions = f"base instructions\n\n{contract}\n\n{contract}"
        with patch.object(server, "_work_item_contract_instructions", return_value=contract):
            cleaned = server._base_developer_instructions("thread-1", instructions)
        self.assertEqual(cleaned, "base instructions")

    def test_resume_thread_persists_base_instructions_but_sends_effective_instructions(self) -> None:
        project = Project(
            id="home",
            name="veridataops",
            path="/home/nbingester/veridataops",
            sandbox="danger-full-access",
            approval_policy="never",
        )
        remembered = ThreadRunSettings(
            sandbox="danger-full-access",
            approval_policy="never",
            developer_instructions="base instructions",
        )
        with (
            patch.object(server, "_project", return_value=project),
            patch.object(server, "_thread_run_settings", return_value=remembered),
            patch.object(server, "_work_item_contract_instructions", return_value="contract"),
            patch.object(server, "_remember_thread_run_settings") as remember,
            patch.object(server.codex, "request", new=AsyncMock(return_value={"ok": True})) as request,
        ):
            asyncio.run(server.resume_thread("thread-1", force_resume=True))

        remember.assert_called_once()
        self.assertEqual(remember.call_args.kwargs["developer_instructions"], "base instructions")
        request.assert_awaited_once()
        self.assertEqual(
            request.await_args.args[1]["developerInstructions"],
            f"base instructions\n\n{security_boundary_instructions()}\n\ncontract",
        )

    def test_resume_thread_backgrounds_slow_codex_resume(self) -> None:
        project = Project(
            id="home",
            name="veridataops",
            path="/home/nbingester/veridataops",
            sandbox="danger-full-access",
            approval_policy="never",
        )
        remembered = ThreadRunSettings(
            sandbox="danger-full-access",
            approval_policy="never",
            developer_instructions="base instructions",
        )

        async def _request(method: str, params: dict[str, object]) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return {"ok": True}

        async def _run() -> None:
            try:
                with (
                    patch.object(server, "_project", return_value=project),
                    patch.object(server, "_thread_run_settings", return_value=remembered),
                    patch.object(server, "_work_item_contract_instructions", return_value="contract"),
                    patch.object(server, "_remember_thread_run_settings"),
                    patch.object(server, "_web_thread_resume_handoff_timeout", return_value=0.01),
                    patch.object(server, "_append_bot_event") as append_event,
                    patch.object(server.codex, "request", side_effect=_request) as request,
                ):
                    result = await server.resume_thread("thread-1", force_resume=True)
                    self.assertEqual(
                        result,
                        {
                            "ok": True,
                            "resuming": True,
                            "threadId": "thread-1",
                            "alreadyResuming": False,
                        },
                    )
                    await asyncio.sleep(0.06)

                request.assert_awaited_once()
                self.assertNotIn("thread-1", server.WEB_THREAD_RESUME_TASKS)
                append_event.assert_called_once()
                self.assertEqual(append_event.call_args.args[0]["type"], "web_resume_backgrounded")
            finally:
                server.WEB_THREAD_RESUME_TASKS.pop("thread-1", None)

        asyncio.run(_run())

    def test_resume_thread_skips_codex_resume_by_default(self) -> None:
        project = Project(
            id="home",
            name="veridataops",
            path="/home/nbingester/veridataops",
            sandbox="danger-full-access",
            approval_policy="never",
        )
        remembered = ThreadRunSettings(
            sandbox="danger-full-access",
            approval_policy="never",
            developer_instructions="base instructions",
        )
        with (
            patch.object(server, "_project", return_value=project),
            patch.object(server, "_thread_run_settings", return_value=remembered),
            patch.object(server, "_remember_thread_run_settings"),
            patch.object(server.codex, "request", new=AsyncMock()) as request,
        ):
            result = asyncio.run(server.resume_thread("thread-1"))

        request.assert_not_awaited()
        self.assertEqual(
            result,
            {
                "ok": True,
                "threadId": "thread-1",
                "skipped": True,
                "reason": "web_load_uses_thread_read",
            },
        )

    def test_read_thread_returns_fallback_on_timeout(self) -> None:
        async def _request(method: str, params: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("thread/read timed out after 10s")

        with (
            patch.object(server.codex, "request", side_effect=_request) as request,
            patch.object(
                server,
                "_load_thread_index",
                return_value=[IndexedThread(id="thread-1", name="Slow thread", cwd="/repo", path="/session.jsonl")],
            ),
            patch.object(server, "_bindings_for_thread", return_value=[]),
            patch.object(server, "_append_bot_event") as append_event,
        ):
            result = asyncio.run(server.read_thread("thread-1", message_limit=25))

        request.assert_awaited_once_with("thread/read", {"threadId": "thread-1", "includeTurns": True})
        self.assertTrue(result["timedOut"])
        self.assertEqual(result["thread"]["id"], "thread-1")
        self.assertEqual(result["thread"]["name"], "Slow thread")
        self.assertEqual(result["thread"]["turns"], [])
        self.assertTrue(result["thread"]["readTimedOut"])
        self.assertEqual(result["thread"]["messageLimit"], 25)
        append_event.assert_called_once()
        self.assertEqual(append_event.call_args.args[0]["type"], "web_read_timeout")

    def test_read_thread_defers_while_web_resume_is_running(self) -> None:
        async def _run() -> None:
            pending = asyncio.create_task(asyncio.sleep(60))
            server.WEB_THREAD_RESUME_TASKS["thread-1"] = pending  # type: ignore[assignment]
            try:
                with (
                    patch.object(server.codex, "request", new=AsyncMock()) as request,
                    patch.object(
                        server,
                        "_load_thread_index",
                        return_value=[IndexedThread(id="thread-1", name="Slow thread", cwd="/repo", path="/session.jsonl")],
                    ),
                    patch.object(server, "_bindings_for_thread", return_value=[]),
                    patch.object(server, "_append_bot_event") as append_event,
                ):
                    result = await server.read_thread("thread-1")

                request.assert_not_awaited()
                self.assertTrue(result["timedOut"])
                self.assertEqual(result["error"], "thread/resume still in progress")
                self.assertTrue(result["thread"]["readTimedOut"])
                append_event.assert_called_once()
                self.assertEqual(append_event.call_args.args[0]["type"], "web_read_deferred_for_resume")
            finally:
                server.WEB_THREAD_RESUME_TASKS.pop("thread-1", None)
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending

        asyncio.run(_run())

    def test_dispatch_event_queues_when_resume_times_out(self) -> None:
        project = Project(
            id="home",
            name="veridataops",
            path="/home/nbingester/veridataops",
            sandbox="danger-full-access",
            approval_policy="never",
        )
        binding = BotBinding(
            id="binding-1",
            connection_id="conn-1",
            provider="slack",
            external_conversation_id="C123",
            thread_id="thread-1",
            project_id="home",
            created_at=1,
            updated_at=1,
        )
        queued = QueuedTurn(
            id="queued-1",
            thread_id="thread-1",
            project_id="home",
            message="handoff",
            source="work-item-handoff",
            created_at=1,
        )

        async def _start(*args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("thread/resume timed out after 20s")

        with (
            patch.object(server, "_project", return_value=project),
            patch.object(server, "_thread_run_settings", return_value=ThreadRunSettings()),
            patch.object(server, "_conversation_target_for_binding", return_value=None),
            patch.object(server, "_release_stale_active_turn"),
            patch.object(server, "_thread_is_active", return_value=False),
            patch.object(server, "_thread_queue_depth", side_effect=[0, 1, 1]),
            patch.object(server, "_find_duplicate_queued_turn", return_value=None),
            patch.object(server, "_enqueue_turn", return_value=queued) as enqueue,
            patch.object(server, "_upsert_bot_binding"),
            patch.object(server, "_publish_queue_status", new=AsyncMock()),
            patch.object(server.hub, "publish", new=AsyncMock()),
            patch.object(server, "_append_bot_event") as append_event,
            patch.object(server, "_start_thread_turn_now", side_effect=_start),
        ):
            result = asyncio.run(server._dispatch_event_to_binding(binding, "handoff", "work-item-handoff"))

        self.assertTrue(result["queued"])
        self.assertEqual(result["queuedId"], "queued-1")
        enqueue.assert_called_once()
        self.assertEqual(append_event.call_args.args[0]["type"], "event_turn_queued_after_timeout")

    def test_create_work_item_handoff_schedules_dispatch(self) -> None:
        state = WorkItemState(
            ref="veridataops/saas-app#264",
            project_id="home",
            handoff=WorkItemHandoff(
                from_agent="james",
                to_agent="quinn",
                requested_at=1,
                status="pending",
            ),
            last_meaningful_update_at=1,
            updated_at=1,
            created_at=1,
        )
        public_state = {"ref": state.ref}
        payload = WorkItemHandoffCreate(from_agent="james", to_agent="quinn")
        with (
            patch.object(server, "_structured_handoff", return_value=state) as structured_handoff,
            patch.object(server, "_work_item_state_public", return_value=public_state),
            patch.object(server.hub, "publish", new=AsyncMock()) as publish,
            patch.object(server, "_schedule_structured_handoff_dispatch") as schedule_dispatch,
            patch.object(server, "_schedule_handoff_continuity_check") as schedule_continuity,
        ):
            result = asyncio.run(server.create_work_item_handoff(state.ref, payload))

        structured_handoff.assert_called_once_with(state.ref, payload)
        publish.assert_awaited_once()
        schedule_dispatch.assert_called_once_with(state, source="work-item-handoff")
        schedule_continuity.assert_called_once_with(state, source="work-item-handoff-continuity")
        self.assertEqual(result, {"ok": True, "item": public_state})

    def test_ack_work_item_handoff_schedules_actionable_owner_dispatch(self) -> None:
        state = WorkItemState(
            ref="veridataops/saas-app#271",
            project_id="home",
            current_owner="james",
            current_stage="failed_with_action_owner",
            last_meaningful_update_at=1,
            updated_at=1,
            created_at=1,
        )
        public_state = {"ref": state.ref}
        payload = WorkItemAckCreate(actor="Quinn", accepted=False)
        with (
            patch.object(server, "_structured_ack", return_value=state) as structured_ack,
            patch.object(server, "_work_item_state_public", return_value=public_state),
            patch.object(server.hub, "publish", new=AsyncMock()) as publish,
            patch.object(server, "_schedule_actionable_owner_dispatch") as schedule_dispatch,
        ):
            result = asyncio.run(server.ack_work_item_handoff(state.ref, payload))

        structured_ack.assert_called_once_with(state.ref, payload)
        publish.assert_awaited_once()
        schedule_dispatch.assert_called_once_with(state, source="work-item-ack", actor=payload.actor)
        self.assertEqual(result, {"ok": True, "item": public_state})

    def test_update_work_item_progress_schedules_actionable_owner_dispatch(self) -> None:
        state = WorkItemState(
            ref="veridataops/saas-app#271",
            project_id="home",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=1,
            updated_at=1,
            created_at=1,
        )
        public_state = {"ref": state.ref}
        payload = WorkItemProgressUpdate(
            actor="Orchestrator",
            current_owner="James",
            current_stage="implementation_active",
            next_action="Fix the shipped gate and hand back to Release Manager.",
            next_owner="James",
        )
        with (
            patch.object(server, "_structured_progress", return_value=state) as structured_progress,
            patch.object(server, "_work_item_state_public", return_value=public_state),
            patch.object(server.hub, "publish", new=AsyncMock()) as publish,
            patch.object(server, "_schedule_actionable_owner_dispatch") as schedule_dispatch,
        ):
            result = asyncio.run(server.update_work_item_progress(state.ref, payload))

        structured_progress.assert_called_once_with(state.ref, payload)
        publish.assert_awaited_once()
        schedule_dispatch.assert_called_once_with(state, source="work-item-progress", actor=payload.actor)
        self.assertEqual(result, {"ok": True, "item": public_state})

    def test_update_work_item_progress_schedules_native_recovery_on_routing_drift(self) -> None:
        state = WorkItemState(
            ref="veridataops/saas-app#271",
            project_id="home",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=1,
            updated_at=1,
            created_at=1,
        )
        public_state = {"ref": state.ref}
        payload = WorkItemProgressUpdate(actor="Orchestrator", next_action="Quinn to validate MR !247.")
        with (
            patch.object(server, "_structured_progress", return_value=state),
            patch.object(server, "_work_item_state_public", return_value=public_state),
            patch.object(server.hub, "publish", new=AsyncMock()),
            patch.object(server, "_schedule_actionable_owner_dispatch"),
            patch.object(server, "_work_item_split_brain_findings", return_value=["next-action owner cue drift"]),
            patch.object(server, "_schedule_native_recovery_cycles") as schedule_recovery,
        ):
            asyncio.run(server.update_work_item_progress(state.ref, payload))

        schedule_recovery.assert_called_once_with(reason="work-item-progress-routing-drift")

    def test_start_turn_persists_base_instructions_but_sends_effective_instructions(self) -> None:
        project = Project(
            id="home",
            name="veridataops",
            path="/home/nbingester/veridataops",
            sandbox="danger-full-access",
            approval_policy="never",
        )
        remembered = ThreadRunSettings(
            sandbox="danger-full-access",
            approval_policy="never",
            developer_instructions="base instructions",
        )
        binding = SimpleNamespace(
            assignment_id="assignment-dev-instructions",
            workspace_id="workspace-dev-instructions",
        )
        session = SimpleNamespace(
            workspace_path=Path("/isolated/workspace"),
            status=lambda: SimpleNamespace(worker_id="worker-1", fence=1),
            request=AsyncMock(
                side_effect=[
                    {"ok": True},
                    {"turn": {"id": "turn-1"}},
                ]
            ),
        )
        execution_service = server.app.state.turn_execution_service

        with (
            patch.object(server, "_project", return_value=project),
            patch.object(server, "_thread_run_settings", return_value=remembered),
            patch.object(server, "_work_item_contract_instructions", return_value="contract"),
            patch.object(server, "_remember_thread_run_settings") as remember,
            patch.object(
                execution_service,
                "_select_runtime_binding",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch.object(execution_service.binding_service, "prepare", return_value=binding),
            patch.object(execution_service.session_manager, "start", new=AsyncMock(return_value=session)),
            patch.object(execution_service, "mark_thread_active"),
            patch.object(server, "_append_bot_event"),
            patch.object(server.hub, "publish", new=AsyncMock()),
        ):
            asyncio.run(server.start_turn("thread-1", TurnCreate(message="fix it")))

        remember.assert_called_once()
        self.assertEqual(remember.call_args.kwargs["developer_instructions"], "base instructions")
        self.assertEqual(
            [call.args[0] for call in session.request.await_args_list],
            ["thread/resume", "turn/start"],
        )
        turn_start_call = session.request.await_args_list[1]
        self.assertEqual(
            turn_start_call.args[1]["developerInstructions"],
            f"base instructions\n\n{security_boundary_instructions()}\n\ncontract",
        )



if __name__ == "__main__":
    unittest.main()
