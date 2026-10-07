from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.models import BotBinding
from codex_web.services.bot_event_dispatch import BotEventDispatchService


class BotEventDispatchScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.execution = SimpleNamespace(
            find_duplicate_queued_turn=Mock(return_value=None),
            enqueue_turn=Mock(return_value=SimpleNamespace(id="queued-1")),
            publish_queue_status=AsyncMock(),
            thread_is_active=Mock(return_value=True),
        )
        self.queue_policy = SimpleNamespace(depth=Mock(return_value=1))
        self.service = BotEventDispatchService(
            projects=SimpleNamespace(
                get=Mock(return_value=SimpleNamespace(id="project-1", model="model-1"))
            ),
            settings=SimpleNamespace(
                get=Mock(return_value=SimpleNamespace(model=None, reasoning_effort=None))
            ),
            targets=SimpleNamespace(conversation_target=Mock(return_value=None)),
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


if __name__ == "__main__":
    unittest.main()
