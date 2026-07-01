from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server
from codex_web.models import ActiveThreadTurn, BotBinding, BotReplyTarget, Project, ThreadRunSettings


class BotThreadReplacementTests(unittest.TestCase):
    def test_replace_stale_bot_thread_falls_back_to_startup_when_variant_is_unsupported(self) -> None:
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

        calls: list[dict[str, object]] = []

        async def _request(method: str, params: dict[str, object]) -> dict[str, object]:
            self.assertEqual(method, "thread/start")
            calls.append(params)
            source = params.get("sessionStartSource")
            if source == "bot-thread-replacement":
                raise RuntimeError(
                    "{'code': -32600, 'message': 'Invalid request: unknown variant `bot-thread-replacement`, expected `startup` or `clear`'}"
                )
            self.assertEqual(source, "startup")
            return {"thread": {"id": "new-thread"}}

        with (
            patch.object(
                server,
                "_project",
                return_value=Project(
                    id="a956644fc336",
                    name="veridataops",
                    path="/home/nbingester/veridataops",
                    sandbox="danger-full-access",
                    approval_policy="never",
                ),
            ),
            patch.object(server, "_thread_run_settings", return_value=ThreadRunSettings()),
            patch.object(server, "_remember_thread_run_settings"),
            patch.object(server, "_set_thread_name", new=AsyncMock()),
            patch.object(server, "_upsert_indexed_thread"),
            patch.object(
                server,
                "_retarget_logical_bot_bindings",
                return_value=binding.model_copy(update={"thread_id": "new-thread"}),
            ) as retarget_bindings,
            patch.object(server, "_retarget_bot_thread_state") as retarget_state,
            patch.object(server, "_append_bot_event"),
            patch.object(server.codex, "request", side_effect=_request),
        ):
            replacement = asyncio.run(server._replace_stale_bot_thread(binding, "thread not found"))

        self.assertEqual(replacement.thread_id, "new-thread")
        self.assertEqual([call["sessionStartSource"] for call in calls], ["bot-thread-replacement", "startup"])
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
            [
                ("slack", "C0B9M89AHCY"),
                ("slack", "C0B9591ESTB"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
