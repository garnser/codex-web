from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from codex_web.models import BotBinding, BotReplyTarget
from codex_web.services.bot_event_dispatch import (
    BotEventDispatchService, BotEventDispatchCompatibilityFacade,
)
from codex_web.services.bot_targets import BotTargetService
from codex_web.notification_provenance import ProviderNotificationPin
from codex_web.compatibility import CanonicalEventEnvelope


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

    async def test_verified_provider_pin_survives_dispatch_queue_admission(self):
        event=CanonicalEventEnvelope(event_id='event-1',event_type='task_source.event',source='gitlab:fixture',
            occurred_at=1,tenant_id='local',workspace_id='default',payload={'project_id':self.binding.project_id,
                'provider_event_type':'issue.updated'})
        pin=ProviderNotificationPin.from_event(event,source_origin='gitlab',provider_event_kind='issue',
            work_item_ref='group/project#1',message='Fixture notification')
        await self.service.dispatch(self.binding,'Fixture notification','gitlab',work_item_ref='group/project#1',
            provider_notification_pin=pin)
        self.assertEqual(self.execution.enqueue_turn.call_args.kwargs['provider_notification_pin'],pin)
        self.assertEqual(self.execution.find_duplicate_queued_turn.call_args.kwargs['provider_notification_pin'],pin)

    async def test_owner_pin_uses_actual_context_and_retained_queue_even_for_idle_thread(self):
        pin = object()
        profile = SimpleNamespace(profile_id='actual-profile', revision=3, execution_profile_id='orchestration-only')
        actor = SimpleNamespace(identity_id='actual-actor')
        self.service.owner_routing_context = Mock(return_value=(profile, actor))
        self.service.agent_profile_resolver = Mock(side_effect=AssertionError('must not use name-based fallback'))
        self.execution.thread_is_active.return_value = False
        self.queue_policy.depth.return_value = 0
        self.execution.schedule_queue_drain = Mock()
        self.execution.start_thread_turn_now = AsyncMock()
        result = await self.service.dispatch(self.binding, 'Original recovery', 'owner-work-watchdog',
            work_item_ref='group/project#1', owner_routing_pin=pin)
        self.assertTrue(result['queued'])
        self.execution.start_thread_turn_now.assert_not_called()
        kwargs = self.execution.enqueue_turn.call_args.kwargs
        self.assertIs(kwargs['owner_routing_pin'], pin)
        self.assertEqual(kwargs['agent_profile_id'], profile.profile_id)
        self.assertEqual(kwargs['agent_profile_revision'], 3)
        self.assertEqual(kwargs['agent_profile_actor_id'], actor.identity_id)
        self.execution.schedule_queue_drain.assert_called_once_with(self.binding.thread_id)

    async def test_internal_recipient_queues_to_platform_without_rewriting_core_source_or_reply(self):
        self.install_captured_target()
        before = self.binding.model_dump(mode='json')
        pin = SimpleNamespace(recipient=SimpleNamespace(thread_id='platform-thread'))
        profile = SimpleNamespace(profile_id='platform-quinn', revision=6, execution_profile_id='repository-write')
        self.service.owner_routing_context = Mock(return_value=(profile, SimpleNamespace(identity_id='root')))
        result = await self.service.dispatch(self.binding, 'Review exact Platform artifact', 'work-item-handoff',
            owner_routing_pin=pin, repository_resource_id='platform', writable_repository_resource_ids=('platform',))
        queued = self.execution.enqueue_turn.call_args.kwargs
        self.assertEqual(queued['thread_id'], 'platform-thread')
        self.assertEqual(queued['agent_profile_id'], 'platform-quinn')
        self.assertEqual(queued['reply_target'], self.captured)
        self.assertEqual(result['threadId'], 'platform-thread')
        self.execution.schedule_queue_drain.assert_called_once_with('platform-thread')
        self.service.settings.get.assert_called_once_with('platform-thread')
        self.service.bindings.upsert.assert_not_called()
        self.assertEqual(self.binding.model_dump(mode='json'), before)

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

    async def test_concurrent_start_retains_original_scope_and_profile(self):
        self.execution.thread_is_active.return_value = False
        self.queue_policy.depth.return_value = 0
        self.execution.start_thread_turn_now = AsyncMock(side_effect=HTTPException(
            409, {"code": "thread_turn_already_active", "threadId": "thread-1"}))
        self.service.agent_profile_resolver = Mock(return_value=(
            SimpleNamespace(profile_id="veridataops-dana", revision=6,
                            execution_profile_id="repository-write"),
            SimpleNamespace(identity_id="local-admin")))
        result = await self.service.dispatch(self.binding, "finish SAME MR52", "operator-retry",
            work_item_ref="veridataops/data-packs!52", repository_resource_id="repo-data",
            writable_repository_resource_ids=("repo-data",), handoff_requested_at=123)
        self.assertTrue(result["queued"])
        self.execution.start_thread_turn_now.assert_awaited_once()
        self.execution.enqueue_turn.assert_called_once()
        kwargs = self.execution.enqueue_turn.call_args.kwargs
        for key, value in {"work_item_ref":"veridataops/data-packs!52",
            "repository_resource_id":"repo-data", "writable_repository_resource_ids":("repo-data",),
            "handoff_requested_at":123, "agent_profile_id":"veridataops-dana",
            "agent_profile_revision":6, "agent_profile_actor_id":"local-admin"}.items():
            self.assertEqual(kwargs[key], value)

    async def test_idle_only_concurrent_start_keeps_background_quiet(self):
        self.execution.thread_is_active.return_value = False
        self.queue_policy.depth.return_value = 0
        self.execution.start_thread_turn_now = AsyncMock(side_effect=HTTPException(
            409, {"code":"thread_turn_already_active", "threadId":"thread-1"}))
        result = await self.service.dispatch(self.binding, "wake", "watchdog", require_idle=True)
        self.assertEqual(result["skipped"], "already_active")
        self.execution.enqueue_turn.assert_not_called()

    async def test_unrelated_conflicts_are_not_queued_or_replayed(self):
        self.execution.thread_is_active.return_value = False
        self.queue_policy.depth.return_value = 0
        self.service.resume = SimpleNamespace(is_timeout_error=lambda e:False, is_stale_thread_error=lambda e:False)
        for detail in ({"code":"uncertain_start", "threadId":"thread-1"},
                       {"code":"thread_turn_already_active", "threadId":"another-thread"}):
            self.execution.start_thread_turn_now = AsyncMock(side_effect=HTTPException(409, detail))
            with self.assertRaises(HTTPException):
                await self.service.dispatch(self.binding, "finish", "operator-retry")
            self.execution.enqueue_turn.assert_not_called()

    async def test_replacement_start_race_retains_verified_replacement(self):
        self.execution.thread_is_active.return_value = False
        self.queue_policy.depth.return_value = 0
        self.execution.start_thread_turn_now = AsyncMock(side_effect=[RuntimeError("stale"),
            HTTPException(409, {"code":"thread_turn_already_active", "threadId":"replacement"})])
        self.service.resume = SimpleNamespace(is_timeout_error=lambda e:False, is_stale_thread_error=lambda e:True)
        self.service.recovery.replace_stale_bot_thread = AsyncMock(return_value=
            self.binding.model_copy(update={"thread_id":"replacement"}))
        result = await self.service.dispatch(self.binding, "finish SAME52", "operator-retry",
            work_item_ref="veridataops/data-packs!52", repository_resource_id="repo-data",
            writable_repository_resource_ids=("repo-data",))
        self.assertTrue(result["queued"])
        self.assertEqual(self.execution.enqueue_turn.call_args.kwargs["thread_id"], "replacement")
        self.assertEqual(self.execution.start_thread_turn_now.await_count, 2)

    async def test_compatibility_start_race_retains_original_work_item(self):
        host = SimpleNamespace(
            _project=self.service.projects.get, _thread_run_settings=self.service.settings.get,
            _conversation_target_for_binding=self.service.targets.conversation_target,
            _release_stale_active_turn=Mock(), _thread_is_active=Mock(return_value=False),
            _thread_queue_depth=Mock(return_value=0),
            _find_duplicate_queued_turn=Mock(return_value=None),
            _enqueue_turn=Mock(return_value=SimpleNamespace(id="queued")),
            _upsert_bot_binding=Mock(), _append_bot_event=Mock(),
            _publish_queue_status=AsyncMock(), hub=SimpleNamespace(publish=AsyncMock()),
            _start_thread_turn_now=AsyncMock(side_effect=HTTPException(409,
                {"code":"thread_turn_already_active", "threadId":"thread-1"})),
            _is_codex_timeout_error=lambda e:False, _is_stale_thread_error=lambda e:False,
        )
        result = await BotEventDispatchCompatibilityFacade(host).dispatch(
            self.binding, "finish existing52", "operator-retry", work_item_ref="veridataops/data-packs!52",
            repository_resource_id="repo-data", writable_repository_resource_ids=("repo-data",))
        self.assertTrue(result["queued"])
        host._enqueue_turn.assert_called_once()
        self.assertEqual(host._enqueue_turn.call_args.kwargs["work_item_ref"], "veridataops/data-packs!52")
        self.assertEqual(host._enqueue_turn.call_args.kwargs["writable_repository_resource_ids"], ("repo-data",))

    def setUp(self) -> None:
        self.execution = SimpleNamespace(
            find_duplicate_queued_turn=Mock(return_value=None),
            enqueue_turn=Mock(return_value=SimpleNamespace(id="queued-1")),
            schedule_queue_drain=Mock(),
            publish_queue_status=AsyncMock(),
            thread_is_active=Mock(return_value=True),
            thread_has_live_agent_runtime_session=Mock(return_value=True),
            thread_has_inflight_agent_runtime_assignment=Mock(return_value=False),
        )
        self.queue_policy = SimpleNamespace(
            depth=Mock(return_value=1), queue=Mock(return_value=[object()]),
        )
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
            handoff_requested_at=100,
            repository_resource_id="repo-saas-app",
            writable_repository_resource_ids=("repo-saas-app",),
        )

        kwargs = self.execution.enqueue_turn.call_args.kwargs
        self.assertEqual(kwargs["work_item_ref"], "veridataops/saas-app#379")
        self.assertEqual(kwargs["handoff_requested_at"], 100)
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
