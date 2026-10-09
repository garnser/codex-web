from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.models import BotBinding, BotReplyTarget
from codex_web.services.bot_event_dispatch import (
    BotEventDispatchService, BotEventDispatchCompatibilityFacade,
)
from codex_web.services.bot_targets import BotTargetService


class BotEventDispatchScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_resolution_keeps_loop_responsive_and_context_before_queueing(self):
        self.install_captured_target()
        started, release = threading.Event(), threading.Event()
        tenant = contextvars.ContextVar("dispatch_tenant", default=None)
        tenant.set("project-1")
        selected = self.service.targets.event_reply_target
        observed = []

        def blocking(binding):
            observed.append((threading.get_ident(), tenant.get()))
            started.set()
            release.wait(2)
            return selected(binding)

        self.service.targets.event_reply_target = blocking
        task = asyncio.create_task(self.service.dispatch(self.binding, "continue", "watchdog"))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.execution.enqueue_turn.assert_not_called()
            self.assertNotEqual(observed[0][0], threading.get_ident())
            self.assertEqual(observed[0][1], "project-1")
        finally:
            release.set()
            await task
        self.assertEqual(self.execution.enqueue_turn.call_args.kwargs["reply_target"], self.captured)

    def install_captured_target(self):
        self.binding = self.binding.model_copy(update={"connection_id": "connection"})
        primary = self.binding.model_copy(update={
            "id": "primary", "external_conversation_id": "PRIMARY",
            "is_primary_channel": True,
        })
        self.captured = BotReplyTarget(
            thread_id="thread-1", provider="slack", external_conversation_id="PRIMARY",
            external_thread_id="human-message", message_id="human-message", updated_at=100,
        )
        self.reply_records = {"slack:PRIMARY:thread-1": self.captured}
        self.target_bindings = [self.binding, primary]
        self.service.targets = BotTargetService(
            load_reply_targets=lambda: self.reply_records,
            save_reply_targets=lambda _records: None,
            load_delivery_targets=lambda: {},
            save_delivery_targets=lambda _records: None,
            load_active_turns=lambda: {},
            bindings_for_project=lambda _provider, _project: self.target_bindings,
        )

    async def test_background_queue_keeps_human_conversation_without_rewriting_capture(self):
        self.install_captured_target()
        await self.service.dispatch(self.binding, "continue owned work", "owner-work-watchdog")
        self.assertEqual(self.execution.enqueue_turn.call_args.kwargs["reply_target"], self.captured)
        self.assertEqual(self.reply_records["slack:PRIMARY:thread-1"], self.captured)
        self.execution.enqueue_turn.assert_called_once()

    async def test_immediate_dispatch_and_recovery_revalidate_captured_target(self):
        self.install_captured_target()
        self.execution.thread_is_active.return_value = False
        self.queue_policy.depth.return_value = 0
        self.execution.start_thread_turn_now = AsyncMock(side_effect=[
            RuntimeError("stale thread"), {"turn": {"id": "new-turn"}},
        ])
        self.service.resume = SimpleNamespace(
            is_timeout_error=lambda _error: False,
            is_stale_thread_error=lambda _error: True,
        )

        async def replace(_binding, _error):
            replacement = self.binding.model_copy(update={"thread_id": "replacement"})
            self.service.targets.retarget("thread-1", "replacement")
            # Use canonical state collaborators as real recovery does.
            self.reply_records = {
                "slack:PRIMARY:replacement": self.captured.model_copy(update={"thread_id": "replacement"})
            }
            self.target_bindings = [b.model_copy(update={"thread_id": "replacement"}) for b in self.target_bindings]
            return replacement

        self.service.recovery.replace_stale_bot_thread = AsyncMock(side_effect=replace)
        await self.service.dispatch(self.binding, "continue work", "work-item-sla")
        first, second = self.execution.start_thread_turn_now.call_args_list
        self.assertEqual(first.kwargs["reply_target"], self.captured)
        actual = second.kwargs["reply_target"]
        self.assertEqual(actual.thread_id, "replacement")
        self.assertEqual(actual.external_conversation_id, "PRIMARY")
        self.assertEqual(actual.external_thread_id, "human-message")

    async def test_compatibility_dispatch_keeps_same_validated_target(self):
        self.install_captured_target()
        host = SimpleNamespace(
            _project=self.service.projects.get,
            _thread_run_settings=self.service.settings.get,
            _event_reply_target_for_binding=self.service.targets.event_reply_target,
            _conversation_target_for_binding=self.service.targets.conversation_target,
            _release_stale_active_turn=Mock(),
            _thread_is_active=Mock(return_value=True),
            _thread_queue_depth=Mock(return_value=1),
            _find_duplicate_queued_turn=Mock(return_value=None),
            _enqueue_turn=Mock(return_value=SimpleNamespace(id="queued")),
            _upsert_bot_binding=Mock(),
            _append_bot_event=Mock(),
            _publish_queue_status=AsyncMock(),
            hub=SimpleNamespace(publish=AsyncMock()),
        )
        await BotEventDispatchCompatibilityFacade(host).dispatch(self.binding, "continue", "orchestrator-watchdog")
        self.assertEqual(host._enqueue_turn.call_args.kwargs["reply_target"], self.captured)
        host._enqueue_turn.assert_called_once()

    def setUp(self) -> None:
        self.execution = SimpleNamespace(
            find_duplicate_queued_turn=Mock(return_value=None),
            enqueue_turn=Mock(return_value=SimpleNamespace(id="queued-1")),
            publish_queue_status=AsyncMock(),
            thread_is_active=Mock(return_value=True),
            thread_has_live_agent_runtime_session=Mock(return_value=True),
            thread_has_inflight_agent_runtime_assignment=Mock(return_value=False),
        )
        self.queue_policy = SimpleNamespace(depth=Mock(return_value=1))
        self.service = BotEventDispatchService(
            projects=SimpleNamespace(
                get=Mock(return_value=SimpleNamespace(id="project-1", model="model-1"))
            ),
            settings=SimpleNamespace(
                get=Mock(return_value=SimpleNamespace(model=None, reasoning_effort=None))
            ),
            targets=SimpleNamespace(
                event_reply_target=Mock(return_value=None),
                conversation_target=Mock(return_value=None),
            ),
            bindings=SimpleNamespace(upsert=Mock()),
            execution=self.execution,
            queue_policy=self.queue_policy,
            recovery=SimpleNamespace(release_stale_active_turn=Mock()),
            resume=SimpleNamespace(),
            telemetry=SimpleNamespace(append=Mock()),
            publish_event=AsyncMock(),
            binding_name=lambda _binding: "quinn",
        )
        self.binding = BotBinding(
            id="binding-1",
            provider="slack",
            external_conversation_id="C1",
            thread_id="thread-1",
            project_id="project-1",
            created_at=1.0,
            updated_at=1.0,
        )

    async def test_queued_event_preserves_canonical_repository_scope(self) -> None:
        await self.service.dispatch(
            self.binding,
            "validate veridataops/saas-app#379",
            "gitlab",
            work_item_ref="veridataops/saas-app#379",
            repository_resource_id="repo-saas-app",
            writable_repository_resource_ids=("repo-saas-app",),
        )

        kwargs = self.execution.enqueue_turn.call_args.kwargs
        self.assertEqual(kwargs["work_item_ref"], "veridataops/saas-app#379")
        self.assertEqual(kwargs["repository_resource_id"], "repo-saas-app")
        self.assertEqual(
            kwargs["writable_repository_resource_ids"],
            ("repo-saas-app",),
        )

    async def test_idle_only_dispatch_does_not_queue_duplicate_work(self) -> None:
        result = await self.service.dispatch(
            self.binding,
            "wake only if idle",
            "assignment-control-plane",
            work_item_ref="project/repo#1",
            require_idle=True,
        )

        self.assertEqual(result["skipped"], "already_active")
        self.assertFalse(result["queued"])
        self.execution.enqueue_turn.assert_not_called()

    async def test_queued_event_preserves_recipient_agent_profile(self) -> None:
        actor = SimpleNamespace(identity_id="local-admin")
        profile = SimpleNamespace(
            profile_id="veridataops-quinn",
            revision=4,
            execution_profile_id="repository-write",
        )
        self.service.agent_profile_resolver = Mock(
            return_value=(profile, actor)
        )

        await self.service.dispatch(
            self.binding,
            "quinn: validate the handoff",
            "work-item-handoff",
        )

        kwargs = self.execution.enqueue_turn.call_args.kwargs
        self.assertEqual(kwargs["agent_profile_id"], "veridataops-quinn")
        self.assertEqual(kwargs["agent_profile_revision"], 4)
        self.assertEqual(kwargs["agent_profile_actor_id"], "local-admin")
        self.assertEqual(kwargs["execution_profile_id"], "repository-write")

    async def test_missing_session_is_not_replaced_while_assignment_is_inflight(self) -> None:
        self.execution.thread_is_active.return_value = False
        self.execution.thread_has_live_agent_runtime_session.return_value = False
        self.execution.thread_has_inflight_agent_runtime_assignment.return_value = True
        self.queue_policy.depth.return_value = 0
        self.service.recovery.replace_stale_bot_thread = AsyncMock()

        result = await self.service.replace_nonperforming_thread(
            self.binding,
            "owner-work-watchdog",
        )

        self.assertIs(result, self.binding)
        self.service.recovery.replace_stale_bot_thread.assert_not_awaited()

    async def test_scratch_profile_removes_event_repository_grants(self) -> None:
        self.service.projects.get.return_value.organization_id = "local"
        self.service.projects.get.return_value.workspace_id = "default"
        self.service.agent_profile_resolver = Mock(return_value=(
            SimpleNamespace(profile_id="coordinator", revision=5,
                            execution_profile_id="custom-scratch-profile"),
            SimpleNamespace(identity_id="operator"),
        ))
        self.service.execution_profiles = SimpleNamespace(resolve=Mock(
            return_value=(SimpleNamespace(repository_access="none"), None)
        ))
        await self.service.dispatch(
            self.binding, "coordinate issue", "gitlab",
            work_item_ref="project/repo#1", repository_resource_id="repo-1",
            writable_repository_resource_ids=("repo-1",),
            read_only_repository_resource_ids=("repo-2",),
        )
        kwargs = self.execution.enqueue_turn.call_args.kwargs
        self.assertIsNone(kwargs["repository_resource_id"])
        self.assertEqual(kwargs["writable_repository_resource_ids"], ())
        self.assertEqual(kwargs["read_only_repository_resource_ids"], ())
        self.assertEqual(kwargs["work_item_ref"], "project/repo#1")
        self.assertEqual(kwargs["execution_profile_id"], "custom-scratch-profile")

    async def test_repository_profile_retains_event_repository_grants(self) -> None:
        self.service.projects.get.return_value.organization_id = "local"
        self.service.projects.get.return_value.workspace_id = "default"
        self.service.execution_profiles = SimpleNamespace(resolve=Mock(
            return_value=(SimpleNamespace(repository_access="write"), None)
        ))
        await self.service.dispatch(
            self.binding, "implement issue", "gitlab",
            repository_resource_id="repo-1",
            writable_repository_resource_ids=("repo-1",),
        )
        kwargs = self.execution.enqueue_turn.call_args.kwargs
        self.assertEqual(kwargs["repository_resource_id"], "repo-1")
        self.assertEqual(kwargs["writable_repository_resource_ids"], ("repo-1",))

    async def test_fresh_replacement_gets_recent_activity_grace(self) -> None:
        self.execution.thread_is_active.return_value = False
        self.execution.thread_has_live_agent_runtime_session.return_value = False
        self.queue_policy.depth.return_value = 0
        self.service.thread_recently_active = Mock(return_value=True)
        self.service.recovery.replace_stale_bot_thread = AsyncMock()

        result = await self.service.replace_nonperforming_thread(
            self.binding,
            "owner-work-watchdog",
        )

        self.assertIs(result, self.binding)
        self.service.recovery.replace_stale_bot_thread.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
