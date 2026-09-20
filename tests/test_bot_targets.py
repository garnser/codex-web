from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from fastapi import FastAPI

from codex_web.models import BotBinding, BotInboundMessage, BotReplyTarget
from codex_web.services.bot_targets import BotTargetService, install_bot_target_service


class _TargetHost:
    def __init__(self) -> None:
        self.reply_targets: dict[str, BotReplyTarget] = {}
        self.delivery_targets: dict[str, BotReplyTarget] = {}
        self.active_turns: dict[str, SimpleNamespace] = {}
        self.bindings: list[BotBinding] = []

    def _load_bot_reply_targets(self):
        return dict(self.reply_targets)

    def _save_bot_reply_targets(self, targets):
        self.reply_targets = dict(targets)

    def _load_bot_delivery_targets(self):
        return dict(self.delivery_targets)

    def _save_bot_delivery_targets(self, targets):
        self.delivery_targets = dict(targets)

    def _load_active_turns(self):
        return self.active_turns

    def _bindings_for_project(self, provider, project_id):
        return [
            binding
            for binding in self.bindings
            if binding.provider == provider and binding.project_id == project_id
        ]

    @staticmethod
    def _should_reply_in_external_thread(binding):
        return binding.post_in_thread


def _service(host: _TargetHost) -> BotTargetService:
    return BotTargetService(
        load_reply_targets=host._load_bot_reply_targets,
        save_reply_targets=host._save_bot_reply_targets,
        load_delivery_targets=host._load_bot_delivery_targets,
        save_delivery_targets=host._save_bot_delivery_targets,
        load_active_turns=host._load_active_turns,
        bindings_for_project=host._bindings_for_project,
    )


def _binding(
    binding_id: str,
    *,
    thread_id: str,
    conversation: str = "C1",
    provider: str = "slack",
    master: bool = False,
    primary: bool = False,
    post_in_thread: bool = False,
    updated_at: float | None = None,
) -> BotBinding:
    now = time.time()
    return BotBinding(
        id=binding_id,
        provider=provider,
        external_conversation_id=conversation,
        thread_id=thread_id,
        project_id="home",
        is_master=master,
        is_primary_channel=primary,
        post_in_thread=post_in_thread,
        created_at=now - 10,
        updated_at=updated_at or now,
    )


class BotTargetServiceTests(unittest.TestCase):
    def test_remember_reply_target_indexes_binding_and_external_thread(self) -> None:
        host = _TargetHost()
        service = _service(host)
        binding = _binding("b1", thread_id="thread-1")
        message = BotInboundMessage(
            provider="slack",
            external_conversation_id="C1",
            text="hello",
            external_thread_id="111.22",
            message_id="111.22",
        )

        target = service.remember_reply_target(binding, message)

        self.assertIsNotNone(target)
        self.assertIn("slack:C1:thread-1", host.reply_targets)
        self.assertIn("slack:C1:external:111.22", host.reply_targets)
        self.assertEqual(
            service.target_for_external_thread("slack", "C1", "111.22").thread_id,
            "thread-1",
        )

    def test_active_target_wins_outbound_selection(self) -> None:
        host = _TargetHost()
        service = _service(host)
        binding = _binding("b1", thread_id="thread-1")
        active_target = BotReplyTarget(
            thread_id="thread-1",
            provider="slack",
            external_conversation_id="C1",
            external_thread_id="active-ts",
            message_id="active-ts",
            updated_at=time.time(),
        )
        host.active_turns["thread-1"] = SimpleNamespace(reply_target=active_target)
        host.delivery_targets[service.reply_target_key(binding)] = BotReplyTarget(
            thread_id="thread-1",
            provider="slack",
            external_conversation_id="C1",
            external_thread_id="old-delivery",
            message_id="old-delivery",
            updated_at=time.time() - 100,
        )

        target, should_thread = service.thread_target_for_outbound(binding)

        self.assertIs(target, active_target)
        self.assertTrue(should_thread)

    def test_master_target_is_fallback_for_agent_binding(self) -> None:
        host = _TargetHost()
        service = _service(host)
        agent = _binding("agent", thread_id="agent-thread")
        master = _binding("master", thread_id="master-thread", master=True)
        host.bindings = [agent, master]
        host.reply_targets[service.reply_target_key(master)] = BotReplyTarget(
            thread_id="master-thread",
            provider="slack",
            external_conversation_id="C1",
            external_thread_id="master-ts",
            message_id="master-ts",
            updated_at=time.time(),
        )

        target, should_thread = service.thread_target_for_outbound(agent)

        self.assertEqual(target.thread_id, "master-thread")
        self.assertTrue(should_thread)

    def test_retarget_rewrites_binding_keys_and_thread_ids(self) -> None:
        host = _TargetHost()
        service = _service(host)
        old = _binding("b1", thread_id="old-thread")
        target = BotReplyTarget(
            thread_id="old-thread",
            provider="slack",
            external_conversation_id="C1",
            external_thread_id="111.22",
            message_id="111.22",
            updated_at=time.time(),
        )
        host.reply_targets[service.reply_target_key(old)] = target
        host.reply_targets[service.external_target_key("slack", "C1", "111.22")] = target

        service.retarget("old-thread", "new-thread")

        self.assertIn("slack:C1:new-thread", host.reply_targets)
        self.assertNotIn("slack:C1:old-thread", host.reply_targets)
        self.assertEqual(host.reply_targets["slack:C1:new-thread"].thread_id, "new-thread")
        self.assertEqual(
            host.reply_targets["slack:C1:external:111.22"].thread_id,
            "new-thread",
        )

    def test_installer_rebinds_compatibility_entrypoints(self) -> None:
        host = _TargetHost()
        app = FastAPI()

        service = install_bot_target_service(app, host)

        self.assertIs(app.state.bot_target_service, service)
        self.assertIs(host._thread_target_for_outbound.__self__, service)
        self.assertIs(host._remember_bot_reply_target.__self__, service)
        self.assertIs(host._retarget_bot_targets.__self__, service)


if __name__ == "__main__":
    unittest.main()
