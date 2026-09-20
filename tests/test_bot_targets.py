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
        self.assertIn(
            "thread:thread-1:slack:C1",
            host.reply_targets,
        )
        self.assertIn("slack:C1:external:111.22", host.reply_targets)
        self.assertEqual(
            service.target_for_external_thread("slack", "C1", "111.22").thread_id,
            "thread-1",
        )

    def test_remember_targets_use_keyed_writes_without_full_registry_load(self) -> None:
        host = _TargetHost()
        reply_loads = 0
        delivery_loads = 0

        def load_reply_targets():
            nonlocal reply_loads
            reply_loads += 1
            return {}

        def load_delivery_targets():
            nonlocal delivery_loads
            delivery_loads += 1
            return {}

        service = BotTargetService(
            load_reply_targets=load_reply_targets,
            save_reply_targets=lambda _targets: None,
            load_delivery_targets=load_delivery_targets,
            save_delivery_targets=lambda _targets: None,
            load_active_turns=host._load_active_turns,
            bindings_for_project=host._bindings_for_project,
            put_reply_target=host.reply_targets.__setitem__,
            put_delivery_target=host.delivery_targets.__setitem__,
        )
        binding = _binding("b1", thread_id="thread-1")
        message = BotInboundMessage(
            provider="slack",
            external_conversation_id="C1",
            text="hello",
            external_thread_id="111.22",
            message_id="111.22",
        )

        service.remember_reply_target(binding, message)
        service.remember_delivery_target(
            binding,
            {
                "sent": True,
                "providerResponse": {"ts": "222.33"},
            },
        )

        self.assertEqual(reply_loads, 0)
        self.assertEqual(delivery_loads, 0)
        self.assertIn("slack:C1:thread-1", host.reply_targets)
        self.assertIn(
            "slack:C1:external:111.22",
            host.reply_targets,
        )
        self.assertIn("slack:C1:thread-1", host.delivery_targets)
        self.assertIn(
            "slack:C1:external:222.33",
            host.delivery_targets,
        )

    def test_external_thread_lookup_is_exact_keyed_with_large_registry(self) -> None:
        host = _TargetHost()
        target = BotReplyTarget(
            thread_id="thread-hit",
            provider="slack",
            external_conversation_id="C-large",
            external_thread_id="hit-ts",
            message_id="hit-ts",
            updated_at=time.time(),
        )
        targets = {
            f"slack:C-large:external:{index}": BotReplyTarget(
                thread_id=f"thread-{index}",
                provider="slack",
                external_conversation_id="C-large",
                external_thread_id=str(index),
                message_id=str(index),
                updated_at=float(index),
            )
            for index in range(20_000)
        }
        targets["slack:C-large:external:hit-ts"] = target
        reply_reads = 0
        delivery_reads = 0

        def get_reply(key):
            nonlocal reply_reads
            reply_reads += 1
            return targets.get(key)

        def get_delivery(key):
            nonlocal delivery_reads
            delivery_reads += 1
            return None

        service = BotTargetService(
            load_reply_targets=lambda: (_ for _ in ()).throw(
                AssertionError("must not load full reply target map")
            ),
            save_reply_targets=lambda _targets: None,
            load_delivery_targets=lambda: (_ for _ in ()).throw(
                AssertionError("must not load full delivery target map")
            ),
            save_delivery_targets=lambda _targets: None,
            load_active_turns=host._load_active_turns,
            bindings_for_project=host._bindings_for_project,
            get_reply_target=get_reply,
            get_delivery_target=get_delivery,
        )

        resolved = service.target_for_external_thread(
            "SLACK",
            "C-large",
            "hit-ts",
        )

        self.assertEqual(resolved.thread_id, "thread-hit")
        self.assertEqual(reply_reads, 1)
        self.assertEqual(delivery_reads, 0)
        self.assertEqual(service.metrics()["compatibilityRepairScans"], 0)

    def test_one_outbound_decision_reuses_request_scoped_target_state(self) -> None:
        host = _TargetHost()
        binding = _binding("b1", thread_id="thread-1")
        reply = BotReplyTarget(
            thread_id="thread-1",
            provider="slack",
            external_conversation_id="C1",
            external_thread_id="111.22",
            message_id="111.22",
            updated_at=time.time(),
        )
        reply_key = f"slack:C1:thread-1"
        counts = {"reply": 0, "delivery": 0, "active": 0}

        def get_reply(key):
            counts["reply"] += 1
            return reply if key == reply_key else None

        def get_delivery(_key):
            counts["delivery"] += 1
            return None

        def get_active(_thread_id):
            counts["active"] += 1
            return None

        service = BotTargetService(
            load_reply_targets=lambda: (_ for _ in ()).throw(
                AssertionError("full reply load is forbidden")
            ),
            save_reply_targets=lambda _targets: None,
            load_delivery_targets=lambda: (_ for _ in ()).throw(
                AssertionError("full delivery load is forbidden")
            ),
            save_delivery_targets=lambda _targets: None,
            load_active_turns=lambda: (_ for _ in ()).throw(
                AssertionError("full active-turn load is forbidden")
            ),
            bindings_for_project=lambda _provider, _project: [],
            get_reply_target=get_reply,
            get_delivery_target=get_delivery,
            get_active_turn=get_active,
        )

        target, should_thread = service.thread_target_for_outbound(binding)

        self.assertIs(target, reply)
        self.assertFalse(should_thread)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(counts["reply"], 1)
        self.assertLessEqual(counts["delivery"], 2)

    def test_thread_scoped_alias_resolves_without_bulk_target_load(self) -> None:
        host = _TargetHost()
        binding = _binding("b1", thread_id="thread-1")
        target = BotReplyTarget(
            thread_id="thread-1",
            provider="slack",
            external_conversation_id="C1",
            external_thread_id="thread-ts",
            message_id="thread-ts",
            updated_at=time.time(),
        )
        thread_key = "thread:thread-1:slack:C1"
        reads: list[str] = []

        service = BotTargetService(
            load_reply_targets=lambda: (_ for _ in ()).throw(
                AssertionError("full reply target load is forbidden")
            ),
            save_reply_targets=lambda _targets: None,
            load_delivery_targets=lambda: {},
            save_delivery_targets=lambda _targets: None,
            load_active_turns=host._load_active_turns,
            bindings_for_project=host._bindings_for_project,
            get_reply_target=lambda key: (
                reads.append(key) or (target if key == thread_key else None)
            ),
        )

        resolved = service.reply_target_for_binding(binding)

        self.assertIs(resolved, target)
        self.assertEqual(
            reads,
            ["slack:C1:thread-1", thread_key],
        )

    def test_missing_legacy_alias_uses_one_bounded_observable_repair_page(self) -> None:
        host = _TargetHost()
        repaired = BotReplyTarget(
            thread_id="thread-old",
            provider="slack",
            external_conversation_id="C-old",
            external_thread_id="legacy-ts",
            message_id="legacy-ts",
            updated_at=time.time(),
        )
        page_calls: list[tuple[str | None, int]] = []
        repaired_aliases: dict[str, BotReplyTarget] = {}

        def page_reply_targets(*, key_prefix=None, after=None, limit=100):
            del after
            page_calls.append((key_prefix, limit))
            return ({"slack:C-old:thread-old": repaired}, None)

        service = BotTargetService(
            load_reply_targets=lambda: {},
            save_reply_targets=lambda _targets: None,
            load_delivery_targets=lambda: {},
            save_delivery_targets=lambda _targets: None,
            load_active_turns=host._load_active_turns,
            bindings_for_project=host._bindings_for_project,
            get_reply_target=lambda _key: None,
            get_delivery_target=lambda _key: None,
            page_reply_targets=page_reply_targets,
            put_reply_target=repaired_aliases.__setitem__,
        )

        target = service.target_for_external_thread(
            "slack",
            "C-old",
            "legacy-ts",
        )

        self.assertIs(target, repaired)
        self.assertEqual(page_calls, [("slack:C-old:", 250)])
        self.assertIn(
            "slack:C-old:external:legacy-ts",
            repaired_aliases,
        )
        self.assertEqual(service.metrics()["compatibilityRepairScans"], 1)
        self.assertEqual(service.metrics()["compatibilityRepairHits"], 1)

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
        self.assertTrue(callable(host._thread_target_for_outbound))
        self.assertIs(host._remember_bot_reply_target.__self__, service)
        self.assertIs(host._retarget_bot_targets.__self__, service)


if __name__ == "__main__":
    unittest.main()
