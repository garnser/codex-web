from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server
from codex_web.models import ActiveThreadTurn, BotBinding, BotReplyTarget, Project, ThreadRunSettings


class BotThreadReplacementTests(unittest.TestCase):
    def test_replace_stale_bot_thread_uses_assignment_bound_bootstrap(self) -> None:
        binding = BotBinding(
            id="binding-1",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="old-thread",
            project_id="a956644fc336",
            thread_name="James - Development Agent",
            route_prefix="James",
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=1.0,
        )

        creator = AsyncMock(return_value={"thread": {"id": "new-thread"}})
        recovery = server._replace_stale_bot_thread.__self__
        project = Project(
            id="a956644fc336",
            name="veridataops",
            path="/home/nbingester/veridataops",
            sandbox="danger-full-access",
            approval_policy="never",
        )

        with (
            patch.object(recovery.projects, "get", return_value=project),
            patch.object(
                recovery.settings,
                "get",
                return_value=ThreadRunSettings(),
            ),
            patch.object(recovery.settings, "remember"),
            patch.object(recovery.naming, "set_name", new=AsyncMock()),
            patch.object(recovery.thread_index, "upsert"),
            patch.object(
                server,
                "_retarget_logical_bot_bindings",
                return_value=binding.model_copy(update={"thread_id": "new-thread"}),
            ) as retarget_bindings,
            patch.object(server, "_retarget_bot_thread_state") as retarget_state,
            patch.object(
                server,
                "_archive_replaced_bot_thread",
                new=AsyncMock(return_value=True),
            ),
            patch.object(recovery, "event_sink"),
            patch.object(server.hub, "publish", new=AsyncMock()),
            patch.object(recovery, "thread_creator", creator),
        ):
            replacement = asyncio.run(server._replace_stale_bot_thread(binding, "thread not found"))

        self.assertEqual(replacement.thread_id, "new-thread")
        creator.assert_awaited_once()
        retarget_bindings.assert_called_once()
        retarget_state.assert_called_once_with("old-thread", "new-thread")

    def test_outbound_bindings_include_passive_report_channels_alongside_active_reply_target(self) -> None:
        active = BotBinding(
            id="binding-active",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-1",
            project_id="a956644fc336",
            thread_name="James - Development Agent",
            route_prefix="James",
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=10.0,
        )
        report = BotBinding(
            id="binding-report",
            provider="slack",
            external_conversation_id="C0B9591ESTB",
            thread_id="thread-1",
            project_id="a956644fc336",
            thread_name="James - Development Agent",
            route_prefix="James",
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=9.0,
        )

        with (
            patch.object(
                server,
                "_load_active_turns",
                return_value={
                    "thread-1": ActiveThreadTurn(
                        thread_id="thread-1",
                        project_id="a956644fc336",
                        source="slack:message",
                        reply_target=BotReplyTarget(
                            thread_id="thread-1",
                            provider="slack",
                            external_conversation_id="C0B9M89AHCY",
                            external_thread_id="parent",
                            message_id="msg-1",
                            updated_at=10.0,
                        ),
                        started_at=10.0,
                        updated_at=10.0,
                    )
                },
            ),
            patch.object(server, "_load_bot_reply_targets", return_value={}),
        ):
            selected = server._outbound_bindings_for_thread("thread-1", [report, active])

        self.assertEqual(
            [(binding.provider, binding.external_conversation_id) for binding in selected],
            [("slack", "C0B9M89AHCY")],
        )

    def test_thread_target_for_outbound_uses_master_delivery_target_for_passive_binding(self) -> None:
        master = BotBinding(
            id="binding-master",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-master",
            project_id="a956644fc336",
            thread_name="Orchestrator",
            route_prefix="Orchestrator",
            is_master=True,
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=20.0,
        )
        passive = BotBinding(
            id="binding-release",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-release",
            project_id="a956644fc336",
            thread_name="Release Manager - Production Deployments Agent",
            route_prefix="Release Manager",
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=10.0,
        )
        master_target = BotReplyTarget(
            thread_id="thread-master",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            external_thread_id="parent-thread",
            message_id="msg-1",
            updated_at=25.0,
        )

        with (
            patch.object(server, "_bindings_for_project", return_value=[master, passive]),
            patch.object(server, "_active_reply_target_for_binding", return_value=None),
            patch.object(server, "_reply_target_for_binding", return_value=None),
            patch.object(
                server,
                "_delivery_target_for_binding",
                side_effect=lambda binding: master_target if binding.thread_id == "thread-master" else None,
            ),
        ):
            target, should_thread = server._thread_target_for_outbound(passive)

        self.assertEqual(target, master_target)
        self.assertTrue(should_thread)

    def test_send_bot_outbound_threads_when_target_requests_threading_even_if_post_in_thread_is_false(self) -> None:
        binding = BotBinding(
            id="binding-release",
            connection_id="conn-1",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-release",
            project_id="a956644fc336",
            thread_name="Release Manager - Production Deployments Agent",
            route_prefix="Release Manager",
            post_in_thread=False,
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=10.0,
        )
        target = BotReplyTarget(
            thread_id="thread-release",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            external_thread_id="known-thread",
            message_id="known-msg",
            updated_at=15.0,
        )
        post_message = AsyncMock(
            return_value={"sent": True, "providerResponse": {"ok": True, "ts": "1"}}
        )
        delivery_service = server.app.state.bot_delivery_service

        with (
            patch.object(
                server,
                "_bot_connection",
                return_value=type(
                    "Conn",
                    (),
                    {
                        "bot_token": "xoxb-token",
                        "project_id": "a956644fc336",
                    },
                )(),
            ),
            patch.object(
                delivery_service,
                "thread_project_id",
                return_value="a956644fc336",
            ),
            patch.object(server, "_thread_target_for_outbound", return_value=(target, True)),
            patch.object(server, "_slack_reply_username", return_value="Codex · Release Manager"),
            patch.object(server, "_slack_reply_icon", return_value=":large_orange_diamond:"),
            patch.object(delivery_service.slack, "post_message", post_message),
        ):
            asyncio.run(server._send_bot_outbound(binding, "status"))

        self.assertEqual(post_message.await_args.kwargs["thread_ts"], "known-thread")


if __name__ == "__main__":
    unittest.main()
